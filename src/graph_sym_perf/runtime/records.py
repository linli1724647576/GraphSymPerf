"""Compact, durable Stage-1 runtime evidence without query or plan text."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any, Protocol

from graph_sym_perf.core import to_jsonable
from graph_sym_perf.runtime.models import RuntimeEvidence

if TYPE_CHECKING:
    from graph_sym_perf.workload.models import CertifiedQueryPair


class _ValueLike(Protocol):
    value: str


class RulePlanEntryLike(Protocol):
    rule_id: str
    family: str
    stream: _ValueLike
    phase: _ValueLike
    graph_scale: int
    shard_seed: int
    measurement_protocol_id: str


class ArtifactWriteError(OSError):
    """A compact runtime artifact could not be durably committed."""


class ArtifactIntegrityError(ArtifactWriteError):
    """An existing compact runtime artifact is malformed or contradictory."""


@dataclass(frozen=True)
class PairRuntimeRecord:
    pair_hash: str
    rule_id: str
    family: str
    stream: str
    phase: str
    graph_scale: int
    shard_seed: int
    dbms_fingerprint: str
    dbms_health_epoch: str
    query_a_hash: str
    query_b_hash: str
    site_class: str
    proof_version: str
    safety_certificate_hash: str
    equivalence_summary: str
    noise_floor_id: str
    samples_us: tuple[tuple[str, int | None, int | None], ...]
    median_a_ms: float | None
    median_b_ms: float | None
    median_delta_ms: float | None
    median_ratio: float | None
    runtime_direction: str | None
    delta_over_noise: float | None
    direction_consistency: float
    timeout_state: str
    screen_stage: str
    protocol_version: str
    lifecycle_status: str
    source_query_ordinal: int | None = None
    graph_fingerprint: str | None = None

    @classmethod
    def from_evidence(
        cls,
        *,
        pair: CertifiedQueryPair,
        entry: RulePlanEntryLike,
        evidence: RuntimeEvidence,
        noise_floor_id: str,
        noise_p99_ms: float,
        dbms_fingerprint: str,
        dbms_health_epoch: str,
        source_query_ordinal: int | None = None,
        graph_fingerprint: str | None = None,
        equivalence_summary: str = "complete_typed_equivalent",
        lifecycle_status: str = "stage1_recorded",
    ) -> PairRuntimeRecord:
        certificate_payload = to_jsonable(asdict(pair.certificate))
        certificate_json = json.dumps(
            certificate_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        safety_hash = hashlib.sha256(certificate_json.encode("utf-8")).hexdigest()
        samples_us = tuple(
            (
                sample.order,
                _milliseconds_to_microseconds(sample.a_ms),
                _milliseconds_to_microseconds(sample.b_ms),
            )
            for sample in evidence.paired_samples
        )
        summary = summarize_integer_samples(samples_us, noise_p99_ms=noise_p99_ms)
        return cls(
            pair_hash=pair.pair_hash,
            rule_id=entry.rule_id,
            family=entry.family,
            stream=entry.stream.value,
            phase=entry.phase.value,
            graph_scale=entry.graph_scale,
            shard_seed=entry.shard_seed,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
            query_a_hash=evidence.query_a_hash,
            query_b_hash=evidence.query_b_hash,
            site_class=pair.certificate.site_class,
            proof_version=pair.certificate.proof_version,
            safety_certificate_hash=safety_hash,
            equivalence_summary=equivalence_summary,
            noise_floor_id=noise_floor_id,
            samples_us=samples_us,
            median_a_ms=summary["median_a_ms"],
            median_b_ms=summary["median_b_ms"],
            median_delta_ms=summary["median_delta_ms"],
            median_ratio=summary["median_ratio"],
            runtime_direction=summary["runtime_direction"],
            delta_over_noise=summary["delta_over_noise"],
            direction_consistency=summary["direction_consistency"],
            timeout_state=summary["timeout_state"],
            screen_stage="stage1",
            protocol_version=entry.measurement_protocol_id,
            lifecycle_status=lifecycle_status,
            source_query_ordinal=source_query_ordinal,
            graph_fingerprint=graph_fingerprint,
        )


class CompactRuntimeStore:
    """Single-writer append-fsync store whose count advances after durability."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.poison_path = self.path.with_name("pair_runtime.poison.json")
        if self.poison_path.exists():
            raise ArtifactIntegrityError("runtime record store has a durable write poison")
        self._lock = threading.Lock()
        self._records = self._load_records()
        self._pair_hashes = set(self._records)

    @property
    def count(self) -> int:
        return len(self._pair_hashes)

    @property
    def pair_hashes(self) -> frozenset[str]:
        return frozenset(self._pair_hashes)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(self._records[pair_hash]) for pair_hash in sorted(self._records))

    def append(self, record: PairRuntimeRecord) -> None:
        payload = to_jsonable(asdict(record))
        with self._lock:
            if record.pair_hash in self._pair_hashes:
                raise ArtifactWriteError(f"duplicate pair_hash: {record.pair_hash}")
            previous_size = self.path.stat().st_size if self.path.exists() else 0
            try:
                line = _canonical_json(payload)
                self._write_line(line)
            except (OSError, TypeError, ValueError) as exc:
                rollback_error = self._rollback(previous_size)
                poison_error = self._write_poison(
                    record_digest=canonical_record_digest(payload),
                    previous_size=previous_size,
                    reason=str(exc),
                    rollback_error=str(rollback_error) if rollback_error is not None else None,
                )
                if poison_error is not None:
                    raise ArtifactWriteError(
                        f"{exc}; rollback={rollback_error!r}; poison={poison_error!r}"
                    ) from exc
                raise ArtifactWriteError(str(exc)) from exc
            self._pair_hashes.add(record.pair_hash)
            self._records[record.pair_hash] = dict(payload)

    def _rollback(self, previous_size: int) -> OSError | None:
        try:
            if self.path.exists():
                with self.path.open("r+b") as handle:
                    handle.truncate(previous_size)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_dir(self.path.parent)
        except OSError as exc:
            return exc
        return None

    def _write_poison(
        self,
        *,
        record_digest: str,
        previous_size: int,
        reason: str,
        rollback_error: str | None,
    ) -> OSError | None:
        payload = {
            "previous_size": previous_size,
            "reason": reason,
            "record_digest": record_digest,
            "rollback_error": rollback_error,
            "schema_version": 1,
        }
        self.poison_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.poison_path.parent,
                prefix=f".{self.poison_path.name}.",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(_canonical_json(payload))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.poison_path)
            _fsync_dir(self.poison_path.parent)
        except OSError as exc:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            return exc
        return None

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _load_records(self) -> dict[str, dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ArtifactIntegrityError(f"runtime record store is unreadable: {exc}") from exc
        records: dict[str, dict[str, Any]] = {}
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArtifactIntegrityError("runtime record store contains truncated JSON") from exc
            if not isinstance(payload, dict):
                raise ArtifactIntegrityError("runtime record must be a JSON object")
            pair_hash = payload.get("pair_hash")
            if not isinstance(pair_hash, str) or len(pair_hash) != 64:
                raise ArtifactIntegrityError("runtime record pair_hash is invalid")
            if pair_hash in records:
                raise ArtifactIntegrityError("runtime record pair_hash is duplicated")
            if any(key in payload for key in ("query_a", "query_b", "profile", "plan")):
                raise ArtifactIntegrityError("runtime record contains forbidden verbose evidence")
            records[pair_hash] = payload
        return records


def summarize_integer_samples(
    samples_us: tuple[tuple[str, int | None, int | None], ...]
    | list[tuple[str, int | None, int | None]],
    *,
    noise_p99_ms: float,
) -> dict[str, Any]:
    """Reconstruct every compact summary from canonical integer samples."""

    if (
        isinstance(noise_p99_ms, bool)
        or not isinstance(noise_p99_ms, int | float)
        or not math.isfinite(noise_p99_ms)
        or noise_p99_ms < 0
    ):
        raise ValueError("noise_p99_ms must be finite and non-negative")
    if not samples_us:
        raise ValueError("runtime samples must not be empty")
    normalized: list[tuple[str, int | None, int | None]] = []
    for sample in samples_us:
        if not isinstance(sample, tuple | list) or len(sample) != 3:
            raise ValueError("runtime sample must contain order, A, and B")
        order, a_us, b_us = sample
        if order not in {"AB", "BA"}:
            raise ValueError("runtime sample order is invalid")
        for value in (a_us, b_us):
            if value is not None and (
                type(value) is not int or value < 0
            ):
                raise ValueError("runtime microseconds must be non-negative integers or null")
        normalized.append((order, a_us, b_us))

    a_values = [a for _order, a, _b in normalized if a is not None]
    b_values = [b for _order, _a, b in normalized if b is not None]
    median_a_ms = float(median(a_values) / 1000.0) if a_values else None
    median_b_ms = float(median(b_values) / 1000.0) if b_values else None
    a_timeout = len(a_values) != len(normalized)
    b_timeout = len(b_values) != len(normalized)
    if a_timeout or b_timeout:
        if a_timeout and b_timeout:
            timeout_state = "both_timeout"
            runtime_direction = None
        elif a_timeout:
            timeout_state = "a_timeout"
            runtime_direction = "a"
        else:
            timeout_state = "b_timeout"
            runtime_direction = "b"
        return {
            "delta_over_noise": None,
            "direction_consistency": 0.0,
            "median_a_ms": median_a_ms,
            "median_b_ms": median_b_ms,
            "median_delta_ms": None,
            "median_ratio": None,
            "runtime_direction": runtime_direction,
            "timeout_state": timeout_state,
        }

    assert median_a_ms is not None and median_b_ms is not None
    runtime_direction = (
        "a" if median_a_ms > median_b_ms else "b" if median_b_ms > median_a_ms else None
    )
    oriented = [
        (b, a) if runtime_direction == "a" else (a, b)
        for _order, a, b in normalized
        if a is not None and b is not None
    ]
    ratios = [slow / fast for fast, slow in oriented if fast > 0]
    if not ratios:
        raise ValueError("runtime samples have no positive fast-side observation")
    median_delta_ms = float(median(slow - fast for fast, slow in oriented) / 1000.0)
    median_ratio = float(median(ratios))
    direction_consistency = sum(value > 1.0 for value in ratios) / len(ratios)
    return {
        "delta_over_noise": median_delta_ms / max(float(noise_p99_ms), 0.001),
        "direction_consistency": direction_consistency,
        "median_a_ms": median_a_ms,
        "median_b_ms": median_b_ms,
        "median_delta_ms": median_delta_ms,
        "median_ratio": median_ratio,
        "runtime_direction": runtime_direction,
        "timeout_state": "none",
    }


def canonical_record_digest(record: PairRuntimeRecord | dict[str, Any]) -> str:
    payload = to_jsonable(asdict(record)) if isinstance(record, PairRuntimeRecord) else record
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _fsync_dir(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _milliseconds_to_microseconds(value: float | None) -> int | None:
    return None if value is None else int(round(value * 1000.0))


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactWriteError",
    "CompactRuntimeStore",
    "PairRuntimeRecord",
    "canonical_record_digest",
    "summarize_integer_samples",
]
