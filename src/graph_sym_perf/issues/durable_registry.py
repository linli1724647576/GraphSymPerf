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
    compact_family_decision_payload,
    compact_family_proof_is_valid,
    family_member_proof_root,
)
from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.issues.exporter import (
    canonical_typed_export_dumps,
    canonical_typed_export_projection,
)
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    PipelineStage,
    VersionedIssueRecord,
    canonical_content_hash,
    canonical_validation_report_hash,
    canonical_validation_provenance_hash,
    compact_cluster_members_root,
    compact_duplicate_clearance_payload,
)
from graph_sym_perf.validation.promotion import deterministic_issue_id
from graph_sym_perf.validation.promotion import promote


MAX_FAMILY_MEMBERS = 4096
MAX_MEMBER_RAW_QUERY_BYTES = 64 * 1024
MAX_COMPACT_FAMILY_PAYLOAD_BYTES = 16 * 1024
MAX_COMPACT_UNION_AUDIT_BYTES = 4 * 1024


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
        _recover_owner_renewal(root_path)
        _validate_known_diagnostics(known_path)
        _validate_family_overflow_diagnostics(root_path / "family_overflow.jsonl")
        records = _load_records(registry_path, review_path)
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
            index = _replay_family_index(records, known_regressions=known)
            index.reissue_owner(fingerprint, issue_id=issue_id)
            if _recover_owner_terminal_unlocked(root_path, existing_owner):
                return None, "owner_already_committed"
            return None, "owner_terminal_evidence_unavailable"
        known_match = next((signature for signature in known if signature.matches(fingerprint)), None)
        if known_match is not None:
            decision = IssueFamilyIndex(known_regressions=known).add(
                fingerprint,
                issue_id=issue_id,
            )
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
        raw_query_bytes = len(fingerprint.reduced_query_a.encode("utf-8")) + len(
            fingerprint.reduced_query_b.encode("utf-8")
        )
        matched_family_ids, prospective_count = _prospective_family_size(
            records,
            fingerprint,
            issue_id=issue_id,
        )
        budget_failure = next(
            (
                failure
                for failure in (
                    (
                        "member_raw_query_bytes",
                        MAX_MEMBER_RAW_QUERY_BYTES,
                        raw_query_bytes,
                    )
                    if raw_query_bytes > MAX_MEMBER_RAW_QUERY_BYTES
                    else None,
                    ("family_member_count", MAX_FAMILY_MEMBERS, prospective_count)
                    if prospective_count > MAX_FAMILY_MEMBERS
                    else None,
                )
                if failure is not None
            ),
            None,
        )
        if budget_failure is not None:
            reason, limit, observed = budget_failure
            _append_family_overflow_once(
                root_path / "family_overflow.jsonl",
                issue_id=issue_id,
                family_id=min(matched_family_ids) if matched_family_ids else None,
                fingerprint_hash=fingerprint.fingerprint_hash,
                reason=reason,
                limit=limit,
                observed=observed,
            )
            return None, "family_budget_exceeded"
        index = _replay_family_index(records, known_regressions=known)
        decision = index.add(fingerprint, issue_id=issue_id)
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
        if issue_id in existing_ids:
            record = next(item for item in records if item.issue_id == issue_id)
        prospective_records = _records_with_current_family(
            index,
            tuple(item for item in records if item.issue_id != issue_id) + (record,),
            family_id=str(decision.family_id),
        )
        prospective_owner_id = str(decision.owner_issue_id)
        prospective_owner_record = next(
            item for item in prospective_records if item.issue_id == prospective_owner_id
        )
        prospective_owner_decision = index.reissue_owner(
            _record_fingerprint(prospective_owner_record),
            issue_id=prospective_owner_id,
        )
        prospective_family_records = tuple(
            item
            for item in prospective_records
            if item.family_id == prospective_owner_decision.family_id
        )
        prospective_clearance = IssueRegistry().duplicate_clearance_for_family(
            prospective_owner_decision,
            prospective_family_records,
        )
        compact_projection_bytes = len(
            stable_json_dumps(
                {
                    "family_decision": compact_family_decision_payload(
                        prospective_owner_decision
                    ),
                    "duplicate_clearance": compact_duplicate_clearance_payload(
                        prospective_clearance
                    ),
                }
            ).encode("utf-8")
        )
        if compact_projection_bytes > MAX_COMPACT_FAMILY_PAYLOAD_BYTES:
            _append_family_overflow_once(
                root_path / "family_overflow.jsonl",
                issue_id=issue_id,
                family_id=str(prospective_owner_decision.family_id),
                fingerprint_hash=fingerprint.fingerprint_hash,
                reason="compact_family_payload_bytes",
                limit=MAX_COMPACT_FAMILY_PAYLOAD_BYTES,
                observed=compact_projection_bytes,
            )
            return None, "family_budget_exceeded"
        if not decision.is_new_family and decision.owner_issue_id != issue_id:
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
        _verify_owner_materialization_against_durable(
            root_path,
            committed,
            require_commit=False,
        )
        _write_commit(root_path, committed, materialized)
        _verify_owner_materialization_against_durable(
            root_path,
            committed,
            require_commit=True,
        )
        return issue, status


