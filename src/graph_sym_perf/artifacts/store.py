from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import fcntl

from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord, ArtifactReference
from graph_sym_perf.reducer.models import MinimalityAttempt, MinimalityDeletion, MinimalityProof
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci
from graph_sym_perf.validation.models import canonical_content_hash
from statistics import median


class ArtifactBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactWriteResult:
    reference: ArtifactReference | None
    warning: str | None = None


@dataclass(frozen=True)
class ShardBudgetReport:
    used_bytes: int
    budget_bytes: int
    warnings: tuple[str, ...] = ()


class ContentAddressedStore:
    def __init__(self, root: str | Path, *, budget_bytes: int) -> None:
        if not isinstance(budget_bytes, int) or isinstance(budget_bytes, bool) or budget_bytes <= 0:
            raise ValueError("artifact budget must be positive")
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.budget_bytes = budget_bytes
        self._lock_path = self.root / ".budget.lock"
        self._usage_path = self.root / ".used_bytes"
        with self._lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # The object link and counter replacement cannot be one filesystem
            # transaction. Reconcile on every open so a crash in that window
            # cannot permanently undercount the shard.
            self._write_used_bytes(self._scan_used_bytes())

    @property
    def used_bytes(self) -> int:
        with self._lock_path.open("r", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._read_used_bytes()

    def put(self, name: str, payload: bytes, *, media_type: str = "application/octet-stream") -> ArtifactReference:
        if not name or not isinstance(payload, bytes):
            raise TypeError("artifact name and byte payload are required")
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("objects") / digest[:2] / digest
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if destination.exists():
                if destination.read_bytes() != payload:
                    raise OSError("content-address collision")
                return ArtifactReference(digest, str(relative), len(payload), media_type)
            current = self._read_used_bytes()
            if current + len(payload) > self.budget_bytes:
                raise ArtifactBudgetExceeded("per-shard artifact budget exceeded")
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=destination.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            self._write_used_bytes(current + len(payload))
        return ArtifactReference(digest, str(relative), len(payload), media_type)

    def put_json(self, name: str, payload: object) -> ArtifactReference:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.put(name, encoded, media_type="application/json")

    def read(self, reference: ArtifactReference | dict[str, object]) -> bytes:
        if not isinstance(reference, ArtifactReference):
            reference = ArtifactReference(**reference)
        path = (self.root / reference.relative_path).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise ValueError("artifact reference escapes store root")
        payload = path.read_bytes()
        if len(payload) != reference.size_bytes:
            raise OSError("artifact reference size mismatch")
        if hashlib.sha256(payload).hexdigest() != reference.content_hash:
            raise OSError("artifact reference content hash mismatch")
        return payload

    def _scan_used_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.objects.rglob("*") if path.is_file())

    def _read_used_bytes(self) -> int:
        try:
            value = int(self._usage_path.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError) as exc:
            raise OSError("artifact usage counter is missing or corrupt") from exc
        if value < 0:
            raise OSError("artifact usage counter is negative")
        return value

    def _write_used_bytes(self, value: int) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".used_bytes.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(str(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._usage_path)
        finally:
            temporary.unlink(missing_ok=True)

    def put_with_policy(
        self,
        name: str,
        payload: bytes,
        *,
        level: ArtifactLevel,
        essential: bool,
        media_type: str = "application/octet-stream",
    ) -> ArtifactWriteResult:
        try:
            return ArtifactWriteResult(self.put(name, payload, media_type=media_type))
        except ArtifactBudgetExceeded:
            if level is ArtifactLevel.MINIMAL and not essential:
                return ArtifactWriteResult(None, "SHARD_ARTIFACT_BUDGET_DEGRADED")
            raise


