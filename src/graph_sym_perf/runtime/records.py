"""Compact, durable Stage-1 runtime evidence without query or plan text."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
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
        timeout_state = _timeout_state(evidence)
        delta_over_noise = (
            evidence.median_delta_ms / max(noise_p99_ms, 0.001)
            if evidence.median_delta_ms is not None
            else None
        )
        return cls(
            pair_hash=pair.pair_hash,
            rule_id=entry.rule_id,
            family=entry.family,
            stream=entry.stream.value,
            phase=entry.phase.value,
            graph_scale=entry.graph_scale,
            shard_seed=entry.shard_seed,
            query_a_hash=evidence.query_a_hash,
            query_b_hash=evidence.query_b_hash,
            site_class=pair.certificate.site_class,
            proof_version=pair.certificate.proof_version,
            safety_certificate_hash=safety_hash,
            equivalence_summary=equivalence_summary,
            noise_floor_id=noise_floor_id,
            samples_us=samples_us,
            median_a_ms=evidence.median_a_ms,
            median_b_ms=evidence.median_b_ms,
            median_delta_ms=evidence.median_delta_ms,
            median_ratio=evidence.median_ratio,
            runtime_direction=evidence.slower_side,
            delta_over_noise=delta_over_noise,
            direction_consistency=evidence.direction_consistency,
            timeout_state=timeout_state,
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
        line = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        with self._lock:
            if record.pair_hash in self._pair_hashes:
                raise ArtifactWriteError(f"duplicate pair_hash: {record.pair_hash}")
            try:
                self._write_line(line)
            except (OSError, TypeError, ValueError) as exc:
                raise ArtifactWriteError(str(exc)) from exc
            self._pair_hashes.add(record.pair_hash)
            self._records[record.pair_hash] = dict(payload)

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


def _milliseconds_to_microseconds(value: float | None) -> int | None:
    return None if value is None else int(round(value * 1000.0))


def _timeout_state(evidence: RuntimeEvidence) -> str:
    a_timeout = any(sample.a_timeout for sample in evidence.paired_samples)
    b_timeout = any(sample.b_timeout for sample in evidence.paired_samples)
    if a_timeout and b_timeout:
        return "both_timeout"
    if a_timeout:
        return "a_timeout"
    if b_timeout:
        return "b_timeout"
    return "none"


__all__ = ["ArtifactIntegrityError", "ArtifactWriteError", "CompactRuntimeStore", "PairRuntimeRecord"]