def committed_owner_outcome(root: str | Path, *, issue_id: str) -> dict[str, object]:
    """Return a verified terminal outcome without rerunning or rewriting evidence."""

    root_path = Path(root)
    with (root_path / ".registry.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_family_transaction(root_path)
        _recover_owner_renewal(root_path)
        records = _load_records(root_path / "registry.jsonl", root_path / "dedup_review.jsonl")
        owners = tuple(
            record
            for record in records
            if record.issue_id == issue_id and record.stage is PipelineStage.SUBMIT_READY
        )
        if len(owners) != 1 or not _recover_owner_terminal_unlocked(root_path, owners[0]):
            raise ValueError("committed family owner outcome is unavailable")
        record = owners[0]
        _receipt, issue_record, store, _export_path = _load_owner_evidence_unlocked(
            root_path,
            record,
            require_current_family=True,
            require_commit=True,
        )
        reduction_summary = json.loads(store.read(issue_record.payload["reduction_ref"]))
        if not isinstance(reduction_summary, dict):
            raise ValueError("committed family owner reduction summary is unavailable")
        index_path = root_path / "index" / f"{issue_id}.json"
        return {
            "issue_id": issue_id,
            "issue_index": str(index_path.relative_to(root_path.parent)),
            "original_validation_hash": record.original_validation_hash,
            "reduced_validation_hash": record.reduced_validation_hash,
            "original_validation_ref": dict(record.original_validation_ref or {}),
            "reduced_validation_ref": dict(record.reduced_validation_ref or {}),
            "reduction_summary": reduction_summary,
        }


def renew_current_family_owner(root: str | Path, *, owner_issue_id: str) -> bool:
    """Recover a stale union owner before currentness checks or CLI export."""

    root_path = Path(root)
    with (root_path / ".registry.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_family_transaction(root_path)
        _recover_owner_renewal(root_path)
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


def _recover_owner_terminal_unlocked(
    root: Path,
    record: VersionedIssueRecord,
) -> bool:
    """Complete registry->commit recovery only from the owner's sealed old evidence."""

    if _owner_commit_is_current(root, record):
        try:
            _load_owner_evidence_unlocked(
                root,
                record,
                require_current_family=True,
                require_commit=True,
            )
        except FileNotFoundError:
            return False
        return True
    commit_path = root / "commits" / f"{record.issue_id}.json"
    if commit_path.exists():
        return False
    try:
        receipt, _issue, _store, _export = _load_owner_evidence_unlocked(
            root,
            record,
            require_current_family=True,
            require_commit=False,
        )
    except FileNotFoundError:
        return False
    _write_commit(root, record, receipt)
    _verify_owner_materialization_against_durable(
        root,
        record,
        require_commit=True,
    )
    return True


def _load_owner_evidence_unlocked(
    root: Path,
    record: VersionedIssueRecord,
    *,
    require_current_family: bool,
    require_commit: bool,
    allow_historical_family: bool = False,
) -> tuple[dict[str, object], object, object, Path]:
    """Validate a durable receipt, sealed issue chain, export, and owner binding."""

    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    index_path = root / "index" / f"{record.issue_id}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or set(index) != required:
        raise ValueError("family owner index receipt is incomplete")
    receipt = {name: index[name] for name in required}
    if receipt.get("issue_id") != record.issue_id:
        raise ValueError("family owner index receipt has the wrong identity")
    commit_path = root / "commits" / f"{record.issue_id}.json"
    if require_commit or commit_path.exists():
        commit = json.loads(commit_path.read_text(encoding="utf-8"))
        if (
            not isinstance(commit, dict)
            or any(commit.get(name) != receipt.get(name) for name in required)
            or (
                require_commit
                and commit.get("registry_record_hash") != _record_hash(record)
            )
        ):
            raise ValueError("committed family owner receipt is inconsistent")
    issue_record, store, export_path = _verify_owner_receipt_unlocked(
        root,
        record,
        receipt,
        require_current_family=require_current_family,
        allow_historical_family=allow_historical_family,
    )
    return receipt, issue_record, store, export_path


def _verify_owner_receipt_unlocked(
    root: Path,
    record: VersionedIssueRecord,
    receipt: Mapping[str, object],
    *,
    require_current_family: bool,
    allow_historical_family: bool = False,
) -> tuple[object, object, Path]:
    """Verify sealed owner evidence independently of mutable index pointers."""

    from graph_sym_perf.artifacts.store import (
        ContentAddressedStore,
        load_artifact_record,
        verify_issue_hash_chain,
    )

    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    if set(receipt) != required or receipt.get("issue_id") != record.issue_id:
        raise ValueError("family owner receipt is incomplete")
    run_dir = root.parent.resolve()
    store_path = (run_dir / str(receipt["artifact_store"])).resolve()
    export_path = (run_dir / str(receipt["export_path"])).resolve()
    if run_dir not in store_path.parents or run_dir not in export_path.parents:
        raise ValueError("family owner receipt escapes the run directory")
    store = ContentAddressedStore(store_path, budget_bytes=(1 << 63) - 1)
    issue_record = load_artifact_record(store, receipt["issue_ref"])
    try:
        verify_issue_hash_chain(store, issue_record, run_dir=run_dir)
    except ValueError:
        if not allow_historical_family:
            raise
        family_payload = issue_record.payload.get("family_decision")
        clearance_payload = issue_record.payload.get("duplicate_clearance")
        if not isinstance(family_payload, Mapping) or not isinstance(
            clearance_payload, Mapping
        ):
            raise ValueError("historical family owner proof is unavailable")
        historical_proof = _historical_family_fingerprint_proof(
            root,
            family_payload,
            clearance_payload,
        )
        verify_issue_hash_chain(
            store,
            issue_record,
            family_fingerprints=historical_proof,
        )
    payload = issue_record.payload
    family = payload.get("family_decision")
    if (
        payload.get("issue_id") != record.issue_id
        or payload.get("original_validation_hash") != record.original_validation_hash
        or payload.get("reduced_validation_hash") != record.reduced_validation_hash
        or stable_json_dumps(payload.get("original_validation_ref"))
        != stable_json_dumps(record.original_validation_ref)
        or stable_json_dumps(payload.get("final_validation_ref"))
        != stable_json_dumps(record.reduced_validation_ref)
        or not isinstance(family, Mapping)
        or stable_json_dumps(family.get("fingerprint"))
        != stable_json_dumps(asdict(_record_fingerprint(record)))
    ):
        raise ValueError("sealed family owner evidence does not bind the registry record")
    if require_current_family and not _durable_family_payload_is_current_unlocked(root, payload):
        raise ValueError("committed family owner artifact is stale")
    if hashlib.sha256(export_path.read_bytes()).hexdigest() != receipt["export_sha256"]:
        raise ValueError("committed family owner export is stale")
    return issue_record, store, export_path


def _verify_owner_materialization_against_durable(
    root: Path,
    record: VersionedIssueRecord,
    *,
    require_commit: bool,
) -> None:
    """Require the materialized artifact to replay from durable family records."""

    _load_owner_evidence_unlocked(
        root,
        record,
        require_current_family=True,
        require_commit=require_commit,
    )


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
    try:
        receipt, old_issue, store, _old_export_path = _load_owner_evidence_unlocked(
            root,
            owner_record,
            require_current_family=False,
            require_commit=False,
            allow_historical_family=True,
        )
    except FileNotFoundError:
        return False
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
    family_payload = compact_family_decision_payload(current)
    clearance_payload = compact_duplicate_clearance_payload(clearance)
    old_payload.pop("issue_hash", None)
    renewed = ArtifactRecord.seal_issue(
        {
            **old_payload,
            "family_id": current.family_id,
            "family_decision": family_payload,
            "duplicate_clearance": clearance_payload,
        }
    )
    verify_issue_hash_chain(store, renewed, run_dir=root.parent)
    issue_ref = store.put_json(f"issue-{owner_issue_id}.json", renewed.to_dict())
    exported = canonical_typed_export_projection(renewed.payload)
    generation_name = (
        f"{owner_issue_id}.{current.family_version_hash}.{current.decision_hash}.json"
    )
    generation_export_path = root / "submit-ready" / generation_name
    _write_json_immutable(generation_export_path, exported)
    new_receipt = {
        "artifact_store": receipt["artifact_store"],
        "export_path": str(generation_export_path.relative_to(root.parent)),
        "export_sha256": hashlib.sha256(generation_export_path.read_bytes()).hexdigest(),
        "issue_id": owner_issue_id,
        "issue_ref": asdict(issue_ref),
    }
    old_commit = _load_owner_commit(root, owner_issue_id)
    if _commit_receipt(old_commit) != receipt:
        raise ValueError("family renewal old index and commit disagree")
    journal: dict[str, object] = {
        "schema": "owner-renewal-v1",
        "owner_issue_id": owner_issue_id,
        "owner_record_hash": _record_hash(owner_record),
        "old_commit_record_hash": old_commit["registry_record_hash"],
        "old_receipt": receipt,
        "new_receipt": new_receipt,
        "generation_export_sha256": new_receipt["export_sha256"],
        "generation_issue_content_hash": issue_ref.content_hash,
        "family_version_hash": current.family_version_hash,
        "family_decision_hash": current.decision_hash,
    }
    journal["journal_hash"] = _hash_payload("owner-renewal-journal-v1", journal)
    _write_json_atomic(root / "owner_renewal.pending.json", journal)
    _after_owner_renewal_generation_write(root)
    _apply_owner_renewal(root, journal)
    return True


def _after_owner_renewal_generation_write(_root: Path) -> None:
    """Test-only crash seam after sealed generations and the durable journal."""


def _after_owner_renewal_index_write(_root: Path) -> None:
    """Test-only crash seam after advancing the renewal index pointer."""


def _after_owner_renewal_commit_write(_root: Path) -> None:
    """Test-only crash seam after advancing the renewal commit pointer."""


def _recover_owner_renewal(root: Path) -> None:
    path = root / "owner_renewal.pending.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("owner renewal journal must be an object")
    journal_hash = payload.pop("journal_hash", None)
    if journal_hash != _hash_payload("owner-renewal-journal-v1", payload):
        raise ValueError("owner renewal journal hash is invalid")
    payload["journal_hash"] = journal_hash
    _apply_owner_renewal(root, payload)


def _apply_owner_renewal(root: Path, payload: Mapping[str, object]) -> None:
    required = {
        "schema",
        "owner_issue_id",
        "owner_record_hash",
        "old_commit_record_hash",
        "old_receipt",
        "new_receipt",
        "generation_export_sha256",
        "generation_issue_content_hash",
        "family_version_hash",
        "family_decision_hash",
        "journal_hash",
    }
    body = {name: value for name, value in payload.items() if name != "journal_hash"}
    if (
        set(payload) != required
        or payload.get("schema") != "owner-renewal-v1"
        or payload.get("journal_hash")
        != _hash_payload("owner-renewal-journal-v1", body)
    ):
        raise ValueError("owner renewal journal is incomplete")
    issue_id = str(payload["owner_issue_id"])
    records = _load_records(root / "registry.jsonl", root / "dedup_review.jsonl")
    owners = tuple(
        record
        for record in records
        if record.issue_id == issue_id and record.stage is PipelineStage.SUBMIT_READY
    )
    if len(owners) != 1 or payload["owner_record_hash"] != _record_hash(owners[0]):
        raise ValueError("owner renewal journal does not bind the current owner")
    owner = owners[0]
    old_receipt = _validate_receipt_payload(payload["old_receipt"], issue_id=issue_id)
    new_receipt = _validate_receipt_payload(payload["new_receipt"], issue_id=issue_id)
    if old_receipt == new_receipt:
        raise ValueError("owner renewal journal does not advance a generation")
    issue_record, _store, generation_export = _verify_owner_receipt_unlocked(
        root,
        owner,
        new_receipt,
        require_current_family=True,
    )
    family = issue_record.payload.get("family_decision")
    fingerprint = family.get("fingerprint") if isinstance(family, Mapping) else None
    exported = json.loads(generation_export.read_text(encoding="utf-8"))
    expected_generation_name = (
        f"{issue_id}.{payload['family_version_hash']}."
        f"{payload['family_decision_hash']}.json"
    )
    issue_ref = new_receipt["issue_ref"]
    if (
        not isinstance(family, Mapping)
        or not isinstance(fingerprint, Mapping)
        or not isinstance(exported, dict)
        or generation_export.parent != root.parent.resolve() / "issues" / "submit-ready"
        or generation_export.name != expected_generation_name
        or payload["generation_export_sha256"] != new_receipt["export_sha256"]
        or not isinstance(issue_ref, Mapping)
        or payload["generation_issue_content_hash"] != issue_ref.get("content_hash")
        or family.get("family_version_hash") != payload["family_version_hash"]
        or family.get("decision_hash") != payload["family_decision_hash"]
        or not _generation_export_binds_owner(exported, issue_record.payload)
    ):
        raise ValueError("owner renewal generation binding is invalid")
    index_path = root / "index" / f"{issue_id}.json"
    index = _load_owner_index(index_path, issue_id=issue_id)
    commit = _load_owner_commit(root, issue_id)
    commit_receipt = _commit_receipt(commit)
    old_commit_hash = payload["old_commit_record_hash"]
    current_hash = _record_hash(owner)
    if index == old_receipt:
        if commit_receipt != old_receipt or commit.get("registry_record_hash") != old_commit_hash:
            raise ValueError("owner renewal old pointer state is invalid")
        _write_json_atomic(index_path, new_receipt)
        _after_owner_renewal_index_write(root)
        index = new_receipt
    if index != new_receipt:
        raise ValueError("owner renewal index is outside the journal state")
    if commit_receipt == old_receipt:
        if commit.get("registry_record_hash") != old_commit_hash:
            raise ValueError("owner renewal old commit binding is invalid")
        _write_commit(root, owner, new_receipt)
        _after_owner_renewal_commit_write(root)
        commit = _load_owner_commit(root, issue_id)
        commit_receipt = _commit_receipt(commit)
    if commit_receipt != new_receipt or commit.get("registry_record_hash") != current_hash:
        raise ValueError("owner renewal commit is outside the journal state")
    if not _owner_commit_is_current(root, owner):
        raise ValueError("owner renewal pointers did not converge")
    _load_owner_evidence_unlocked(
        root,
        owner,
        require_current_family=True,
        require_commit=True,
    )
    (root / "owner_renewal.pending.json").unlink()
    _fsync_directory(root)


def _generation_export_binds_owner(
    exported: Mapping[str, object],
    issue_payload: Mapping[str, object],
) -> bool:
    try:
        expected = canonical_typed_export_projection(issue_payload)
    except (TypeError, ValueError):
        return False
    try:
        return canonical_typed_export_dumps(exported) == canonical_typed_export_dumps(
            expected
        )
    except (TypeError, ValueError):
        return False


def _validate_receipt_payload(value: object, *, issue_id: str) -> dict[str, object]:
    required = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("issue_id") != issue_id
    ):
        raise ValueError("owner renewal receipt is incomplete")
    return dict(value)


def _load_owner_index(path: Path, *, issue_id: str) -> dict[str, object]:
    return _validate_receipt_payload(
        json.loads(path.read_text(encoding="utf-8")),
        issue_id=issue_id,
    )


def _load_owner_commit(root: Path, issue_id: str) -> dict[str, object]:
    payload = json.loads(
        (root / "commits" / f"{issue_id}.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict) or set(payload) != {
        "artifact_store",
        "export_path",
        "export_sha256",
        "issue_id",
        "issue_ref",
        "registry_record_hash",
    }:
        raise ValueError("owner commit is incomplete")
    return payload


def _commit_receipt(commit: Mapping[str, object]) -> dict[str, object]:
    return {
        name: commit[name]
        for name in (
            "artifact_store",
            "export_path",
            "export_sha256",
            "issue_id",
            "issue_ref",
        )
    }


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
            _recover_owner_renewal(root_path)
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
            _recover_owner_renewal(root_path)
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
    expected_family = compact_family_decision_payload(current)
    expected_clearance = compact_duplicate_clearance_payload(current_clearance)
    legacy_family = {
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
    legacy_clearance = {
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
    supplied_family = stable_json_dumps(dict(family))
    supplied_clearance = stable_json_dumps(dict(clearance))
    return bool(
        issue_payload.get("family_id") == current.family_id
        and (
            supplied_family == stable_json_dumps(expected_family)
            and supplied_clearance == stable_json_dumps(expected_clearance)
            or supplied_family == stable_json_dumps(legacy_family)
            and supplied_clearance == stable_json_dumps(legacy_clearance)
        )
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


def verify_durable_family_artifact_proof(
    root: str | Path,
    family_payload: Mapping[str, object],
    clearance_payload: Mapping[str, object],
) -> None:
    """Resolve compact refs and recompute every member from durable raw queries."""

    root_path = Path(root)
    run_dir = root_path.parent
    proof_ref = family_payload.get("registry_proof_ref")
    if not isinstance(proof_ref, Mapping) or proof_ref.get("schema") != "durable-family-proof-v1":
        raise ValueError("durable family proof reference is invalid")
    registry_path = _resolve_family_proof_ref(
        run_dir,
        proof_ref.get("registry_rel"),
        expected=root_path / "registry.jsonl",
    )
    review_path = _resolve_family_proof_ref(
        run_dir,
        proof_ref.get("review_rel"),
        expected=root_path / "dedup_review.jsonl",
    )
    records = _load_records(registry_path, review_path)
    index = _replay_family_index(records)
    owner_id = str(family_payload.get("owner_issue_id", ""))
    owner_record = next(
        (
            record
            for record in records
            if record.issue_id == owner_id and record.stage is PipelineStage.SUBMIT_READY
        ),
        None,
    )
    if owner_record is None:
        raise ValueError("durable family proof owner is missing")
    current = index.reissue_owner(_record_fingerprint(owner_record), issue_id=owner_id)
    family_records = tuple(
        record for record in records if record.family_id == current.family_id
    )
    proof = tuple(
        sorted(
            (record.issue_id, _record_fingerprint(record))
            for record in family_records
        )
    )
    current_clearance = IssueRegistry().duplicate_clearance_for_family(
        current,
        family_records,
    )
    if (
        not compact_family_proof_is_valid(family_payload, proof)
        or stable_json_dumps(dict(family_payload))
        != stable_json_dumps(compact_family_decision_payload(current))
        or stable_json_dumps(dict(clearance_payload))
        != stable_json_dumps(compact_duplicate_clearance_payload(current_clearance))
    ):
        raise ValueError("durable family proof does not match the compact artifact")


def _historical_family_fingerprint_proof(
    root: Path,
    family_payload: Mapping[str, object],
    clearance_payload: Mapping[str, object],
) -> tuple[tuple[str, IssueFingerprint], ...]:
    """Rebuild a stale owner proof only for owner renewal/recovery.

    Currentness and export verification never call this resolver. It starts from
    the current raw durable records, reverses the content-bound union audit, and
    rebuilds each historical index without trusting stored family IDs or stages.
    """

    owner_id = str(family_payload.get("owner_issue_id", ""))
    if not owner_id:
        raise ValueError("historical family proof has no owner")
    current_records = _load_records(root / "registry.jsonl", root / "dedup_review.jsonl")
    working = {record.issue_id: record for record in current_records}
    audits = _load_union_audits(root / "family_unions.jsonl")
    if not audits:
        raise ValueError("historical family proof audit is unavailable")
    for audit in reversed(audits):
        added = tuple(str(value) for value in audit["added_issue_ids"])
        if any(issue_id not in working for issue_id in added):
            raise ValueError("historical family audit additions do not replay")
        after_records = tuple(working.values())
        after_states = _canonical_family_component_states(after_records)
        affected_after = [
            state for state in after_states if set(added).intersection(state[1])
        ]
        if (
            len(affected_after) != 1
            or not set(added) <= affected_after[0][1]
        ):
            raise ValueError("historical family audit additions span components")
        actual_after = [affected_after[0][0]]
        if stable_json_dumps(actual_after) != stable_json_dumps(
            audit["after_components"]
        ):
            raise ValueError("historical family audit after snapshot does not replay")
        after_ids = set(affected_after[0][1])
        unrelated_after = [
            snapshot for snapshot, members in after_states if members != after_ids
        ]
        for issue_id in added:
            del working[issue_id]
        before_records = tuple(working.values())
        expected_before_ids = after_ids - set(added)
        before_states = _canonical_family_component_states(before_records)
        affected_before = [
            state for state in before_states if expected_before_ids.intersection(state[1])
        ]
        actual_before_ids = (
            set().union(*(members for _snapshot, members in affected_before))
            if affected_before
            else set()
        )
        if actual_before_ids != expected_before_ids:
            raise ValueError("historical family audit before component is not closed")
        actual_before = [snapshot for snapshot, _members in affected_before]
        if stable_json_dumps(actual_before) != stable_json_dumps(
            audit["before_components"]
        ):
            raise ValueError("historical family audit before snapshot does not replay")
        unrelated_before = [
            snapshot
            for snapshot, members in before_states
            if not expected_before_ids.intersection(members)
        ]
        if stable_json_dumps(unrelated_after) != stable_json_dumps(
            unrelated_before
        ):
            raise ValueError("historical family audit changed an unrelated component")
        if owner_id not in working:
            continue
        proof = _rebuild_historical_owner_snapshot(
            tuple(working.values()),
            owner_id=owner_id,
            family_payload=family_payload,
            clearance_payload=clearance_payload,
        )
        if proof is not None:
            return proof
    raise ValueError("historical family proof does not replay from durable records")


def _canonical_family_component_states(
    records: tuple[VersionedIssueRecord, ...],
) -> tuple[tuple[dict[str, object], set[str]], ...]:
    """Rebuild compact family components solely from raw member fingerprints."""

    if not records:
        return ()
    index = IssueFamilyIndex()
    fingerprints: dict[str, IssueFingerprint] = {}
    for record in sorted(records, key=lambda item: item.issue_id):
        fingerprint = _record_fingerprint(record)
        fingerprints[record.issue_id] = fingerprint
        index.add(fingerprint, issue_id=record.issue_id)
    grouped: dict[str, list[str]] = {}
    owner_decisions: dict[str, object] = {}
    for issue_id, fingerprint in sorted(fingerprints.items()):
        try:
            decision = index.reissue_owner(fingerprint, issue_id=issue_id)
            owner_decisions[str(decision.family_id)] = decision
        except ValueError:
            decision = index.reissue_member(fingerprint, issue_id=issue_id)
        grouped.setdefault(str(decision.family_id), []).append(issue_id)
    snapshots: list[tuple[dict[str, object], set[str]]] = []
    for family_id, members_value in sorted(grouped.items()):
        members = sorted(members_value)
        decision = owner_decisions.get(family_id)
        if decision is None:
            raise ValueError("canonical family component has no deterministic owner")
        proof_root = family_member_proof_root(
            (member, fingerprints[member]) for member in members
        )
        snapshots.append(
            (
                {
                    "family_id": family_id,
                    "member_count": len(members),
                    "members_root": _hash_payload(
                        "durable-family-audit-members-v1", members
                    ),
                    "member_proof_root": proof_root,
                    "owner_issue_id": str(decision.owner_issue_id),
                    "family_version_hash": str(decision.family_version_hash),
                    "family_decision_hash": str(decision.decision_hash),
                    "version_decision_root": _hash_payload(
                        "durable-family-audit-version-decision-v1",
                        {
                            "family_version_hash": str(
                                decision.family_version_hash
                            ),
                            "family_decision_hash": str(decision.decision_hash),
                        },
                    ),
                },
                set(members),
            )
        )
    return tuple(snapshots)


def _canonical_component_snapshots(
    records: tuple[VersionedIssueRecord, ...],
    affected_ids: set[str],
) -> list[dict[str, object]]:
    components = _canonical_family_component_states(records)
    selected = [
        state for state in components if affected_ids.intersection(state[1])
    ]
    selected_ids = (
        set().union(*(members for _snapshot, members in selected))
        if selected
        else set()
    )
    if selected_ids != affected_ids:
        raise ValueError("family audit affected component is not closed")
    return [snapshot for snapshot, _members in selected]


def _rebuild_historical_owner_snapshot(
    records: tuple[VersionedIssueRecord, ...],
    *,
    owner_id: str,
    family_payload: Mapping[str, object],
    clearance_payload: Mapping[str, object],
) -> tuple[tuple[str, IssueFingerprint], ...] | None:
    index = IssueFamilyIndex()
    fingerprints: dict[str, IssueFingerprint] = {}
    for record in sorted(records, key=lambda item: item.issue_id):
        fingerprint = _record_fingerprint(record)
        fingerprints[record.issue_id] = fingerprint
        index.add(fingerprint, issue_id=record.issue_id)
    owner_fingerprint = fingerprints.get(owner_id)
    if owner_fingerprint is None:
        return None
    try:
        current = index.reissue_owner(owner_fingerprint, issue_id=owner_id)
    except ValueError:
        return None
    member_ids = tuple(member for member, _exact in current.family_members)
    records_by_id = {record.issue_id: record for record in records}
    family_records = tuple(
        replace(records_by_id[member], family_id=str(current.family_id))
        for member in member_ids
    )
    clearance = IssueRegistry().duplicate_clearance_for_family(current, family_records)
    proof = tuple((member, fingerprints[member]) for member in member_ids)
    compact_match = (
        compact_family_proof_is_valid(family_payload, proof)
        and stable_json_dumps(dict(family_payload))
        == stable_json_dumps(compact_family_decision_payload(current))
        and stable_json_dumps(dict(clearance_payload))
        == stable_json_dumps(compact_duplicate_clearance_payload(clearance))
    )
    legacy_family = {
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
    legacy_clearance = {
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
    legacy_match = (
        stable_json_dumps(dict(family_payload)) == stable_json_dumps(legacy_family)
        and stable_json_dumps(dict(clearance_payload))
        == stable_json_dumps(legacy_clearance)
    )
    if compact_match or legacy_match:
        return proof
    return None


def _resolve_family_proof_ref(
    run_dir: Path,
    relative_value: object,
    *,
    expected: Path,
) -> Path:
    if not isinstance(relative_value, str):
        raise ValueError("durable family proof path is unavailable")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("durable family proof path escapes the run directory")
    run_resolved = run_dir.resolve()
    candidate = run_dir / relative
    cursor = run_dir
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("durable family proof path cannot traverse a symlink")
    resolved = candidate.resolve()
    if (
        resolved != expected.resolve()
        or resolved == run_resolved
        or run_resolved not in resolved.parents
    ):
        raise ValueError("durable family proof path is outside the expected registry")
    return resolved


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


def _prospective_family_size(
    records: tuple[VersionedIssueRecord, ...],
    fingerprint: IssueFingerprint,
    *,
    issue_id: str,
) -> tuple[set[str], int]:
    """Read-only budget preflight before constructing or mutating an index."""

    matched_family_ids: set[str] = set()
    for record in records:
        existing = _record_fingerprint(record)
        if existing.exact == fingerprint.exact or (
            fingerprint.structural is not None
            and existing.structural == fingerprint.structural
            and existing.submission_path == fingerprint.submission_path
            and existing.reduced_validation_phenotype
            == fingerprint.reduced_validation_phenotype
        ):
            matched_family_ids.add(record.family_id)
    existing_members = {
        record.issue_id
        for record in records
        if record.family_id in matched_family_ids
    }
    prospective_count = len(existing_members | {issue_id})
    return matched_family_ids, prospective_count


def _validate_family_overflow_diagnostics(path: Path) -> None:
    if not path.is_file():
        return
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("family overflow diagnostic must be an object")
        diagnostic_hash = value.pop("diagnostic_hash", None)
        expected = _hash_payload("family-overflow-diagnostic-v1", value)
        if diagnostic_hash != expected or diagnostic_hash in seen:
            raise ValueError("family overflow diagnostic hash is invalid or duplicated")
        seen.add(str(diagnostic_hash))


def _append_family_overflow_once(
    path: Path,
    *,
    issue_id: str,
    family_id: str | None,
    fingerprint_hash: str,
    reason: str,
    limit: int,
    observed: int,
) -> None:
    body: dict[str, object] = {
        "family_id": family_id,
        "fingerprint_hash": fingerprint_hash,
        "issue_id": issue_id,
        "limit": limit,
        "observed": observed,
        "reason": reason,
    }
    diagnostic_hash = _hash_payload("family-overflow-diagnostic-v1", body)
    existing: list[object] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if value.get("diagnostic_hash") == diagnostic_hash:
                return
            existing.append(value)
    _write_payload_lines_atomic(
        path,
        [*existing, {**body, "diagnostic_hash": diagnostic_hash}],
    )


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
    added_issue_ids = sorted(
        {record.issue_id for record in after_component}
        - {record.issue_id for record in before_component}
    )
    removed_issue_ids = sorted(
        {record.issue_id for record in before_component}
        - {record.issue_id for record in after_component}
    )
    if removed_issue_ids:
        raise ValueError("formal family transactions cannot remove durable members")
    before_components = _canonical_component_snapshots(
        previous_records,
        {record.issue_id for record in before_component},
    )
    after_components = _canonical_component_snapshots(
        records,
        {record.issue_id for record in after_component},
    )
    previous_audits = _load_union_audits(root / "family_unions.jsonl")
    audit = _build_union_audit(
        previous_union_id=(
            str(previous_audits[-1]["union_id"]) if previous_audits else None
        ),
        added_issue_ids=added_issue_ids,
        before_components=before_components,
        after_components=after_components,
    )
    if len(_serialized_payload_line(audit)) > MAX_COMPACT_UNION_AUDIT_BYTES:
        raise ValueError("compact family union audit exceeds its durable budget")
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


def _build_union_audit(
    *,
    previous_union_id: str | None,
    added_issue_ids: list[str],
    before_components: list[dict[str, object]],
    after_components: list[dict[str, object]],
) -> dict[str, object]:
    audit: dict[str, object] = {
        "previous_union_id": previous_union_id,
        "added_issue_ids": added_issue_ids,
        "before_components": before_components,
        "after_components": after_components,
    }
    audit["union_id"] = _hash_payload("durable-family-union-v1", audit)
    return audit


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
    audits = _load_union_audits(path)
    if any(value.get("union_id") == union_id for value in audits):
        return
    expected_previous = audits[-1]["union_id"] if audits else None
    body = {name: value for name, value in audit.items() if name != "union_id"}
    added = audit.get("added_issue_ids")
    before_components = audit.get("before_components")
    after_components = audit.get("after_components")
    if (
        set(audit)
        != {
            "previous_union_id",
            "added_issue_ids",
            "before_components",
            "after_components",
            "union_id",
        }
        or audit.get("previous_union_id") != expected_previous
        or union_id != _hash_payload("durable-family-union-v1", body)
        or not isinstance(added, list)
        or not added
        or added != sorted(set(added))
        or any(not isinstance(issue_id, str) or not issue_id for issue_id in added)
        or not isinstance(before_components, list)
        or not isinstance(after_components, list)
        or len(_serialized_payload_line(audit)) > MAX_COMPACT_UNION_AUDIT_BYTES
    ):
        raise ValueError("family union audit chain is discontinuous")
    _validate_audit_components(before_components)
    _validate_audit_components(after_components)
    _append_payload(path, audit)


def _load_union_audits(path: Path) -> tuple[dict[str, object], ...]:
    if not path.is_file():
        return ()
    audits: list[dict[str, object]] = []
    seen: set[str] = set()
    previous: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("family union audit must be an object")
        union_id = value.get("union_id")
        body = {name: item for name, item in value.items() if name != "union_id"}
        added = value.get("added_issue_ids")
        before_components = value.get("before_components")
        after_components = value.get("after_components")
        if (
            set(value)
            != {
                "previous_union_id",
                "added_issue_ids",
                "before_components",
                "after_components",
                "union_id",
            }
            or not isinstance(union_id, str)
            or union_id in seen
            or union_id != _hash_payload("durable-family-union-v1", body)
            or value.get("previous_union_id") != previous
            or not isinstance(added, list)
            or not added
            or any(not isinstance(issue_id, str) or not issue_id for issue_id in added)
            or added != sorted(set(added))
            or not isinstance(before_components, list)
            or not isinstance(after_components, list)
            or len(line.encode("utf-8")) + 1 > MAX_COMPACT_UNION_AUDIT_BYTES
        ):
            raise ValueError("family union audit chain is invalid")
        _validate_audit_components(before_components)
        _validate_audit_components(after_components)
        seen.add(union_id)
        previous = union_id
        audits.append(value)
    return tuple(audits)


def _validate_audit_components(components: list[object]) -> None:
    required = {
        "family_id",
        "member_count",
        "members_root",
        "member_proof_root",
        "owner_issue_id",
        "family_version_hash",
        "family_decision_hash",
        "version_decision_root",
    }
    family_ids: set[str] = set()
    for component in components:
        if not isinstance(component, Mapping) or set(component) != required:
            raise ValueError("family union audit component is incomplete")
        if (
            not isinstance(component["family_id"], str)
            or not component["family_id"]
            or component["family_id"] in family_ids
            or not isinstance(component["member_count"], int)
            or component["member_count"] < 1
            or any(
                not isinstance(component[name], str)
                or len(component[name]) != 64
                for name in (
                    "members_root",
                    "member_proof_root",
                    "family_version_hash",
                    "family_decision_hash",
                    "version_decision_root",
                )
            )
            or not isinstance(component["owner_issue_id"], str)
            or not component["owner_issue_id"]
            or component["version_decision_root"]
            != _hash_payload(
                "durable-family-audit-version-decision-v1",
                {
                    "family_version_hash": component["family_version_hash"],
                    "family_decision_hash": component["family_decision_hash"],
                },
            )
        ):
            raise ValueError("family union audit component hash is invalid")
        family_ids.add(str(component["family_id"]))


def _write_payload_lines_atomic(path: Path, payloads: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for payload in payloads:
                stream.write(_serialized_payload_line(payload).decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_payload_lines_atomic(path, [payload])


def _serialized_payload_line(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_json_immutable(path: Path, payload: Mapping[str, object]) -> None:
    """Create one canonical generation without replacing an existing inode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_typed_export_dumps(payload) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ValueError("immutable owner renewal generation already differs")
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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


def _owner_commit_is_current(root: Path, record: VersionedIssueRecord) -> bool:
    try:
        commit = _load_owner_commit(root, record.issue_id)
        index = _load_owner_index(
            root / "index" / f"{record.issue_id}.json",
            issue_id=record.issue_id,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    return bool(
        _commit_receipt(commit) == index
        and commit.get("registry_record_hash") == _record_hash(record)
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
    "MAX_COMPACT_FAMILY_PAYLOAD_BYTES",
    "MAX_COMPACT_UNION_AUDIT_BYTES",
    "MAX_FAMILY_MEMBERS",
    "MAX_MEMBER_RAW_QUERY_BYTES",
    "committed_owner_outcome",
    "durable_family_credentials_are_current",
    "durable_family_payload_is_current",
    "promote_unique_issue",
    "renew_current_family_owner",
    "verify_durable_family_artifact_proof",
]
