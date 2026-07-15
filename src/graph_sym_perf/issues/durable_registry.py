"""Process-safe run-local duplicate registry for submit-ready promotion."""

from __future__ import annotations

import fcntl
import json
import os
import hashlib
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.issues.dedup import (
    FamilyMatchKind,
    FingerprintSubject,
    IssueFamilyIndex,
    IssueFingerprint,
    fingerprint_issue,
    fingerprint_validated_reduction,
    known_regression_signature_from_manifest,
)
from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    PipelineStage,
    VersionedIssueRecord,
    canonical_content_hash,
    canonical_validation_report_hash,
    canonical_validation_provenance_hash,
)
from graph_sym_perf.validation.promotion import deterministic_issue_id
from graph_sym_perf.validation.promotion import promote


def promote_unique_issue(
    root: str | Path,
    *,
    validation: object,
    reduction: object,
    certificate: object,
    dbms: str,
    known_regression_manifest: str | Path,
    original_validation_ref: ArtifactReference,
    reduced_validation_ref: ArtifactReference,
    materialize: Callable[[object], dict[str, object]],
) -> tuple[object | None, str]:
    """Promote and atomically reserve a unique issue, or return a duplicate reason."""

    if not callable(materialize):
        raise TypeError("formal issue promotion requires a materialize callback")
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    lock_path = root_path / ".registry.lock"
    registry_path = root_path / "registry.jsonl"
    review_path = root_path / "dedup_review.jsonl"
    known_path = root_path / "known_regressions.jsonl"
    provenance = _validation_provenance(
        validation,
        reduction,
        original_validation_ref=original_validation_ref,
        reduced_validation_ref=reduced_validation_ref,
    )
    issue_id = deterministic_issue_id(validation, reduction)
    fingerprint = _validated_fingerprint(validation, reduction, certificate, dbms=dbms)
    known = (known_regression_signature_from_manifest(known_regression_manifest),)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_family_transaction(root_path)
        _validate_known_diagnostics(known_path)
        records = _load_records(registry_path, review_path)
        index = _replay_family_index(records, known_regressions=known)
        existing_ids = {item.issue_id for item in records}
        existing_owner = next(
            (
                item
                for item in records
                if item.issue_id == issue_id and item.stage is PipelineStage.SUBMIT_READY
            ),
            None,
        )
        if existing_owner is not None:
            index.reissue_owner(fingerprint, issue_id=issue_id)
            if not _record_provenance_matches(existing_owner, provenance):
                return None, "owner_validation_provenance_drift"
            if _owner_commit_is_current(root_path, existing_owner):
                return None, "owner_already_committed"
        decision = (
            index.reissue_owner(fingerprint, issue_id=issue_id)
            if existing_owner is not None
            else index.add(fingerprint, issue_id=issue_id)
        )
        if decision.known_regression_match:
            diagnostic = {
                "case_id": decision.known_regression_case_id,
                "decision_hash": decision.decision_hash,
                "fingerprint_hash": fingerprint.fingerprint_hash,
                "issue_id": issue_id,
                "known_regression_match": True,
                **provenance,
            }
            _append_payload(
                known_path,
                {
                    **diagnostic,
                    "diagnostic_hash": _hash_payload("known-regression-diagnostic-v1", diagnostic),
                },
            )
            return None, "known_regression_excluded"
        record = _record(
            issue_id,
            validation,
            reduction,
            certificate,
            dbms,
            fingerprint=fingerprint,
            family_decision=decision,
            validation_provenance=provenance,
            stage=PipelineStage.REDUCED_VALID,
        )
        if existing_owner is not None:
            record = replace(
                existing_owner,
                stage=PipelineStage.REDUCED_VALID,
                family_id=str(decision.family_id),
                family_version_hash=decision.family_version_hash,
                family_decision_hash=decision.decision_hash,
                owner_issue_id=str(decision.owner_issue_id),
            )
        if not decision.is_new_family and decision.owner_issue_id != issue_id:
            if issue_id in existing_ids:
                record = next(item for item in records if item.issue_id == issue_id)
            merged_records = _records_with_current_family(
                index,
                tuple(item for item in records if item.issue_id != issue_id) + (record,),
                family_id=str(decision.family_id),
            )
            _commit_family_transaction(
                root_path,
                previous_records=records,
                records=merged_records,
            )
            _before_owner_artifact_renewal(root_path)
            _renew_current_family_owner_unlocked(
                root_path,
                owner_issue_id=str(decision.owner_issue_id),
            )
            status = (
                "exact_family_duplicate"
                if decision.match_kind is FamilyMatchKind.EXACT
                else "structural_family_duplicate"
            )
            return None, status
        family_transaction_records: tuple[VersionedIssueRecord, ...] | None = None
        if not decision.is_new_family:
            decision = index.reissue_owner(fingerprint, issue_id=issue_id)
            record = replace(
                record,
                family_id=str(decision.family_id),
                family_version_hash=decision.family_version_hash,
                family_decision_hash=decision.decision_hash,
                owner_issue_id=str(decision.owner_issue_id),
            )
            family_transaction_records = _records_with_current_family(
                index,
                tuple(item for item in records if item.issue_id != issue_id) + (record,),
                family_id=str(decision.family_id),
            )
        family_records = [
            item
            for item in (family_transaction_records or records)
            if item.family_id == decision.family_id
        ]
        if issue_id not in {item.issue_id for item in family_records}:
            family_records.append(record)
        clearance = IssueRegistry().duplicate_clearance_for_family(decision, family_records)
        issue = promote(
            validation,
            reduction_report=reduction,
            certificate=certificate,
            duplicate_clearance=clearance,
            family_decision=decision,
        )
        if issue is None:
            return None, "promotion_gate_failed"
        materialized = materialize(issue)
        committed = VersionedIssueRecord(
            **{**record.__dict__, "stage": PipelineStage.SUBMIT_READY}
        )
        if family_transaction_records is not None:
            committed_records = tuple(
                replace(item, stage=PipelineStage.SUBMIT_READY)
                if item.issue_id == issue_id
                else item
                for item in family_transaction_records
            )
            _commit_family_transaction(
                root_path,
                previous_records=records,
                records=committed_records,
            )
            status = "family_owner_changed"
        elif issue_id in existing_ids:
            _replace_record(registry_path, committed)
            status = "family_owner_repaired"
        else:
            _append_record(registry_path, committed)
            status = "unique_family"
        _write_commit(root_path, committed, materialized)
        return issue, status


