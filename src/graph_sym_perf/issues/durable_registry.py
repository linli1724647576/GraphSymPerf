"""Process-safe run-local duplicate registry for submit-ready promotion."""

from __future__ import annotations

import fcntl
import json
import os
import hashlib
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping

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
    issue_id = deterministic_issue_id(validation, reduction)
    fingerprint = _validated_fingerprint(validation, reduction, certificate, dbms=dbms)
    known = (known_regression_signature_from_manifest(known_regression_manifest),)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        records = _load_records(registry_path, review_path)
        index = IssueFamilyIndex(known_regressions=known)
        for existing in records:
            index.add(_record_fingerprint(existing), issue_id=existing.issue_id)
        existing_ids = {item.issue_id for item in records}
        decision = (
            index.reissue_owner(fingerprint, issue_id=issue_id)
            if any(
                item.issue_id == issue_id and item.stage is PipelineStage.SUBMIT_READY
                for item in records
            )
            else index.add(fingerprint, issue_id=issue_id)
        )
        if decision.known_regression_match:
            _append_payload(
                known_path,
                {
                    "case_id": decision.known_regression_case_id,
                    "decision_hash": decision.decision_hash,
                    "fingerprint_hash": fingerprint.fingerprint_hash,
                    "issue_id": issue_id,
                    "known_regression_match": True,
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
            stage=PipelineStage.REDUCED_VALID,
        )
        if not decision.is_new_family:
            if issue_id not in existing_ids:
                _append_record(review_path, record)
            status = (
                "exact_family_duplicate"
                if decision.match_kind is FamilyMatchKind.EXACT
                else "structural_family_duplicate"
            )
            return None, status
        family_records = [
            record if item.issue_id == issue_id else item
            for item in records
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
        if issue_id in existing_ids:
            _replace_record(registry_path, committed)
            status = "family_owner_repaired"
        else:
            _append_record(registry_path, committed)
            status = "unique_family"
        _write_commit(root_path, committed, materialized)
        return issue, status


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
            records = _load_records(
                root_path / "registry.jsonl",
                root_path / "dedup_review.jsonl",
            )
            index = IssueFamilyIndex()
            fingerprints: dict[str, IssueFingerprint] = {}
            for record in records:
                fingerprint = _record_fingerprint(record)
                fingerprints[record.issue_id] = fingerprint
                index.add(fingerprint, issue_id=record.issue_id)
            owner_fingerprint = fingerprints.get(decision.issue_id)
            if owner_fingerprint is None:
                return False
            current = index.reissue_owner(owner_fingerprint, issue_id=decision.issue_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return bool(
        current.verify()
        and current.is_new_family
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
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
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
        "family_id": current.family_id,
        "fingerprint": asdict(current.fingerprint),
        "match_kind": current.match_kind.value,
        "matched_issue_id": current.matched_issue_id,
        "known_regression_match": current.known_regression_match,
        "known_regression_case_id": current.known_regression_case_id,
        "is_new_family": current.is_new_family,
        "family_members": current.family_members,
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
    index = IssueFamilyIndex()
    fingerprints: dict[str, IssueFingerprint] = {}
    for record in records:
        fingerprint = _record_fingerprint(record)
        fingerprints[record.issue_id] = fingerprint
        index.add(fingerprint, issue_id=record.issue_id)
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


def _record(
    issue_id: str,
    validation: object,
    reduction: object,
    certificate: object,
    dbms: str,
    *,
    fingerprint: IssueFingerprint,
    family_decision: object,
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
        family_id=str(getattr(family_decision, "family_id") or ""),
        family_version_hash=str(getattr(family_decision, "family_version_hash")),
        family_decision_hash=str(getattr(family_decision, "decision_hash")),
        exact_signature=fingerprint.exact,
        structural_signature=fingerprint.structural,
        fingerprint_hash=fingerprint.fingerprint_hash,
        submission_path=fingerprint.submission_path,
        reduced_phenotype=fingerprint.reduced_validation_phenotype,
        known_regression_match=bool(getattr(family_decision, "known_regression_match")),
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
    "durable_family_credentials_are_current",
    "durable_family_payload_is_current",
    "promote_unique_issue",
]
