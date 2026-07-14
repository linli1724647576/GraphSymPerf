"""Deterministic two-stage runtime screening without plan/profile evidence."""

from __future__ import annotations

import math
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.runtime.records import (
    ArtifactIntegrityError,
    ArtifactWriteError,
    PairRuntimeRecord,
)


_TIMEOUT_STATES = {"a_timeout", "b_timeout", "both_timeout"}


@dataclass(frozen=True)
class Stage2Decision:
    relative_pass: bool
    absolute_pass: bool
    reason_codes: tuple[str, ...]

    @property
    def promote(self) -> bool:
        return self.relative_pass or self.absolute_pass


@dataclass(frozen=True)
class Stage2Candidate:
    pair_hash: str
    decision: Stage2Decision
    noise_normalized_score: float
    evidence: RuntimeEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.pair_hash, str) or len(self.pair_hash) != 64:
            raise ValueError("Stage-2 candidate pair_hash must be 64 hex characters")
        if (
            isinstance(self.noise_normalized_score, bool)
            or not isinstance(self.noise_normalized_score, int | float)
            or not math.isfinite(self.noise_normalized_score)
        ):
            raise ValueError("Stage-2 noise-normalized score must be finite")


@dataclass(frozen=True)
class TimeoutQueueRecord:
    entry_id: str
    pair_hash: str
    timeout_side: str
    stage: str
    evidence_digest: str
    graph_snapshot_hash: str
    dbms_fingerprint: str
    dbms_health_epoch: str
    noise_floor_id: str

    def __post_init__(self) -> None:
        if not self.entry_id:
            raise ValueError("timeout queue entry_id is required")
        if self.timeout_side not in {"a", "b"}:
            raise ValueError("timeout queue side must be 'a' or 'b'")
        if self.stage not in {"stage1", "stage2"}:
            raise ValueError("timeout queue stage must be stage1 or stage2")
        for name in (
            "pair_hash",
            "evidence_digest",
            "graph_snapshot_hash",
            "dbms_fingerprint",
            "dbms_health_epoch",
            "noise_floor_id",
        ):
            _require_sha256(getattr(self, name), name)

    @property
    def dedupe_key(self) -> str:
        return _sha256_json(
            {
                "entry_id": self.entry_id,
                "evidence_digest": self.evidence_digest,
                "pair_hash": self.pair_hash,
                "timeout_side": self.timeout_side,
            }
        )

    def to_payload(self) -> dict[str, object]:
        body = {
            "schema_version": 1,
            **asdict(self),
            "dedupe_key": self.dedupe_key,
        }
        return {**body, "content_hash": _sha256_json(body)}