def enforce_shard_budget(
    root: str | Path,
    *,
    budget_bytes: int,
    nonessential_paths: tuple[str | Path, ...] = (),
    reserve_bytes: int = 0,
) -> ShardBudgetReport:
    """Bound a complete shard, dropping only declared non-candidate diagnostics."""

    if budget_bytes <= 0 or reserve_bytes < 0 or reserve_bytes >= budget_bytes:
        raise ValueError("invalid shard budget or reserve")
    root_path = Path(root).resolve()
    limit = budget_bytes - reserve_bytes
    used = _directory_size(root_path)
    warnings: list[str] = []
    if used > limit:
        for candidate in nonessential_paths:
            path = Path(candidate).resolve()
            if path == root_path:
                raise ValueError("shard root cannot be declared nonessential")
            if root_path not in path.parents:
                raise ValueError("nonessential artifact path escapes shard directory")
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
            else:
                continue
            warnings.append(f"SHARD_ARTIFACT_BUDGET_DEGRADED:{path.relative_to(root_path)}")
            used = _directory_size(root_path)
            if used <= limit:
                break
    if used > limit:
        raise ArtifactBudgetExceeded(
            f"complete shard artifacts use {used} bytes; allowed {limit} bytes after reserve"
        )
    return ShardBudgetReport(used, budget_bytes, tuple(warnings))


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def load_artifact_record(
    store: ContentAddressedStore,
    reference: ArtifactReference | dict[str, object],
) -> ArtifactRecord:
    try:
        value = json.loads(store.read(reference))
    except json.JSONDecodeError as exc:
        raise OSError("referenced artifact record is not valid JSON") from exc
    if not isinstance(value, dict):
        raise OSError("referenced artifact record must be an object")
    level = value.pop("level", None)
    schema_version = value.pop("schema_version", None)
    protocol = value.pop("measurement_protocol", None)
    value.pop("record_ref", None)
    return ArtifactRecord(
        level=level,
        payload=value,
        schema_version=schema_version,
        measurement_protocol=protocol,
    )


def verify_candidate_record_reference(
    store: ContentAddressedStore,
    record: dict[str, object],
) -> ArtifactRecord:
    reference = record.get("record_ref")
    if not isinstance(reference, dict):
        raise ValueError("candidate record reference is missing")
    loaded = load_artifact_record(store, reference)
    expected = dict(record)
    expected.pop("record_ref", None)
    encoded = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != reference.get("content_hash"):
        raise OSError("candidate JSONL record does not match its content-addressed record")
    if loaded.to_dict() != expected:
        raise OSError("candidate referenced record payload mismatch")
    return loaded


