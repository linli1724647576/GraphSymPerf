"""Public issue-workflow command boundary.

The serializer for typed validation and reduction objects is deliberately not
implemented as permissive raw-JSON loading.  Until a sealed typed transport is
available, all artifact inputs fail closed at this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import fcntl
from collections.abc import Mapping
from pathlib import Path

from graph_sym_perf.artifacts.store import (
    ContentAddressedStore,
    load_artifact_record,
    verify_candidate_outcome_validation_chain,
    verify_candidate_record_reference,
    verify_issue_hash_chain,
)
from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord


class IssueGateError(ValueError):
    def __init__(self, *missing_gates: str) -> None:
        self.missing_gates = tuple(missing_gates)
        super().__init__("; ".join(self.missing_gates))


class IssueCommands:
    def validate(self, *, candidate: str | Path) -> dict[str, object]:
        outcome, _store = _verified_candidate_outcome(candidate)
        validation = outcome.get("validation")
        if not isinstance(validation, dict) or validation.get("stage") not in {
            "validation_passed",
            "rejected",
            "inconclusive",
            "invalid",
        }:
            raise IssueGateError("terminal independent validation evidence unavailable")
        return validation

    def reduce(self, *, candidate: str | Path) -> dict[str, object]:
        outcome, _store = _verified_candidate_outcome(candidate)
        validation = outcome.get("validation")
        reduction = outcome.get("reduction")
        if not isinstance(validation, dict) or not isinstance(reduction, dict):
            raise IssueGateError("validation/reduction terminal evidence unavailable")
        status = reduction.get("status")
        if status not in {
            "not_applicable",
            "reduced_valid",
            "reduction_partial",
            "reduction_invalidated",
        }:
            raise IssueGateError("typed reducer terminal status unavailable")
        if validation.get("stage") != "validation_passed" and status != "not_applicable":
            raise IssueGateError("reducer evidence exists without VALIDATION_PASSED")
        return reduction

    def export(self, *, issue: str | Path, output: str | Path) -> Path:
        index_path = Path(issue)
        if not index_path.exists():
            raise IssueGateError("SubmitReadyIssue payload does not exist")
        registry_lock = None
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            required = {
                "artifact_store",
                "export_path",
                "export_sha256",
                "issue_id",
                "issue_ref",
            }
            if not isinstance(index, dict) or not required <= set(index):
                raise ValueError("not a submit-ready index")
            if index_path.parent.name != "index" or index_path.parent.parent.name != "issues":
                raise ValueError("index path is outside the run issue directory")
            run_dir = index_path.parent.parent.parent.resolve()
            registry_lock = (run_dir / "issues" / ".registry.lock").open("a+", encoding="utf-8")
            fcntl.flock(registry_lock.fileno(), fcntl.LOCK_SH)
            if _revoked(run_dir / "issues" / "revocations.jsonl", str(index["issue_id"])):
                raise ValueError("submit-ready issue was revoked by a later possible-duplicate cluster")
            committed_record = _verify_registry_commit(run_dir / "issues", index)
            store_path = _within(run_dir, str(index["artifact_store"]))
            export_path = _within(run_dir, str(index["export_path"]))
            if (
                store_path.name != "artifacts"
                or store_path.parent.parent.name != "shards"
                or export_path.parent != run_dir / "issues" / "submit-ready"
            ):
                raise ValueError("issue index does not use the canonical run layout")
            store = ContentAddressedStore(store_path, budget_bytes=(1 << 63) - 1)
            record = load_artifact_record(store, index["issue_ref"])
            if getattr(record.level, "value", record.level) != "issue":
                raise ValueError("referenced record is not an issue")
            if record.payload.get("issue_id") != index["issue_id"]:
                raise ValueError("issue index identity does not match the sealed issue")
            _verify_issue_registry_binding(record.payload, committed_record)
            clearance = record.payload.get("duplicate_clearance")
            formal_family = isinstance(clearance, Mapping) and bool(clearance.get("family_id"))
            if not formal_family and _has_current_possible_duplicate(
                run_dir / "issues", str(index["issue_id"])
            ):
                raise ValueError("submit-ready issue is in the current possible-duplicate cluster")
            _verify_current_duplicate_clearance(run_dir / "issues", record.payload)
            verify_issue_hash_chain(store, record)
            payload = export_path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != index["export_sha256"]:
                raise ValueError("typed export hash mismatch")
            exported = json.loads(payload)
            family = record.payload.get("family_decision")
            fingerprint = family.get("fingerprint") if isinstance(family, Mapping) else None
            if (
                not isinstance(exported, dict)
                or not isinstance(family, Mapping)
                or not isinstance(fingerprint, Mapping)
                or exported.get("issue_id") != index["issue_id"]
                or exported.get("stage") != "submit_ready"
                or exported.get("reduction_status") != "reduced_valid"
                or exported.get("reduction_status") != record.payload.get("reduction_status")
                or exported.get("rule_id") != record.payload.get("certificate", {}).get("rule_id")
                or exported.get("validated_ratio") != record.payload.get("validated_ratio")
                or exported.get("dbms_version") != record.payload.get("dbms_version")
                or exported.get("reduced_original") != record.payload.get("reduced_query_a")
                or exported.get("reduced_transformed") != record.payload.get("reduced_query_b")
                or exported.get("family_id") != record.payload.get("family_id")
                or exported.get("exact_signature") != fingerprint.get("exact")
                or exported.get("structural_signature") != fingerprint.get("structural")
                or exported.get("submission_path") != fingerprint.get("submission_path")
                or exported.get("reduced_validation_phenotype")
                != fingerprint.get("reduced_validation_phenotype")
                or exported.get("family_version_hash") != family.get("family_version_hash")
                or exported.get("family_decision_hash") != family.get("decision_hash")
                or exported.get("original_validation_hash")
                != record.payload.get("original_validation_hash")
                or exported.get("reduced_validation_hash")
                != record.payload.get("reduced_validation_hash")
            ):
                raise ValueError("typed export identity does not match the issue index")
            destination = Path(output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(export_path, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IssueGateError(
                "raw JSON cannot prove a typed SubmitReadyIssue",
                f"verified issue hash chain unavailable: {type(exc).__name__}",
            ) from exc
        finally:
            if registry_lock is not None:
                fcntl.flock(registry_lock.fileno(), fcntl.LOCK_UN)
                registry_lock.close()


def _within(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("issue index path escapes run directory")
    return candidate


def _verified_candidate_outcome(candidate: str | Path) -> tuple[dict[str, object], ContentAddressedStore]:
    path = Path(candidate)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("candidate_ref"), dict):
            raise ValueError("candidate outcome index is incomplete")
        if path.parent.name != "candidate-outcomes" or path.parent.parent.name in {"", "shards"}:
            raise ValueError("candidate outcome is outside a shard")
        shard_dir = path.parent.parent
        store = ContentAddressedStore(shard_dir / "artifacts", budget_bytes=(1 << 63) - 1)
        candidate_record = load_artifact_record(store, value["candidate_ref"])
        candidate_hash = value.get("candidate_hash")
        if candidate_record.payload.get("candidate_hash") != candidate_hash:
            raise ValueError("candidate outcome breaks candidate hash binding")
        verify_candidate_record_reference(
            store,
            {**candidate_record.to_dict(), "record_ref": value["candidate_ref"]},
        )
        verify_candidate_outcome_validation_chain(store, value)
        return value, store
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IssueGateError(
            "typed CandidateBundle evidence unavailable",
            f"candidate hash-chain verification failed: {type(exc).__name__}",
        ) from exc


def _revoked(path: Path, issue_id: str) -> bool:
    if not path.is_file():
        return False
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("issue_id") == issue_id:
                return True
    return False


def _verify_registry_commit(root: Path, index: dict[str, object]) -> dict[str, object]:
    issue_id = str(index["issue_id"])
    commit_path = root / "commits" / f"{issue_id}.json"
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    if not isinstance(commit, dict):
        raise ValueError("issue registry commit must be an object")
    receipt_fields = {"artifact_store", "export_path", "export_sha256", "issue_id", "issue_ref"}
    if any(commit.get(name) != index.get(name) for name in receipt_fields):
        raise ValueError("issue index is not bound to its committed materialization receipt")
    committed_record = None
    with (root / "registry.jsonl").open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("issue_id") == issue_id:
                if committed_record is not None:
                    raise ValueError("issue registry contains duplicate committed identities")
                committed_record = value
    if committed_record is None or committed_record.get("stage") != "submit_ready":
        raise ValueError("issue has no committed SUBMIT_READY registry record")
    record_hash = hashlib.sha256(
        json.dumps(committed_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if commit.get("registry_record_hash") != record_hash:
        raise ValueError("issue registry commit hash mismatch")
    return committed_record


def _verify_issue_registry_binding(
    issue: Mapping[str, object],
    registry: Mapping[str, object],
) -> None:
    family = issue.get("family_decision")
    fingerprint = family.get("fingerprint") if isinstance(family, Mapping) else None
    if not isinstance(family, Mapping) or not isinstance(fingerprint, Mapping):
        raise ValueError("sealed issue family provenance is unavailable")
    expected = {
        "issue_id": issue.get("issue_id"),
        "family_id": issue.get("family_id"),
        "owner_issue_id": family.get("owner_issue_id"),
        "family_version_hash": family.get("family_version_hash"),
        "family_decision_hash": family.get("decision_hash"),
        "exact_signature": fingerprint.get("exact"),
        "structural_signature": fingerprint.get("structural"),
        "fingerprint_hash": fingerprint.get("fingerprint_hash"),
        "graph_snapshot_hash": fingerprint.get("graph_snapshot_hash"),
        "submission_path": fingerprint.get("submission_path"),
        "reduced_phenotype": fingerprint.get("reduced_validation_phenotype"),
        "original_validation_hash": issue.get("original_validation_hash"),
        "reduced_validation_hash": issue.get("reduced_validation_hash"),
        "original_validation_ref": issue.get("original_validation_ref"),
        "reduced_validation_ref": issue.get("final_validation_ref"),
    }
    if any(registry.get(name) != value for name, value in expected.items()):
        raise ValueError("sealed issue provenance does not match committed registry record")


def _has_current_possible_duplicate(root: Path, issue_id: str) -> bool:
    registry = _current_issue_registry(root)
    if issue_id not in registry.records:
        raise ValueError("issue is absent from the current duplicate registry")
    return bool(registry.possible_duplicate_ids(issue_id))


def _current_issue_registry(root: Path) -> IssueRegistry:
    registry = IssueRegistry()
    for path in (root / "registry.jsonl", root / "dedup_review.jsonl"):
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("issue registry record must be an object")
                value["stage"] = PipelineStage(value["stage"])
                value["developer_status"] = DeveloperStatus(value["developer_status"])
                registry.record(VersionedIssueRecord(**value))
    return registry


def _verify_current_duplicate_clearance(root: Path, issue_payload: object) -> None:
    if not isinstance(issue_payload, Mapping):
        raise ValueError("sealed issue payload is unavailable")
    issue_id = str(issue_payload.get("issue_id"))
    stored = issue_payload.get("duplicate_clearance")
    if isinstance(stored, Mapping) and stored.get("family_id"):
        from graph_sym_perf.issues.durable_registry import (
            _durable_family_payload_is_current_unlocked,
        )

        if not _durable_family_payload_is_current_unlocked(root, issue_payload):
            raise ValueError("sealed family decision is stale for the durable registry")
        return
    registry = _current_issue_registry(root)
    current = registry.duplicate_clearance(issue_id)
    if current is None or not isinstance(stored, Mapping):
        raise ValueError("issue has no current duplicate clearance")
    expected = {
        "issue_id": current.issue_id,
        "possible_duplicate": current.possible_duplicate,
        "cluster_members": list(current.cluster_members),
        "cluster_version_hash": current.cluster_version_hash,
        "evidence_url": current.evidence_url,
        "registry_signature": current.registry_signature,
    }
    normalized = {**stored, "cluster_members": list(stored.get("cluster_members", ())) }
    if normalized != expected:
        raise ValueError("sealed duplicate clearance does not match the current registry cluster")


__all__ = ["IssueCommands", "IssueGateError"]
