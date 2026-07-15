"""Durable, typed shard state and progress journal."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any


class ErrorKind(str, Enum):
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    DRIVER_TIMEOUT = "DRIVER_TIMEOUT"
    WORKER_HUNG = "WORKER_HUNG"
    WORKER_CRASHED = "WORKER_CRASHED"
    DBMS_UNAVAILABLE = "DBMS_UNAVAILABLE"
    DBMS_CRASHED = "DBMS_CRASHED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    EVIDENCE_INTEGRITY_ERROR = "EVIDENCE_INTEGRITY_ERROR"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


class EntryState(str, Enum):
    PLANNED = "planned"
    GENERATING = "generating"
    BUILDING_ACCEPTED_PAIRS = "building_accepted_pairs"
    STAGE1_COMPLETE = "stage1_complete"
    STAGE2_COMPLETE = "stage2_complete"
    VALIDATION_COMPLETE = "validation_complete"
    REDUCTION_COMPLETE = "reduction_complete"
    MATERIALIZED = "materialized"
    COMPLETED = "completed"
    UNDERFILLED = "underfilled"
    QUARANTINED = "quarantined"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"


class ShardState(str, Enum):
    PLANNED = "planned"
    GENERATING = "generating"
    LOADING = "loading"
    BUILDING_ACCEPTED_PAIRS = "building_accepted_pairs"
    SCREENING = "screening"
    VALIDATING = "validating"
    REDUCING = "reducing"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"


_ACTIVE = {
    ShardState.PLANNED,
    ShardState.GENERATING,
    ShardState.LOADING,
    ShardState.BUILDING_ACCEPTED_PAIRS,
    ShardState.SCREENING,
    ShardState.VALIDATING,
    ShardState.REDUCING,
}

_NEXT = {
    ShardState.PLANNED: {
        ShardState.GENERATING,
        ShardState.INCOMPLETE,
        ShardState.FAILED_RETRYABLE,
        ShardState.FAILED_FINAL,
    },
    ShardState.GENERATING: {ShardState.LOADING, ShardState.FAILED_RETRYABLE, ShardState.FAILED_FINAL},
    ShardState.LOADING: {ShardState.BUILDING_ACCEPTED_PAIRS, ShardState.FAILED_RETRYABLE, ShardState.FAILED_FINAL},
    ShardState.BUILDING_ACCEPTED_PAIRS: {
        ShardState.SCREENING,
        ShardState.INCOMPLETE,
        ShardState.FAILED_RETRYABLE,
        ShardState.FAILED_FINAL,
    },
    ShardState.SCREENING: {
        ShardState.VALIDATING,
        ShardState.INCOMPLETE,
        ShardState.FAILED_RETRYABLE,
        ShardState.FAILED_FINAL,
    },
    ShardState.VALIDATING: {
        ShardState.REDUCING,
        ShardState.INCOMPLETE,
        ShardState.FAILED_RETRYABLE,
        ShardState.FAILED_FINAL,
    },
    ShardState.REDUCING: {
        ShardState.COMPLETED,
        ShardState.INCOMPLETE,
        ShardState.FAILED_RETRYABLE,
        ShardState.FAILED_FINAL,
    },
    ShardState.FAILED_RETRYABLE: {
        ShardState.GENERATING,
        ShardState.INCOMPLETE,
        ShardState.FAILED_FINAL,
    },
}


class IllegalTransition(ValueError):
    pass


class ManifestMismatchError(ValueError):
    pass


class PersistenceError(OSError):
    pass


def can_transition(current: ShardState, target: ShardState) -> bool:
    return target in _NEXT.get(current, set())


def transition(current: ShardState, target: ShardState) -> ShardState:
    if not can_transition(current, target):
        raise IllegalTransition(f"illegal shard transition: {current.value} -> {target.value}")
    return target


class ShardJournal:
    """One shard's append-only events plus atomic best-known checkpoint."""

    def __init__(
        self,
        shard_dir: str | Path,
        *,
        manifest_hash: str,
        task_identity: str | None = None,
    ) -> None:
        self.shard_dir = Path(shard_dir)
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.shard_dir / "events.jsonl"
        self.checkpoint_path = self.shard_dir / "checkpoint.json"
        self.manifest_hash = manifest_hash
        self.task_identity = task_identity
        checkpoint = _read_mapping(self.checkpoint_path)
        if checkpoint and checkpoint.get("manifest_hash") != manifest_hash:
            raise ManifestMismatchError("checkpoint belongs to a different immutable manifest")
        if checkpoint and task_identity is not None and checkpoint.get("task_identity") != task_identity:
            raise ManifestMismatchError("checkpoint belongs to a different shard task")
        self._state = ShardState(str(checkpoint.get("state", ShardState.PLANNED.value)))
        self._event_seq = int(checkpoint.get("event_seq", 0))
        self._attempt = int(checkpoint.get("attempt", 0))
        self._accepted_pair_hashes = _string_set(checkpoint.get("accepted_pair_hashes", ()))
        self._candidate_hashes = _string_set(checkpoint.get("candidate_hashes", ()))
        self._last_seed = _optional_int(checkpoint.get("last_seed"))
        progress = checkpoint.get("generator_progress", {})
        self._generator_progress = dict(progress) if isinstance(progress, Mapping) else {}
        stage_evidence = checkpoint.get("stage_evidence", {})
        self._stage_evidence = dict(stage_evidence) if isinstance(stage_evidence, Mapping) else {}
        if self._replay_event_tail():
            self._write_checkpoint()

    @property
    def state(self) -> ShardState:
        return self._state

    @property
    def event_seq(self) -> int:
        return self._event_seq

    @property
    def accepted_pair_hashes(self) -> frozenset[str]:
        return frozenset(self._accepted_pair_hashes)

    @property
    def candidate_hashes(self) -> frozenset[str]:
        return frozenset(self._candidate_hashes)

    def transition(
        self,
        target: ShardState,
        *,
        attempt: int,
        error_kind: ErrorKind | None = None,
        reason: str | None = None,
    ) -> None:
        previous = self._state
        transition(previous, target)
        next_seq = self._event_seq + 1
        payload = {
            "attempt": attempt,
            "error_kind": error_kind.value if error_kind is not None else None,
            "manifest_hash": self.manifest_hash,
            "previous_state": previous.value,
            "reason": reason,
            "seq": next_seq,
            "state": target.value,
            "timestamp": time.time(),
        }
        if self.task_identity is not None:
            payload["task_identity"] = self.task_identity
        _append_jsonl_fsync(self.events_path, payload)
        self._write_checkpoint(state=target, event_seq=next_seq, attempt=attempt)
        self._state, self._event_seq, self._attempt = target, next_seq, attempt

    def update_progress(
        self,
        *,
        attempt: int,
        accepted_pair_hashes: Iterable[str] | None = None,
        candidate_hashes: Iterable[str] | None = None,
        last_seed: int | None = None,
        generator_progress: Mapping[str, object] | None = None,
        stage_evidence: Mapping[str, object] | None = None,
    ) -> None:
        next_accepted = set(self._accepted_pair_hashes)
        next_candidates = set(self._candidate_hashes)
        if accepted_pair_hashes is not None:
            next_accepted.update(accepted_pair_hashes)
        if candidate_hashes is not None:
            next_candidates.update(candidate_hashes)
        next_last_seed = last_seed if last_seed is not None else self._last_seed
        next_progress = dict(generator_progress) if generator_progress is not None else dict(self._generator_progress)
        next_stage_evidence = dict(stage_evidence) if stage_evidence is not None else dict(self._stage_evidence)
        self._write_checkpoint(
            attempt=attempt,
            accepted_pair_hashes=next_accepted,
            candidate_hashes=next_candidates,
            last_seed=next_last_seed,
            generator_progress=next_progress,
            stage_evidence=next_stage_evidence,
        )
        self._accepted_pair_hashes = next_accepted
        self._candidate_hashes = next_candidates
        self._last_seed = next_last_seed
        self._generator_progress = next_progress
        self._stage_evidence = next_stage_evidence
        self._attempt = attempt

    def _write_checkpoint(
        self,
        *,
        state: ShardState | None = None,
        event_seq: int | None = None,
        attempt: int | None = None,
        accepted_pair_hashes: set[str] | None = None,
        candidate_hashes: set[str] | None = None,
        last_seed: int | None = None,
        generator_progress: Mapping[str, object] | None = None,
        stage_evidence: Mapping[str, object] | None = None,
    ) -> None:
        payload = {
            "accepted_pair_hashes": sorted(
                self._accepted_pair_hashes if accepted_pair_hashes is None else accepted_pair_hashes
            ),
            "attempt": self._attempt if attempt is None else attempt,
            "candidate_hashes": sorted(self._candidate_hashes if candidate_hashes is None else candidate_hashes),
            "event_seq": self._event_seq if event_seq is None else event_seq,
            "generator_progress": self._generator_progress if generator_progress is None else dict(generator_progress),
            "last_seed": self._last_seed if last_seed is None else last_seed,
            "manifest_hash": self.manifest_hash,
            "stage_evidence": self._stage_evidence if stage_evidence is None else dict(stage_evidence),
            "state": (self._state if state is None else state).value,
        }
        if self.task_identity is not None:
            payload["task_identity"] = self.task_identity
        _atomic_write_json(self.checkpoint_path, payload)

    def _replay_event_tail(self) -> bool:
        replayed = False
        for event in _read_events(self.events_path):
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= self._event_seq:
                continue
            if seq != self._event_seq + 1:
                raise PersistenceError(f"event sequence gap in {self.events_path}")
            event_manifest = event.get("manifest_hash")
            if event_manifest not in {None, self.manifest_hash}:
                raise ManifestMismatchError("event stream belongs to a different immutable manifest")
            if self.task_identity is not None and event.get("task_identity") != self.task_identity:
                raise ManifestMismatchError("event stream belongs to a different shard task")
            try:
                previous = ShardState(str(event["previous_state"]))
                target = ShardState(str(event["state"]))
                attempt = int(event["attempt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(f"invalid event in {self.events_path}") from exc
            if previous is not self._state:
                raise PersistenceError(f"event previous_state mismatch in {self.events_path}")
            transition(self._state, target)
            self._state = target
            self._event_seq = seq
            self._attempt = attempt
            replayed = True
        return replayed


def _append_jsonl_fsync(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PersistenceError(f"failed to append durable event: {path}") from exc


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise PersistenceError(f"failed to atomically persist checkpoint: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"cannot read checkpoint: {path}") from exc
    if not isinstance(value, Mapping):
        raise PersistenceError(f"checkpoint is not a JSON object: {path}")
    return value


def _read_events(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        stream = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise PersistenceError(f"cannot read event stream: {path}") from exc
    events = []
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PersistenceError(f"invalid event JSON in {path}") from exc
            if not isinstance(value, Mapping):
                raise PersistenceError(f"event is not a JSON object: {path}")
            events.append(value)
    return tuple(events)


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return set()
    return {item for item in value if isinstance(item, str)}


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