def verify_issue_hash_chain(
    store: ContentAddressedStore,
    issue: ArtifactRecord,
) -> None:
    if getattr(issue.level, "value", issue.level) != "issue":
        raise TypeError("issue hash-chain verification requires an issue artifact")
    payload = issue.payload
    original = load_artifact_record(store, payload["original_validation_ref"])
    final = load_artifact_record(store, payload["final_validation_ref"])
    if getattr(original.level, "value", original.level) != "validation" or getattr(final.level, "value", final.level) != "validation":
        raise ValueError("issue validation references have the wrong artifact level")
    if original.payload.get("stage") != "validation_passed" or final.payload.get("stage") != "validation_passed":
        raise ValueError("issue hash chain requires passed original and reduced validation")
    resolved_candidates = []
    validation_directions: list[str] = []
    for validation in (original, final):
        candidate = load_artifact_record(store, validation.payload["candidate_ref"])
        if getattr(candidate.level, "value", candidate.level) != "candidate":
            raise ValueError("validation candidate_ref has the wrong artifact level")
        if candidate.payload.get("candidate_hash") != validation.payload.get("candidate_hash"):
            raise ValueError("validation candidate_ref breaks the candidate hash chain")
        resolved_candidates.append(candidate)
        graph_hash = candidate.payload.get("graph_snapshot_hash")
        candidate_hashes = candidate.payload.get("content_hashes")
        sessions = tuple(validation.payload.get("sessions", ()))
        if not isinstance(candidate_hashes, Mapping) or any(
            session.get("graph_fingerprint") != graph_hash
            or session.get("dbms_fingerprint") != candidate.payload.get("dbms_fingerprint")
            or session.get("query_a_hash") != candidate_hashes.get("query_a")
            or session.get("query_b_hash") != candidate_hashes.get("query_b")
            for session in sessions
        ):
            raise ValueError("validation session identity does not bind the candidate")
        environment = validation.payload.get("environment")
        if (
            not isinstance(environment, Mapping)
            or canonical_content_hash(environment.get("dbms_version"))
            != candidate_hashes.get("dbms_version")
            or environment.get("config_hash") != candidate_hashes.get("resolved_config")
        ):
            raise ValueError("validation environment does not bind the candidate")
        directions = {session.get("slow_query") for session in sessions}
        if len(directions) != 1:
            raise ValueError("validation sessions disagree on runtime direction")
        validation_direction = str(next(iter(directions)))
        if validation_direction != candidate.payload.get("discovery_slow_query"):
            raise ValueError("validation direction does not preserve candidate discovery direction")
        validation_directions.append(validation_direction)
    if original.payload["candidate_hash"] != payload["candidate_hash"]:
        raise ValueError("original validation does not bind the issue candidate")
    if final.payload["candidate_hash"] != payload["reduced_candidate_hash"]:
        raise ValueError("final validation does not bind the reduced candidate")
    if (
        original.payload.get("report_hash") != payload.get("original_validation_hash")
        or final.payload.get("report_hash") != payload.get("reduced_validation_hash")
    ):
        raise ValueError("issue validation report hash does not bind referenced validation")
    final_hashes = resolved_candidates[1].payload.get("content_hashes")
    original_hashes = resolved_candidates[0].payload.get("content_hashes")
    if not isinstance(original_hashes, Mapping) or not isinstance(final_hashes, Mapping) or (
        final_hashes.get("query_a") != payload["reduced_content_hashes"]["query_a"]
        or final_hashes.get("query_b") != payload["reduced_content_hashes"]["query_b"]
    ):
        raise ValueError("reduced candidate does not bind the issue query pair")
    if (
        (original_hashes.get("query_a"), original_hashes.get("query_b"))
        == (final_hashes.get("query_a"), final_hashes.get("query_b"))
        or any(
            original_hashes.get(name) != final_hashes.get(name)
            for name in ("graph_snapshot", "dbms_fingerprint", "dbms_version", "resolved_config", "validation_policy", "certificate")
        )
    ):
        raise ValueError("reduction candidate identity does not preserve the sealed environment")
    family = payload.get("family_decision")
    fingerprint = family.get("fingerprint") if isinstance(family, Mapping) else None
    if (
        not isinstance(fingerprint, Mapping)
        or fingerprint.get("graph_snapshot_hash")
        != resolved_candidates[1].payload.get("graph_snapshot_hash")
        or fingerprint.get("dbms_fingerprint")
        != resolved_candidates[1].payload.get("dbms_fingerprint")
    ):
        raise ValueError("issue family fingerprint does not bind reduced candidate identity")
    reduction = json.loads(store.read(payload["reduction_ref"]))
    if not isinstance(reduction, dict) or (
        reduction.get("candidate_hash") != payload["candidate_hash"]
        or reduction.get("reduced_candidate_hash") != payload["reduced_candidate_hash"]
        or reduction.get("reduced_content_hashes") != dict(payload["reduced_content_hashes"])
        or reduction.get("original_validation_hash") != payload.get("original_validation_hash")
        or reduction.get("reduced_validation_hash") != payload.get("reduced_validation_hash")
        or reduction.get("original_validation_ref") != dict(payload["original_validation_ref"])
        or reduction.get("reduced_validation_ref") != dict(payload["final_validation_ref"])
    ):
        raise ValueError("reduction artifact breaks the issue hash chain")
    reduction_evidence = reduction.get("final_evidence")
    if (
        not isinstance(reduction_evidence, Mapping)
        or len(validation_directions) != 2
        or validation_directions[0] != validation_directions[1]
        or validation_directions[1] != reduction_evidence.get("slow_query")
    ):
        raise ValueError("runtime direction is not preserved across validation and reduction")
    final_samples = [
        sample
        for session in final.payload["sessions"]
        for sample in session["paired_timing"]
    ]
    if reduction_evidence.get("paired_samples") != final_samples:
        raise ValueError("reduction evidence samples do not bind final validation")
    if payload.get("validated_ratio") != final.payload.get("validated_ratio"):
        raise ValueError("issue validated_ratio does not bind final validation")
    _verify_reduction_promotion_evidence(reduction, resolved_candidates[1], payload)
    for validation in (original, final):
        environment = validation.payload.get("environment")
        if not isinstance(environment, Mapping) or environment.get("dbms_version") != payload.get("dbms_version"):
            raise ValueError("validation DBMS version does not bind the issue artifact")
    store.read(payload["reproducer_ref"])
    if payload.get("explanation_ref") is not None:
        store.read(payload["explanation_ref"])


