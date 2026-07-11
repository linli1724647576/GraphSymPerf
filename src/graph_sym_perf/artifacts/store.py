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
    for validation in (original, final):
        candidate = load_artifact_record(store, validation.payload["candidate_ref"])
        if getattr(candidate.level, "value", candidate.level) != "candidate":
            raise ValueError("validation candidate_ref has the wrong artifact level")
        if candidate.payload.get("candidate_hash") != validation.payload.get("candidate_hash"):
            raise ValueError("validation candidate_ref breaks the candidate hash chain")
        resolved_candidates.append(candidate)
    if original.payload["candidate_hash"] != payload["candidate_hash"]:
        raise ValueError("original validation does not bind the issue candidate")
    if final.payload["candidate_hash"] != payload["reduced_candidate_hash"]:
        raise ValueError("final validation does not bind the reduced candidate")
    final_hashes = resolved_candidates[1].payload.get("content_hashes")
    if not isinstance(final_hashes, Mapping) or (
        final_hashes.get("query_a") != payload["reduced_content_hashes"]["query_a"]
        or final_hashes.get("query_b") != payload["reduced_content_hashes"]["query_b"]
    ):
        raise ValueError("reduced candidate does not bind the issue query pair")
    reduction = json.loads(store.read(payload["reduction_ref"]))
    if not isinstance(reduction, dict) or (
        reduction.get("candidate_hash") != payload["candidate_hash"]
        or reduction.get("reduced_candidate_hash") != payload["reduced_candidate_hash"]
        or reduction.get("reduced_content_hashes") != dict(payload["reduced_content_hashes"])
    ):
        raise ValueError("reduction artifact breaks the issue hash chain")
    store.read(payload["reproducer_ref"])
    if payload.get("explanation_ref") is not None:
        store.read(payload["explanation_ref"])


__all__ = [
    "ArtifactBudgetExceeded",
    "ArtifactWriteResult",
    "ContentAddressedStore",
    "ShardBudgetReport",
    "enforce_shard_budget",
    "load_artifact_record",
    "verify_candidate_record_reference",
    "verify_issue_hash_chain",
]
