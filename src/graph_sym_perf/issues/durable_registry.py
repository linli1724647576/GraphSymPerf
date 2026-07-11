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
from collections.abc import Callable

from graph_sym_perf.cypher.parser import alpha_normalize_query
from graph_sym_perf.issues.registry import IssueRegistry, canonical_reduced_pair_hash
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
    materialize: Callable[[object], dict[str, object]] | None = None,
) -> tuple[object | None, str]:
    """Promote and atomically reserve a unique issue, or return a duplicate reason."""

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    lock_path = root_path / ".registry.lock"
    registry_path = root_path / "registry.jsonl"
    review_path = root_path / "dedup_review.jsonl"
    revocation_path = root_path / "revocations.jsonl"
    issue_id = deterministic_issue_id(validation, reduction)
    record = _record(issue_id, validation, reduction, certificate, dbms, stage=PipelineStage.REDUCED_VALID)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        registry = _load_registry(registry_path, review_path)
        exact = registry.exact_duplicate(record)
        if exact is not None:
            if exact.issue_id != issue_id:
                return None, "exact_duplicate"
            clearance = registry.duplicate_clearance(issue_id)
            if clearance is None:
                return None, "exact_duplicate_in_unresolved_cluster"
            issue = promote(
                validation,
                reduction_report=reduction,
                certificate=certificate,
                duplicate_clearance=clearance,
            )
            if issue is None:
                return None, "promotion_gate_failed"
            materialized = materialize(issue) if materialize is not None else {}
            _write_commit(root_path, exact, materialized)
            return issue, "exact_duplicate_repaired"
        registry.record(record)
        clearance = registry.duplicate_clearance(issue_id)
        if clearance is None:
            _append_record(review_path, record)
            for member in registry.possible_duplicate_ids(issue_id):
                _append_revocation(
                    revocation_path,
                    issue_id=member,
                    conflicting_issue_id=issue_id,
                )
            return None, "possible_duplicate_requires_manual_resolution"
        issue = promote(
            validation,
            reduction_report=reduction,
            certificate=certificate,
            duplicate_clearance=clearance,
        )
        if issue is None:
            return None, "promotion_gate_failed"
        materialized = materialize(issue) if materialize is not None else {}
        _append_record(
            registry_path,
            committed := VersionedIssueRecord(**{**record.__dict__, "stage": PipelineStage.SUBMIT_READY}),
        )
        _write_commit(root_path, committed, materialized)
        return issue, "unique"


def _record(
    issue_id: str,
    validation: object,
    reduction: object,
    certificate: object,
    dbms: str,
    *,
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
        pair_hash=canonical_reduced_pair_hash(query_a, query_b),
        direction=getattr(reduction, "final_evidence").slow_query,
        rule_id=rule,
        detector_version="runtime-first-v3",
        validator_version=getattr(validation, "validation_protocol"),
        reducer_version="paired-ast-ddmin-v1",
        rule_version=getattr(certificate, "proof_version"),
        discovered_at=now,
        site_class=getattr(certificate, "site_class"),
        normalized_pattern=alpha_normalize_query(query_a),
        normalized_predicate=alpha_normalize_query(query_b),
        reduced_query_a=query_a,
        reduced_query_b=query_b,
    )


def _load_registry(*paths: Path) -> IssueRegistry:
    registry = IssueRegistry()
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                value["stage"] = PipelineStage(value["stage"])
                value["developer_status"] = DeveloperStatus(value["developer_status"])
                registry.record(VersionedIssueRecord(**value))
    return registry


def _append_record(path: Path, record: VersionedIssueRecord) -> None:
    payload = _record_payload(record)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _append_revocation(path: Path, *, issue_id: str, conflicting_issue_id: str) -> None:
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        payload = {
            "conflicting_issue_id": conflicting_issue_id,
            "issue_id": issue_id,
            "reason": "possible_duplicate_requires_manual_resolution",
        }
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


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


__all__ = ["promote_unique_issue"]