def verify_candidate_outcome_validation_chain(
    store: ContentAddressedStore,
    outcome: Mapping[str, object],
) -> None:
    """Verify the independently stored validation/reduction provenance on resume."""

    validation = outcome.get("validation")
    reduction = outcome.get("reduction")
    if not isinstance(validation, Mapping) or not isinstance(reduction, Mapping):
        raise ValueError("candidate outcome validation/reduction evidence is unavailable")
    validation_ref = validation.get("validation_ref")
    if not isinstance(validation_ref, Mapping):
        raise ValueError("candidate outcome validation reference is unavailable")
    original = load_artifact_record(store, validation_ref)
    if (
        getattr(original.level, "value", original.level) != "validation"
        or original.payload.get("candidate_hash") != outcome.get("candidate_hash")
        or original.payload.get("stage") != validation.get("stage")
    ):
        raise ValueError("candidate outcome validation reference breaks identity")
    status = reduction.get("status")
    validation_statuses = {
        "reduced_valid",
        "reduction_invalidated",
        "reduction_partial",
    }
    if status not in validation_statuses:
        return
    reduction_ref = reduction.get("reduction_ref")
    if not isinstance(reduction_ref, Mapping):
        raise ValueError("candidate outcome validation provenance has no reduction reference")
    summary = json.loads(store.read(reduction_ref))
    if not isinstance(summary, Mapping):
        raise ValueError("candidate outcome reduction summary is unavailable")
    if (
        summary.get("candidate_hash") != outcome.get("candidate_hash")
        or summary.get("status") != status
        or summary.get("reduced_candidate_hash")
        != (reduction.get("reduced_candidate_hash") or "")
    ):
        raise ValueError("candidate outcome reduction summary breaks identity")
    fields = (
        "original_validation_hash",
        "reduced_validation_hash",
        "original_validation_ref",
        "reduced_validation_ref",
    )
    provenance_present = any(name in summary or name in reduction for name in fields)
    if not provenance_present:
        if status != "reduction_partial":
            raise ValueError("candidate outcome validation provenance is unavailable")
        return
    if any(name not in summary or name not in reduction for name in fields):
        raise ValueError("candidate outcome validation provenance is incomplete")
    if any(summary.get(name) != reduction.get(name) for name in fields):
        raise ValueError("candidate outcome validation provenance differs from reduction summary")
    if summary.get("original_validation_ref") != dict(validation_ref):
        raise ValueError("candidate outcome original validation reference is inconsistent")
    reduced_ref = summary.get("reduced_validation_ref")
    if not isinstance(reduced_ref, Mapping):
        raise ValueError("candidate outcome reduced validation reference is unavailable")
    reduced = load_artifact_record(store, reduced_ref)
    if (
        getattr(reduced.level, "value", reduced.level) != "validation"
        or original.payload.get("report_hash") != summary.get("original_validation_hash")
        or reduced.payload.get("report_hash") != summary.get("reduced_validation_hash")
        or reduced.payload.get("candidate_hash") != reduction.get("reduced_candidate_hash")
    ):
        raise ValueError("candidate outcome validation report hash/identity is invalid")