class DurableTimeoutQueue:
    """Strict append-fsync timeout queue with rollback and exact deduplication."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.poison_path = self.path.with_name("timeout_queue.poison.json")
        self._poisoned = False
        if self.poison_path.exists():
            self._validate_poison()
            raise ArtifactIntegrityError("timeout queue has a durable write poison")
        self._lock = threading.Lock()
        self._records = self._load()

    @property
    def count(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(value) for value in self._records.values())

    def append(self, record: TimeoutQueueRecord) -> bool:
        payload = record.to_payload()
        with self._lock:
            if self._poisoned:
                raise ArtifactIntegrityError("timeout queue has an in-memory write poison")
            existing = self._records.get(record.dedupe_key)
            if existing is not None:
                if existing != payload:
                    raise ArtifactIntegrityError(
                        "timeout queue dedupe key has contradictory evidence"
                    )
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            previous_size = self.path.stat().st_size if self.path.exists() else 0
            try:
                self._write_line(_canonical_json(payload))
            except (OSError, TypeError, ValueError) as exc:
                rollback_error = self._rollback(previous_size)
                self._poisoned = True
                poison_error = self._write_poison(
                    record_digest=_sha256_json(payload),
                    dedupe_key=record.dedupe_key,
                    previous_size=previous_size,
                    reason=str(exc),
                    rollback_error=(
                        str(rollback_error) if rollback_error is not None else None
                    ),
                )
                if poison_error is not None:
                    raise ArtifactWriteError(
                        f"{exc}; rollback={rollback_error!r}; poison={poison_error!r}"
                    ) from exc
                raise ArtifactWriteError(str(exc)) from exc
            self._records[record.dedupe_key] = payload
            return True

    def reconcile(self, expected: Iterable[TimeoutQueueRecord]) -> None:
        expected_by_key: dict[str, tuple[TimeoutQueueRecord, dict[str, object]]] = {}
        for record in expected:
            if not isinstance(record, TimeoutQueueRecord):
                raise TypeError("timeout reconciliation requires typed records")
            payload = record.to_payload()
            previous = expected_by_key.get(record.dedupe_key)
            if previous is not None and previous[1] != payload:
                raise ArtifactIntegrityError(
                    "timeout reconciliation contains contradictory expected evidence"
                )
            expected_by_key[record.dedupe_key] = (record, payload)
        with self._lock:
            if self._poisoned:
                raise ArtifactIntegrityError("timeout queue has an in-memory write poison")
            for key, payload in self._records.items():
                expected_value = expected_by_key.get(key)
                if expected_value is None:
                    raise ArtifactIntegrityError(
                        "timeout queue contains foreign evidence outside the expected set"
                    )
                if payload != expected_value[1]:
                    raise ArtifactIntegrityError(
                        "timeout queue contains contradictory expected evidence"
                    )
        for key in sorted(expected_by_key):
            if key not in self._records:
                self.append(expected_by_key[key][0])
        if self._records != {
            key: payload for key, (_record, payload) in expected_by_key.items()
        }:
            raise ArtifactIntegrityError(
                "timeout queue does not exactly match the expected evidence set"
            )

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            _fsync_directory(self.path.parent)

    def _rollback(self, previous_size: int) -> OSError | None:
        try:
            if self.path.exists():
                with self.path.open("r+b") as handle:
                    handle.truncate(previous_size)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(self.path.parent)
        except OSError as exc:
            return exc
        return None

    def _write_poison(
        self,
        *,
        record_digest: str,
        dedupe_key: str,
        previous_size: int,
        reason: str,
        rollback_error: str | None,
    ) -> OSError | None:
        body = {
            "dedupe_key": dedupe_key,
            "previous_size": previous_size,
            "reason": reason,
            "record_digest": record_digest,
            "rollback_error": rollback_error,
            "schema_version": 1,
        }
        payload = {**body, "content_hash": _sha256_json(body)}
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
            _fsync_directory(self.poison_path.parent)
        except OSError as exc:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            return exc
        return None

    def _validate_poison(self) -> None:
        try:
            raw = self.poison_path.read_bytes()
            if not raw.endswith(b"\n"):
                raise ValueError("truncated poison")
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactIntegrityError("timeout queue poison is unreadable or invalid") from exc
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("timeout queue poison is not an object")
        body = {key: value for key, value in payload.items() if key != "content_hash"}
        if (
            set(body)
            != {
                "dedupe_key",
                "previous_size",
                "reason",
                "record_digest",
                "rollback_error",
                "schema_version",
            }
            or body.get("schema_version") != 1
            or payload.get("content_hash") != _sha256_json(body)
        ):
            raise ArtifactIntegrityError("timeout queue poison hash or shape differs")

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ArtifactIntegrityError("timeout queue is unreadable") from exc
        if raw and not raw.endswith(b"\n"):
            raise ArtifactIntegrityError("timeout queue contains a truncated tail")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise ArtifactIntegrityError("timeout queue is not UTF-8") from exc
        records: dict[str, dict[str, object]] = {}
        for line in lines:
            try:
                payload = json.loads(line)
                record = _timeout_record_from_payload(payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ArtifactIntegrityError("timeout queue contains invalid evidence") from exc
            canonical = record.to_payload()
            if payload != canonical:
                raise ArtifactIntegrityError("timeout queue evidence hash or shape differs")
            if record.dedupe_key in records:
                raise ArtifactIntegrityError("timeout queue contains a duplicate record")
            records[record.dedupe_key] = canonical
        return records


def stage1_k(accepted_pairs: int) -> int:
    if isinstance(accepted_pairs, bool) or not isinstance(accepted_pairs, int):
        raise TypeError("accepted_pairs must be an integer")
    if accepted_pairs <= 0:
        raise ValueError("accepted_pairs must be positive")
    return min(30, max(3, math.ceil(0.05 * accepted_pairs)))


def stage1_top_k(
    records: Iterable[PairRuntimeRecord],
    *,
    noise_p99_ms: float,
) -> tuple[str, ...]:
    """Merge three deterministic Stage-1 queues with unique contributions.

    Each queue may contribute at most ``ceil(K/3)`` unique pairs. A duplicate
    encountered in a later queue does not consume that queue's allocation.
    Query-timeout records are deliberately excluded for the timeout queue.
    """

    noise = _nonnegative_finite(noise_p99_ms, "noise_p99_ms")
    frozen = tuple(records)
    if not frozen:
        return ()
    seen_hashes: set[str] = set()
    ordinary: list[PairRuntimeRecord] = []
    for record in frozen:
        if not isinstance(record, PairRuntimeRecord):
            raise TypeError("Stage-1 screening requires PairRuntimeRecord values")
        if record.pair_hash in seen_hashes:
            raise ValueError("Stage-1 records contain a duplicate pair_hash")
        seen_hashes.add(record.pair_hash)
        if record.timeout_state in _TIMEOUT_STATES:
            continue
        if record.timeout_state != "none":
            raise ValueError("Stage-1 record has an unknown timeout state")
        _validate_ordinary_record(record)
        if record.runtime_direction is None:
            if (
                float(record.median_delta_ms) != 0.0
                or float(record.median_ratio) != 1.0
            ):
                raise ValueError(
                    "directionless Stage-1 record has a non-tie runtime summary"
                )
            continue
        if record.runtime_direction not in {"a", "b"}:
            raise ValueError("Stage-1 runtime direction is invalid")
        ordinary.append(record)
    if not ordinary:
        return ()

    k = min(stage1_k(len(frozen)), len(ordinary))
    allocation = math.ceil(k / 3)
    queues = (
        sorted(ordinary, key=lambda value: (-float(value.median_delta_ms), value.pair_hash)),
        sorted(
            (
                value
                for value in ordinary
                if float(value.median_delta_ms) >= 0.5 * noise
            ),
            key=lambda value: (-float(value.median_ratio), value.pair_hash),
        ),
        sorted(ordinary, key=lambda value: (-float(value.delta_over_noise), value.pair_hash)),
    )
    cursors = [0, 0, 0]
    contributions = [0, 0, 0]
    selected: list[str] = []
    selected_hashes: set[str] = set()
    while len(selected) < k:
        progressed = False
        for queue_index, queue in enumerate(queues):
            if len(selected) >= k:
                break
            if contributions[queue_index] >= allocation:
                continue
            while cursors[queue_index] < len(queue):
                record = queue[cursors[queue_index]]
                cursors[queue_index] += 1
                if record.pair_hash in selected_hashes:
                    continue
                selected.append(record.pair_hash)
                selected_hashes.add(record.pair_hash)
                contributions[queue_index] += 1
                progressed = True
                break
        if not progressed:
            break
    return tuple(selected)


def evaluate_stage2(
    evidence: RuntimeEvidence,
    *,
    noise_p99_ms: float,
) -> Stage2Decision:
    """Apply the fixed relative-or-absolute Stage-2 wide-recall gate."""

    noise = _nonnegative_finite(noise_p99_ms, "noise_p99_ms")
    if not isinstance(evidence, RuntimeEvidence):
        raise TypeError("Stage-2 screening requires RuntimeEvidence")
    if evidence.verdict is not RuntimeVerdict.PASS:
        return Stage2Decision(
            False,
            False,
            tuple(dict.fromkeys(("stage2_evidence_not_pass", *evidence.reason_codes))),
        )
    if evidence.reason_codes:
        reasons = tuple(
            dict.fromkeys(
                (
                    "stage1_direction_changed"
                    if "direction_mismatch" in evidence.reason_codes
                    else "stage2_evidence_has_reason_codes",
                    *evidence.reason_codes,
                )
            )
        )
        return Stage2Decision(False, False, reasons)

    ratio = _finite_required(evidence.median_ratio, "median_ratio")
    ratio_ci_low = _finite_required(evidence.ci_ratio_low, "ci_ratio_low")
    delta = _finite_required(evidence.median_delta_ms, "median_delta_ms")
    delta_ci_low = _finite_required(evidence.ci_delta_low_ms, "ci_delta_low_ms")
    direction = _finite_required(evidence.direction_consistency, "direction_consistency")
    if ratio <= 0:
        raise ValueError("Stage-2 median_ratio must be positive")
    if not 0.0 <= direction <= 1.0:
        raise ValueError("Stage-2 direction_consistency must be in [0, 1]")

    relative_checks = (
        (ratio >= 1.5, "relative_ratio_below_1_5"),
        (ratio_ci_low > 1.1, "relative_ci_low_not_above_1_1"),
        (delta > noise, "relative_delta_within_noise"),
        (direction >= 0.75, "relative_direction_below_0_75"),
    )
    absolute_checks = (
        (delta >= max(2.0 * noise, 10.0), "absolute_delta_below_wide_gate"),
        (delta_ci_low > 0.0, "absolute_delta_ci_not_positive"),
        (ratio > 1.1, "absolute_ratio_not_above_1_1"),
        (direction >= 0.75, "absolute_direction_below_0_75"),
    )
    relative_pass = all(passed for passed, _reason in relative_checks)
    absolute_pass = all(passed for passed, _reason in absolute_checks)
    reasons: list[str] = []
    if relative_pass:
        reasons.append("relative_wide_gate")
    else:
        reasons.extend(reason for passed, reason in relative_checks if not passed)
    if absolute_pass:
        reasons.append("absolute_wide_gate")
    else:
        reasons.extend(reason for passed, reason in absolute_checks if not passed)
    return Stage2Decision(relative_pass, absolute_pass, tuple(reasons))


def _validate_ordinary_record(record: PairRuntimeRecord) -> None:
    for name, value in (
        ("median_delta_ms", record.median_delta_ms),
        ("median_ratio", record.median_ratio),
        ("delta_over_noise", record.delta_over_noise),
        ("direction_consistency", record.direction_consistency),
    ):
        _finite_required(value, name)
    if float(record.median_delta_ms) < 0 or float(record.delta_over_noise) < 0:
        raise ValueError("Stage-1 delta scores must be non-negative")
    if float(record.median_ratio) < 1.0:
        raise ValueError("Stage-1 median_ratio must be at least one")
    if not 0.0 <= float(record.direction_consistency) <= 1.0:
        raise ValueError("Stage-1 direction_consistency must be in [0, 1]")
    # Task 6 validates this persisted derived field against the content-bound
    # noise artifact before screening. Keep this pure selector usable with
    # already-validated records and reject only malformed scalar domains here.


def _nonnegative_finite(value: float, name: str) -> float:
    result = _finite_required(value, name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _finite_required(value: float | None, name: str) -> float:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _timeout_record_from_payload(payload: object) -> TimeoutQueueRecord:
    if not isinstance(payload, dict):
        raise TypeError("timeout queue record must be an object")
    expected = {
        "content_hash",
        "dbms_fingerprint",
        "dbms_health_epoch",
        "dedupe_key",
        "entry_id",
        "evidence_digest",
        "graph_snapshot_hash",
        "noise_floor_id",
        "pair_hash",
        "schema_version",
        "stage",
        "timeout_side",
    }
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise ValueError("timeout queue record shape differs")
    return TimeoutQueueRecord(
        entry_id=str(payload["entry_id"]),
        pair_hash=str(payload["pair_hash"]),
        timeout_side=str(payload["timeout_side"]),
        stage=str(payload["stage"]),
        evidence_digest=str(payload["evidence_digest"]),
        graph_snapshot_hash=str(payload["graph_snapshot_hash"]),
        dbms_fingerprint=str(payload["dbms_fingerprint"]),
        dbms_health_epoch=str(payload["dbms_health_epoch"]),
        noise_floor_id=str(payload["noise_floor_id"]),
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _require_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 digest") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "Stage2Candidate",
    "Stage2Decision",
    "DurableTimeoutQueue",
    "TimeoutQueueRecord",
    "evaluate_stage2",
    "stage1_k",
    "stage1_top_k",
]