def committed_owner_outcome(root: str | Path, *, issue_id: str) -> dict[str, object]:
    """Return a verified terminal outcome without rerunning or rewriting evidence."""

    root_path = Path(root)
    with (root_path / ".registry.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_family_transaction(root_path)
        records = _load_records(root_path / "registry.jsonl", root_path / "dedup_review.jsonl")
        owners = tuple(
            record
            for record in records
            if record.issue_id == issue_id and record.stage is PipelineStage.SUBMIT_READY
        )
        if len(owners) != 1 or not _owner_commit_is_current(root_path, owners[0]):
            raise ValueError("committed family owner outcome is unavailable")
        record = owners[0]
        commit = json.loads((root_path / "commits" / f"{issue_id}.json").read_text(encoding="utf-8"))
        index_path = root_path / "index" / f"{issue_id}.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        receipt_fields = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
        if any(index.get(name) != commit.get(name) for name in receipt_fields):
            raise ValueError("committed family owner receipt is inconsistent")
        from graph_sym_perf.artifacts.store import (
            ContentAddressedStore,
            load_artifact_record,
            verify_issue_hash_chain,
        )

        run_dir = root_path.parent.resolve()
        store_path = (run_dir / str(commit["artifact_store"])).resolve()
        export_path = (run_dir / str(commit["export_path"])).resolve()
        if run_dir not in store_path.parents or run_dir not in export_path.parents:
            raise ValueError("committed family owner receipt escapes the run directory")
        store = ContentAddressedStore(store_path, budget_bytes=(1 << 63) - 1)
        issue_record = load_artifact_record(store, commit["issue_ref"])
        verify_issue_hash_chain(store, issue_record)
        if not _durable_family_payload_is_current_unlocked(
            root_path,
            issue_record.payload,
        ):
            raise ValueError("committed family owner artifact is stale")
        if hashlib.sha256(export_path.read_bytes()).hexdigest() != commit["export_sha256"]:
            raise ValueError("committed family owner export is stale")
        return {
            "issue_id": issue_id,
            "issue_index": str(index_path.relative_to(root_path.parent)),
            "original_validation_hash": record.original_validation_hash,
            "reduced_validation_hash": record.reduced_validation_hash,
            "original_validation_ref": dict(record.original_validation_ref or {}),
            "reduced_validation_ref": dict(record.reduced_validation_ref or {}),
        }


def renew_current_family_owner(root: str | Path, *, owner_issue_id: str) -> bool:
    """Recover a stale union owner before currentness checks or CLI export."""

    root_path = Path(root)
    with (root_path / ".registry.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_family_transaction(root_path)
        records = _load_records(root_path / "registry.jsonl", root_path / "dedup_review.jsonl")
        owners = tuple(
            record
            for record in records
            if record.issue_id == owner_issue_id and record.stage is PipelineStage.SUBMIT_READY
        )
        if len(owners) != 1:
            raise ValueError("current family owner is unavailable")
        if _owner_commit_is_current(root_path, owners[0]):
            return False
        return _renew_current_family_owner_unlocked(
            root_path,
            owner_issue_id=owner_issue_id,
        )


def _before_owner_artifact_renewal(_root: Path) -> None:
    """Test-only crash seam after durable union and before artifact renewal."""


def _renew_current_family_owner_unlocked(
    root: Path,
    *,
    owner_issue_id: str,
) -> bool:
    """Re-seal a stale owner from its own committed evidence after family growth."""

    from graph_sym_perf.artifacts.schema import ArtifactRecord
    from graph_sym_perf.artifacts.store import (
        ContentAddressedStore,
        load_artifact_record,
        verify_issue_hash_chain,
    )

    records = _load_records(root / "registry.jsonl", root / "dedup_review.jsonl")
    owner_records = tuple(
        record
        for record in records
        if record.issue_id == owner_issue_id and record.stage is PipelineStage.SUBMIT_READY
    )
    if len(owner_records) != 1:
        raise ValueError("family renewal requires exactly one current owner")
    owner_record = owner_records[0]
    if _owner_commit_is_current(root, owner_record):
        return False
    commit_path = root / "commits" / f"{owner_issue_id}.json"
    if not commit_path.is_file():
        return False
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    if not isinstance(commit, dict) or not required <= set(commit):
        raise ValueError("family owner commit receipt is incomplete")
    run_dir = root.parent.resolve()
    store_path = (run_dir / str(commit["artifact_store"])).resolve()
    export_path = (run_dir / str(commit["export_path"])).resolve()
    if run_dir not in store_path.parents or run_dir not in export_path.parents:
        raise ValueError("family owner receipt escapes the run directory")
    if not store_path.is_dir():
        # Unit-level durable-registry tests use receipt-only materializers.
        return False
    store = ContentAddressedStore(store_path, budget_bytes=(1 << 63) - 1)
    old_issue = load_artifact_record(store, commit["issue_ref"])
    verify_issue_hash_chain(store, old_issue)
    old_payload = dict(old_issue.payload)
    old_family = old_payload.get("family_decision")
    if not isinstance(old_family, Mapping) or old_family.get("issue_id") != owner_issue_id:
        raise ValueError("sealed owner artifact has no family identity")
    if (
        old_payload.get("original_validation_hash") != owner_record.original_validation_hash
        or old_payload.get("reduced_validation_hash") != owner_record.reduced_validation_hash
        or stable_json_dumps(old_payload.get("original_validation_ref"))
        != stable_json_dumps(owner_record.original_validation_ref)
        or stable_json_dumps(old_payload.get("final_validation_ref"))
        != stable_json_dumps(owner_record.reduced_validation_ref)
    ):
        raise ValueError("family renewal cannot replace owner validation provenance")
    current, clearance = _replay_family_owner(root, owner_issue_id)
    if stable_json_dumps(old_family.get("fingerprint")) != stable_json_dumps(
        asdict(current.fingerprint)
    ):
        raise ValueError("family renewal cannot replace owner fingerprint")
    family_payload = {
        "issue_id": current.issue_id,
        "owner_issue_id": current.owner_issue_id,
        "family_id": current.family_id,
        "fingerprint": asdict(current.fingerprint),
        "match_kind": current.match_kind.value,
        "matched_issue_id": current.matched_issue_id,
        "known_regression_match": current.known_regression_match,
        "known_regression_case_id": current.known_regression_case_id,
        "is_new_family": current.is_new_family,
        "family_members": current.family_members,
        "family_fingerprints": tuple(
            (member, asdict(fingerprint))
            for member, fingerprint in current.family_fingerprints
        ),
        "family_version_hash": current.family_version_hash,
        "decision_hash": current.decision_hash,
    }
    clearance_payload = {
        "issue_id": clearance.issue_id,
        "possible_duplicate": clearance.possible_duplicate,
        "cluster_members": clearance.cluster_members,
        "cluster_version_hash": clearance.cluster_version_hash,
        "evidence_url": clearance.evidence_url,
        "registry_signature": clearance.registry_signature,
        "family_id": clearance.family_id,
        "family_version_hash": clearance.family_version_hash,
        "family_decision_hash": clearance.family_decision_hash,
    }
    old_payload.pop("issue_hash", None)
    renewed = ArtifactRecord.seal_issue(
        {
            **old_payload,
            "family_id": current.family_id,
            "family_decision": family_payload,
            "duplicate_clearance": clearance_payload,
        }
    )
    verify_issue_hash_chain(store, renewed)
    issue_ref = store.put_json(f"issue-{owner_issue_id}.json", renewed.to_dict())
    fingerprint = family_payload["fingerprint"]
    certificate = renewed.payload.get("certificate")
    if not isinstance(fingerprint, Mapping) or not isinstance(certificate, Mapping):
        raise ValueError("renewed issue cannot produce a typed export")
    exported = {
        "issue_id": owner_issue_id,
        "family_id": current.family_id,
        "stage": "submit_ready",
        "dbms_version": renewed.payload.get("dbms_version"),
        "rule_id": certificate.get("rule_id"),
        "validated_ratio": renewed.payload.get("validated_ratio"),
        "reduction_status": renewed.payload.get("reduction_status"),
        "reduced_original": renewed.payload.get("reduced_query_a"),
        "reduced_transformed": renewed.payload.get("reduced_query_b"),
        "exact_signature": fingerprint.get("exact"),
        "structural_signature": fingerprint.get("structural"),
        "submission_path": fingerprint.get("submission_path"),
        "reduced_validation_phenotype": fingerprint.get("reduced_validation_phenotype"),
        "family_version_hash": current.family_version_hash,
        "family_decision_hash": current.decision_hash,
        "original_validation_hash": owner_record.original_validation_hash,
        "reduced_validation_hash": owner_record.reduced_validation_hash,
        "original_validation_ref": dict(owner_record.original_validation_ref or {}),
        "reduced_validation_ref": dict(owner_record.reduced_validation_ref or {}),
    }
    _write_json_atomic(export_path, exported)
    receipt = {
        "artifact_store": commit["artifact_store"],
        "export_path": commit["export_path"],
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
        "issue_id": owner_issue_id,
        "issue_ref": asdict(issue_ref),
    }
    _write_json_atomic(root / "index" / f"{owner_issue_id}.json", receipt)
    _write_commit(root, owner_record, receipt)
    return True


def durable_family_credentials_are_current(
    root: str | Path,
    *,
    decision: object,
    clearance: object,
) -> bool:
    """Recompute the durable family snapshot before re-export or replay."""

    from graph_sym_perf.issues.dedup import IssueFamilyDecision
    from graph_sym_perf.validation.models import DuplicateClearance

    if (
        not isinstance(decision, IssueFamilyDecision)
        or not isinstance(clearance, DuplicateClearance)
        or not decision.verify()
        or not clearance.verify()
        or decision.family_id is None
    ):
        return False
    root_path = Path(root)
    lock_path = root_path / ".registry.lock"
    try:
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _recover_family_transaction(root_path)
            records = _load_records(
                root_path / "registry.jsonl",
                root_path / "dedup_review.jsonl",
            )
            index = _replay_family_index(records)
            fingerprints = {
                record.issue_id: _record_fingerprint(record) for record in records
            }
            owner_fingerprint = fingerprints.get(decision.issue_id)
            if owner_fingerprint is None:
                return False
            current = index.reissue_owner(owner_fingerprint, issue_id=decision.issue_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return bool(
        current.verify()
        and current.is_new_family
        and current.owner_issue_id == decision.issue_id
        and decision.owner_issue_id == decision.issue_id
        and current.family_id == decision.family_id
        and current.family_version_hash == decision.family_version_hash
        and current.decision_hash == decision.decision_hash
        and clearance.issue_id == decision.issue_id
        and clearance.family_id == current.family_id
        and clearance.family_version_hash == current.family_version_hash
        and clearance.family_decision_hash == current.decision_hash
        and clearance.cluster_members == tuple(member for member, _exact in current.family_members)
        and clearance.cluster_version_hash == current.family_version_hash
    )


def durable_family_payload_is_current(
    root: str | Path,
    issue_payload: object,
) -> bool:
    """Verify a sealed artifact's family decision against the durable logs."""

    root_path = Path(root)
    try:
        with (root_path / ".registry.lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _recover_family_transaction(root_path)
            if isinstance(issue_payload, Mapping):
                issue_id = str(issue_payload.get("issue_id", ""))
                records = _load_records(
                    root_path / "registry.jsonl",
                    root_path / "dedup_review.jsonl",
                )
                owner = next(
                    (
                        record
                        for record in records
                        if record.issue_id == issue_id
                        and record.stage is PipelineStage.SUBMIT_READY
                    ),
                    None,
                )
                if owner is not None and not _owner_commit_is_current(root_path, owner):
                    _renew_current_family_owner_unlocked(
                        root_path,
                        owner_issue_id=issue_id,
                    )
            return _durable_family_payload_is_current_unlocked(root_path, issue_payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _durable_family_payload_is_current_unlocked(
    root: Path,
    issue_payload: object,
) -> bool:
    """Validate a payload while the caller holds the durable registry lock."""

    if not isinstance(issue_payload, Mapping):
        return False
    family = issue_payload.get("family_decision")
    clearance = issue_payload.get("duplicate_clearance")
    issue_id = issue_payload.get("issue_id")
    if not isinstance(family, Mapping) or not isinstance(clearance, Mapping):
        return False
    try:
        current, current_clearance = _replay_family_owner(root, str(issue_id))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    expected_family = {
        "issue_id": current.issue_id,
        "owner_issue_id": current.owner_issue_id,
        "family_id": current.family_id,
        "fingerprint": asdict(current.fingerprint),
        "match_kind": current.match_kind.value,
        "matched_issue_id": current.matched_issue_id,
        "known_regression_match": current.known_regression_match,
        "known_regression_case_id": current.known_regression_case_id,
        "is_new_family": current.is_new_family,
        "family_members": current.family_members,
        "family_fingerprints": tuple(
            (member, asdict(fingerprint))
            for member, fingerprint in current.family_fingerprints
        ),
        "family_version_hash": current.family_version_hash,
        "decision_hash": current.decision_hash,
    }
    expected_clearance = {
        "issue_id": current_clearance.issue_id,
        "possible_duplicate": current_clearance.possible_duplicate,
        "cluster_members": current_clearance.cluster_members,
        "cluster_version_hash": current_clearance.cluster_version_hash,
        "evidence_url": current_clearance.evidence_url,
        "registry_signature": current_clearance.registry_signature,
        "family_id": current_clearance.family_id,
        "family_version_hash": current_clearance.family_version_hash,
        "family_decision_hash": current_clearance.family_decision_hash,
    }
    return bool(
        issue_payload.get("family_id") == current.family_id
        and stable_json_dumps(dict(family)) == stable_json_dumps(expected_family)
        and stable_json_dumps(dict(clearance)) == stable_json_dumps(expected_clearance)
    )


def _replay_family_owner(root: Path, issue_id: str) -> tuple[object, object]:
    records = _load_records(
        root / "registry.jsonl",
        root / "dedup_review.jsonl",
    )
    index = _replay_family_index(records)
    fingerprints = {record.issue_id: _record_fingerprint(record) for record in records}
    owner_records = tuple(record for record in records if record.owner_issue_id == issue_id)
    if len(owner_records) < 1 or any(record.family_id != owner_records[0].family_id for record in owner_records):
        raise ValueError("durable family owner binding is ambiguous")
    owner_fingerprint = fingerprints.get(issue_id)
    if owner_fingerprint is None:
        raise ValueError("durable family owner is absent")
    current = index.reissue_owner(owner_fingerprint, issue_id=issue_id)
    family_records = tuple(record for record in records if record.family_id == current.family_id)
    current_clearance = IssueRegistry().duplicate_clearance_for_family(
        current,
        family_records,
    )
    return current, current_clearance


def _replay_family_index(
    records: tuple[VersionedIssueRecord, ...],
    *,
    known_regressions: tuple[object, ...] = (),
) -> IssueFamilyIndex:
    """Rebuild families from explicit owners, independently of append order."""

    if not records:
        return IssueFamilyIndex(known_regressions=known_regressions)
    by_family: dict[str, list[VersionedIssueRecord]] = {}
    for record in records:
        if not record.family_id or not record.owner_issue_id:
            raise ValueError("durable family has no explicit owner binding")
        by_family.setdefault(record.family_id, []).append(record)
    index = IssueFamilyIndex(known_regressions=known_regressions)
    for family_id, members in sorted(by_family.items()):
        owner_ids = {member.owner_issue_id for member in members}
        if len(owner_ids) != 1:
            raise ValueError("durable family has conflicting owners")
        owner_id = next(iter(owner_ids))
        owners = [member for member in members if member.issue_id == owner_id]
        submit_ready = [member for member in members if member.stage is PipelineStage.SUBMIT_READY]
        if len(owners) != 1 or len(submit_ready) != 1 or submit_ready[0].issue_id != owner_id:
            raise ValueError("durable family must have exactly one submit-ready owner")
        ordered = owners + sorted(
            (member for member in members if member.issue_id != owner_id),
            key=lambda member: member.issue_id,
        )
        for member in ordered:
            index.add(_record_fingerprint(member), issue_id=member.issue_id)
    for record in records:
        fingerprint = _record_fingerprint(record)
        decision = (
            index.reissue_owner(fingerprint, issue_id=record.issue_id)
            if record.stage is PipelineStage.SUBMIT_READY
            else index.reissue_member(fingerprint, issue_id=record.issue_id)
        )
        if (
            decision.family_id != record.family_id
            or decision.owner_issue_id != record.owner_issue_id
            or decision.family_version_hash != record.family_version_hash
            or decision.decision_hash != record.family_decision_hash
        ):
            raise ValueError("durable family owner/family identity does not replay")
    return index


def _validation_provenance(
    validation: object,
    reduction: object,
    *,
    original_validation_ref: ArtifactReference,
    reduced_validation_ref: ArtifactReference,
) -> dict[str, object]:
    if not isinstance(original_validation_ref, ArtifactReference) or not isinstance(
        reduced_validation_ref,
        ArtifactReference,
    ):
        raise TypeError("durable promotion requires typed validation artifact references")
    original_hash = str(getattr(reduction, "original_validation_hash", ""))
    reduced_hash = str(getattr(reduction, "reduced_validation_hash", ""))
    final_validation = getattr(reduction, "final_validation", None)
    if (
        original_hash != canonical_validation_report_hash(validation)
        or reduced_hash != canonical_validation_report_hash(final_validation)
        or original_validation_ref.content_hash == reduced_validation_ref.content_hash
    ):
        raise ValueError("validation artifact provenance does not bind both reports")
    original_ref = asdict(original_validation_ref)
    reduced_ref = asdict(reduced_validation_ref)
    provenance = {
        "original_validation_hash": original_hash,
        "reduced_validation_hash": reduced_hash,
        "original_validation_ref": original_ref,
        "reduced_validation_ref": reduced_ref,
    }
    provenance["validation_provenance_hash"] = canonical_validation_provenance_hash(
        original_validation_hash=original_hash,
        reduced_validation_hash=reduced_hash,
        original_validation_ref=original_ref,
        reduced_validation_ref=reduced_ref,
    )
    return provenance


def _validate_known_diagnostics(path: Path) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("known regression diagnostic must be an object")
            diagnostic_hash = payload.pop("diagnostic_hash", None)
            if (
                not _validation_provenance_is_intact(payload)
                or diagnostic_hash
                != _hash_payload("known-regression-diagnostic-v1", payload)
            ):
                raise ValueError("known regression diagnostic provenance is invalid")


def _validation_provenance_is_intact(payload: Mapping[str, object]) -> bool:
    original_hash = payload.get("original_validation_hash")
    reduced_hash = payload.get("reduced_validation_hash")
    original_ref = payload.get("original_validation_ref")
    reduced_ref = payload.get("reduced_validation_ref")
    if (
        not _is_sha256(original_hash)
        or not _is_sha256(reduced_hash)
        or not isinstance(original_ref, Mapping)
        or not isinstance(reduced_ref, Mapping)
        or not _is_sha256(original_ref.get("content_hash"))
        or not _is_sha256(reduced_ref.get("content_hash"))
        or original_ref.get("content_hash") == reduced_ref.get("content_hash")
    ):
        return False
    expected = canonical_validation_provenance_hash(
        original_validation_hash=str(original_hash),
        reduced_validation_hash=str(reduced_hash),
        original_validation_ref=original_ref,
        reduced_validation_ref=reduced_ref,
    )
    return payload.get("validation_provenance_hash") == expected


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _hash_payload(domain: str, payload: object) -> str:
    return hashlib.sha256(
        (domain + "\0" + stable_json_dumps(payload)).encode("utf-8")
    ).hexdigest()


def _record(
    issue_id: str,
    validation: object,
    reduction: object,
    certificate: object,
    dbms: str,
    *,
    fingerprint: IssueFingerprint,
    family_decision: object,
    validation_provenance: Mapping[str, object],
    stage: PipelineStage,
) -> VersionedIssueRecord:
    pair = getattr(reduction, "reduced_pair")
    query_a = pair.query_a
    query_b = pair.query_b
    rule = getattr(certificate, "rule_id")
    now = datetime.now(timezone.utc).isoformat()
    return VersionedIssueRecord(
        issue_id=issue_id,
        stage=stage,
        developer_status=DeveloperStatus.WAITING_REPLY,
        dbms=dbms,
        dbms_version=getattr(validation, "manifest").dbms_version,
        pair_hash=fingerprint.exact,
        direction=getattr(reduction, "final_evidence").slow_query,
        rule_id=rule,
        detector_version="runtime-first-v3",
        validator_version=getattr(validation, "validation_protocol"),
        reducer_version="paired-ast-ddmin-v1",
        rule_version=getattr(certificate, "proof_version"),
        discovered_at=now,
        site_class=getattr(certificate, "site_class"),
        normalized_pattern=(
            fingerprint.normalized_pair[0]
            if fingerprint.normalized_pair is not None
            else fingerprint.query_a_hash
        ),
        normalized_predicate=(
            fingerprint.normalized_pair[1]
            if fingerprint.normalized_pair is not None
            else fingerprint.query_b_hash
        ),
        reduced_query_a=query_a,
        reduced_query_b=query_b,
        dbms_fingerprint=fingerprint.dbms_fingerprint,
        graph_snapshot_hash=fingerprint.graph_snapshot_hash,
        family_id=str(getattr(family_decision, "family_id") or ""),
        family_version_hash=str(getattr(family_decision, "family_version_hash")),
        family_decision_hash=str(getattr(family_decision, "decision_hash")),
        owner_issue_id=str(getattr(family_decision, "owner_issue_id") or ""),
        exact_signature=fingerprint.exact,
        structural_signature=fingerprint.structural,
        fingerprint_hash=fingerprint.fingerprint_hash,
        submission_path=fingerprint.submission_path,
        reduced_phenotype=fingerprint.reduced_validation_phenotype,
        known_regression_match=bool(getattr(family_decision, "known_regression_match")),
        original_validation_hash=str(validation_provenance["original_validation_hash"]),
        reduced_validation_hash=str(validation_provenance["reduced_validation_hash"]),
        original_validation_ref=dict(validation_provenance["original_validation_ref"]),
        reduced_validation_ref=dict(validation_provenance["reduced_validation_ref"]),
        validation_provenance_hash=str(validation_provenance["validation_provenance_hash"]),
    )


def _validated_fingerprint(
    validation: object,
    reduction: object,
    certificate: object,
    *,
    dbms: str,
) -> IssueFingerprint:
    return fingerprint_validated_reduction(
        validation,
        reduction,
        certificate,
        dbms=dbms,
    )


def _record_fingerprint(record: VersionedIssueRecord) -> IssueFingerprint:
    if not record.exact_signature or not record.dbms_fingerprint:
        raise ValueError("formal durable registry contains a legacy duplicate record")
    fingerprint = fingerprint_issue(
        FingerprintSubject(
            dbms=record.dbms,
            dbms_version=record.dbms_version,
            dbms_fingerprint=record.dbms_fingerprint,
            graph_snapshot_hash=record.graph_snapshot_hash,
            canonical_rule_id=record.rule_id,
            reduced_query_a=record.reduced_query_a or "",
            reduced_query_b=record.reduced_query_b or "",
            runtime_direction=record.direction,
            site_class=record.site_class,
            submission_path=record.submission_path,
            reduced_validation_phenotype=record.reduced_phenotype,
        )
    )
    if (
        fingerprint.exact != record.exact_signature
        or fingerprint.structural != record.structural_signature
        or fingerprint.fingerprint_hash != record.fingerprint_hash
    ):
        raise ValueError("durable family fingerprint no longer verifies")
    return fingerprint


def _records_with_current_family(
    index: IssueFamilyIndex,
    records: tuple[VersionedIssueRecord, ...],
    *,
    family_id: str,
) -> tuple[VersionedIssueRecord, ...]:
    """Bind every member record to the authoritative post-union decision."""

    member_records = tuple(record for record in records if record.family_id == family_id)
    if not member_records:
        # A bridge retains old family IDs until this atomic rewrite.
        member_ids: set[str] = set()
        for record in records:
            try:
                decision = (
                    index.reissue_owner(_record_fingerprint(record), issue_id=record.issue_id)
                    if record.stage is PipelineStage.SUBMIT_READY
                    else index.reissue_member(_record_fingerprint(record), issue_id=record.issue_id)
                )
            except ValueError:
                continue
            if decision.family_id == family_id:
                member_ids.add(record.issue_id)
        member_records = tuple(record for record in records if record.issue_id in member_ids)
    else:
        # Include bridge members which still carry a losing pre-union family ID.
        member_ids = {record.issue_id for record in member_records}
        for record in records:
            if record.issue_id in member_ids:
                continue
            fingerprint = _record_fingerprint(record)
            try:
                current = index.reissue_member(fingerprint, issue_id=record.issue_id)
            except ValueError:
                try:
                    current = index.reissue_owner(fingerprint, issue_id=record.issue_id)
                except ValueError:
                    continue
            if current.family_id == family_id:
                member_ids.add(record.issue_id)
        member_records = tuple(record for record in records if record.issue_id in member_ids)
    if not member_records:
        raise ValueError("current durable family has no records")

    current_owner: str | None = None
    updated: dict[str, VersionedIssueRecord] = {}
    for record in member_records:
        fingerprint = _record_fingerprint(record)
        try:
            decision = index.reissue_owner(fingerprint, issue_id=record.issue_id)
            current_owner = record.issue_id
            stage = PipelineStage.SUBMIT_READY
        except ValueError:
            decision = index.reissue_member(fingerprint, issue_id=record.issue_id)
            stage = PipelineStage.REDUCED_VALID
        updated[record.issue_id] = replace(
            record,
            stage=stage,
            family_id=str(decision.family_id),
            family_version_hash=decision.family_version_hash,
            family_decision_hash=decision.decision_hash,
            owner_issue_id=str(decision.owner_issue_id),
        )
    if current_owner is None:
        raise ValueError("current durable family has no owner")
    return tuple(updated.get(record.issue_id, record) for record in records)


def _commit_family_transaction(
    root: Path,
    *,
    previous_records: tuple[VersionedIssueRecord, ...],
    records: tuple[VersionedIssueRecord, ...],
) -> None:
    """Crash-recoverable registry/review rewrite under the registry lock."""

    registry_records = tuple(
        record for record in records if record.stage is PipelineStage.SUBMIT_READY
    )
    review_records = tuple(
        record for record in records if record.stage is not PipelineStage.SUBMIT_READY
    )
    previous_by_id = {record.issue_id: record for record in previous_records}
    current_by_id = {record.issue_id: record for record in records}
    changed_ids = {
        issue_id
        for issue_id in previous_by_id.keys() | current_by_id.keys()
        if issue_id not in previous_by_id
        or issue_id not in current_by_id
        or _record_payload(previous_by_id[issue_id])
        != _record_payload(current_by_id[issue_id])
    }
    if not changed_ids:
        return
    component_ids = set(changed_ids)
    changed = True
    while changed:
        changed = False
        before_families = {
            previous_by_id[issue_id].family_id
            for issue_id in component_ids & previous_by_id.keys()
        }
        after_families = {
            current_by_id[issue_id].family_id
            for issue_id in component_ids & current_by_id.keys()
        }
        connected = {
            record.issue_id
            for record in (*previous_records, *records)
            if record.family_id in before_families | after_families
        }
        if not connected <= component_ids:
            component_ids.update(connected)
            changed = True
    before_component = tuple(
        record for record in previous_records if record.issue_id in component_ids
    )
    after_component = tuple(record for record in records if record.issue_id in component_ids)
    audit = {
        "family_ids_before": sorted({record.family_id for record in before_component}),
        "family_ids_after": sorted({record.family_id for record in after_component}),
        "members_before": sorted(record.issue_id for record in before_component),
        "members_after": sorted(record.issue_id for record in after_component),
        "owners_before": sorted(
            record.issue_id
            for record in before_component
            if record.stage is PipelineStage.SUBMIT_READY
        ),
        "owners_after": sorted(
            record.issue_id
            for record in after_component
            if record.stage is PipelineStage.SUBMIT_READY
        ),
        "fingerprints_after": [
            {
                "issue_id": record.issue_id,
                "fingerprint": asdict(_record_fingerprint(record)),
            }
            for record in sorted(after_component, key=lambda item: item.issue_id)
        ],
        "decision_hashes_after": {
            record.issue_id: record.family_decision_hash for record in after_component
        },
    }
    audit["union_id"] = _hash_payload("durable-family-union-v1", audit)
    payload: dict[str, object] = {
        "registry_records": [_record_payload(record) for record in registry_records],
        "review_records": [_record_payload(record) for record in review_records],
        "audit": audit,
    }
    payload["transaction_hash"] = _hash_payload("family-transaction-v1", payload)
    _write_json_atomic(root / "family_transaction.pending.json", payload)
    _apply_family_transaction(root, payload)


def _recover_family_transaction(root: Path) -> None:
    path = root / "family_transaction.pending.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("family transaction journal must be an object")
    transaction_hash = payload.pop("transaction_hash", None)
    if transaction_hash != _hash_payload("family-transaction-v1", payload):
        raise ValueError("family transaction journal hash is invalid")
    payload["transaction_hash"] = transaction_hash
    _apply_family_transaction(root, payload)


def _apply_family_transaction(root: Path, payload: Mapping[str, object]) -> None:
    registry = payload.get("registry_records")
    review = payload.get("review_records")
    audit = payload.get("audit")
    if not isinstance(registry, list) or not isinstance(review, list) or not isinstance(audit, Mapping):
        raise ValueError("family transaction journal is incomplete")
    _write_payload_lines_atomic(root / "registry.jsonl", registry)
    _after_union_registry_replace(root)
    _write_payload_lines_atomic(root / "dedup_review.jsonl", review)
    _append_union_audit_once(root / "family_unions.jsonl", dict(audit))
    (root / "family_transaction.pending.json").unlink(missing_ok=True)
    _fsync_directory(root)


def _after_union_registry_replace(_root: Path) -> None:
    """Test-only crash injection seam after the safe first replace."""


def _append_union_audit_once(path: Path, audit: dict[str, object]) -> None:
    union_id = audit.get("union_id")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("union_id") == union_id:
                return
    _append_payload(path, audit)


def _write_payload_lines_atomic(path: Path, payloads: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for payload in payloads:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_payload_lines_atomic(path, [payload])


def _load_records(*paths: Path) -> tuple[VersionedIssueRecord, ...]:
    records: list[VersionedIssueRecord] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                value["stage"] = PipelineStage(value["stage"])
                value["developer_status"] = DeveloperStatus(value["developer_status"])
                record = VersionedIssueRecord(**value)
                IssueRegistry._validate_record(record)
                if record.issue_id in seen:
                    raise ValueError("duplicate issue id across durable family logs")
                seen.add(record.issue_id)
                records.append(record)
    return tuple(records)


def _append_record(path: Path, record: VersionedIssueRecord) -> None:
    payload = _record_payload(record)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _append_payload(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_record(path: Path, replacement: VersionedIssueRecord) -> None:
    if not path.is_file():
        raise ValueError("family owner registry record is missing")
    payloads: list[dict[str, object]] = []
    replaced = False
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            payload = json.loads(line)
            if payload.get("issue_id") == replacement.issue_id:
                if replaced:
                    raise ValueError("duplicate family owner registry record")
                payload = _record_payload(replacement)
                replaced = True
            payloads.append(payload)
    if not replaced:
        raise ValueError("family owner registry record is missing")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for payload in payloads:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _record_payload(record: VersionedIssueRecord) -> dict[str, object]:
    payload = asdict(record)
    payload["stage"] = record.stage.value
    payload["developer_status"] = record.developer_status.value
    return payload


def _record_hash(record: VersionedIssueRecord) -> str:
    encoded = json.dumps(_record_payload(record), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_provenance_matches(
    record: VersionedIssueRecord,
    provenance: Mapping[str, object],
) -> bool:
    return bool(
        record.original_validation_hash == provenance.get("original_validation_hash")
        and record.reduced_validation_hash == provenance.get("reduced_validation_hash")
        and stable_json_dumps(record.original_validation_ref)
        == stable_json_dumps(provenance.get("original_validation_ref"))
        and stable_json_dumps(record.reduced_validation_ref)
        == stable_json_dumps(provenance.get("reduced_validation_ref"))
        and record.validation_provenance_hash == provenance.get("validation_provenance_hash")
    )


def _owner_commit_is_current(root: Path, record: VersionedIssueRecord) -> bool:
    path = root / "commits" / f"{record.issue_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    return bool(
        isinstance(payload, dict)
        and payload.get("issue_id") == record.issue_id
        and all(name in payload for name in required)
        and payload.get("registry_record_hash") == _record_hash(record)
    )


def _write_commit(root: Path, record: VersionedIssueRecord, materialized: dict[str, object]) -> None:
    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    if set(materialized) != required or materialized.get("issue_id") != record.issue_id:
        raise ValueError("issue materialization receipt is incomplete")
    path = root / "commits" / f"{record.issue_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**materialized, "registry_record_hash": _record_hash(record)}
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "committed_owner_outcome",
    "durable_family_credentials_are_current",
    "durable_family_payload_is_current",
    "promote_unique_issue",
    "renew_current_family_owner",
]