def _verify_reduction_promotion_evidence(
    reduction: Mapping[str, object],
    reduced_candidate: ArtifactRecord,
    issue: Mapping[str, object],
) -> None:
    if (
        reduction.get("status") != "reduced_valid"
        or reduction.get("stop_reason") != "exhausted_and_validated"
    ):
        raise ValueError("reduction artifact is not REDUCED_VALID")
    content_hashes = reduced_candidate.payload.get("content_hashes")
    if not isinstance(content_hashes, Mapping) or (
        reduction.get("validation_policy_hash") != content_hashes.get("validation_policy")
        or reduction.get("certificate_hash") != content_hashes.get("certificate")
        or reduction.get("certificate_hash") != canonical_content_hash(issue.get("certificate"))
    ):
        raise ValueError("reduction policy/certificate hashes do not bind the reduced candidate")
    proof_value = reduction.get("minimality_proof")
    if not isinstance(proof_value, Mapping):
        raise ValueError("reduction minimality proof is unavailable")
    try:
        proof = MinimalityProof(
            reduced_pair_hash=str(proof_value["reduced_pair_hash"]),
            protocol=str(proof_value["protocol"]),
            protocol_version=str(proof_value["protocol_version"]),
            universe_hash=str(proof_value["universe_hash"]),
            enumerated_deletions=tuple(
                MinimalityDeletion(**item) for item in proof_value["enumerated_deletions"]
            ),
            attempts=tuple(MinimalityAttempt(**item) for item in proof_value["attempts"]),
            chain_hash=str(proof_value["chain_hash"]),
        )
        pair = EquivalentQueryPair(
            str(issue["reduced_query_a"]),
            str(issue["reduced_query_b"]),
            dict(reduced_candidate.payload.get("parameters", {})),
            str(reduced_candidate.payload["graph_snapshot_hash"]),
            str(reduced_candidate.payload["dbms_fingerprint"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("reduction minimality proof cannot be reconstructed") from exc
    if not proof.verify(pair):
        raise ValueError("reduction minimality proof does not verify")
    evidence = reduction.get("final_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("reduction final runtime evidence is unavailable")
    reduced_hashes = issue["reduced_content_hashes"]
    if (
        evidence.get("query_a_hash") != reduced_hashes["query_a"]
        or evidence.get("query_b_hash") != reduced_hashes["query_b"]
        or evidence.get("graph_snapshot_hash")
        != reduced_candidate.payload.get("graph_snapshot_hash")
        or evidence.get("dbms_fingerprint")
        != reduced_candidate.payload.get("dbms_fingerprint")
    ):
        raise ValueError("reduction final runtime evidence does not bind the reduced candidate")
    samples = evidence.get("paired_samples")
    if (
        evidence.get("verdict") != "pass"
        or evidence.get("measurement_source") != "ordinary_execute"
        or evidence.get("measurement_protocol") != "ordinary-execute-40-pair-combined-v1"
        or evidence.get("slow_query") not in {"a", "b"}
        or not isinstance(samples, list | tuple)
        or len(samples) != 40
        or sum(sample.get("order") == "AB" for sample in samples) != 20
        or sum(sample.get("order") == "BA" for sample in samples) != 20
    ):
        raise ValueError("reduction final runtime protocol is incomplete")
    slow_query = evidence["slow_query"]
    oriented = []
    for sample in samples:
        a_ms = sample.get("a_ms")
        b_ms = sample.get("b_ms")
        if (
            not isinstance(a_ms, int | float)
            or isinstance(a_ms, bool)
            or not isinstance(b_ms, int | float)
            or isinstance(b_ms, bool)
            or a_ms <= 0
            or b_ms < 0
            or sample.get("a_timeout", False)
            or sample.get("b_timeout", False)
        ):
            raise ValueError("reduction final runtime sample is invalid")
        oriented.append((a_ms, b_ms) if slow_query == "b" else (b_ms, a_ms))
    ratios = [slow / fast for fast, slow in oriented]
    ratio = float(median(ratios))
    delta = float(median(slow - fast for fast, slow in oriented))
    ci = bootstrap_ratio_ci(tuple(oriented))
    direction_consistency = sum(sample_ratio > 1.0 for sample_ratio in ratios) / len(ratios)
    if (
        ratio < 3.0
        or ci.low < 2.0
        or delta <= 0
        or direction_consistency <= 0.5
        or tuple(evidence.get("reason_codes", ()))
        or abs(float(evidence.get("median_ratio", 0.0)) - ratio) > 1e-12
        or abs(float(evidence.get("median_delta_ms", 0.0)) - delta) > 1e-12
        or abs(float(evidence.get("ci_ratio_low", 0.0)) - ci.low) > 1e-12
        or abs(float(evidence.get("ci_ratio_high", 0.0)) - ci.high) > 1e-12
        or abs(float(evidence.get("direction_consistency", 0.0)) - direction_consistency)
        > 1e-12
    ):
        raise ValueError("reduction final runtime evidence does not satisfy promotion")


__all__ = [
    "ArtifactBudgetExceeded",
    "ArtifactWriteResult",
    "ContentAddressedStore",
    "ShardBudgetReport",
    "enforce_shard_budget",
    "load_artifact_record",
    "verify_candidate_record_reference",
    "verify_candidate_outcome_validation_chain",
    "verify_issue_hash_chain",
]
