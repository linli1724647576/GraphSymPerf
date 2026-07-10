"""Shared strict validation for persisted shard evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from graph_sym_perf.experiment.state import ErrorKind, ShardState, can_transition


CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def canonical_hash(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=repr,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON evidence must be an object: {path}")
    return value


def artifact_hashes(path: Path, key: str, *, required: bool = False) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        if required:
            raise OSError(f"required append-only artifact is missing: {path}")
        return set()
    hashes: set[str] = set()
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError(f"corrupt append-only artifact: {path}") from exc
        if not isinstance(record, Mapping):
            raise OSError(f"append-only artifact record is not an object: {path}")
        value = record.get(key)
        if not isinstance(value, str) or CONTENT_HASH.fullmatch(value) is None:
            raise OSError(f"append-only artifact has invalid {key}: {path}")
        if value in hashes:
            raise OSError(f"append-only artifact contains duplicate {key}: {path}")
        hashes.add(value)
    return hashes


def validate_failure_evidence(
    path: Path,
    *,
    manifest_hash: str,
    task_identity: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    failure = read_json_object(path)
    if (
        failure.get("manifest_hash") != manifest_hash
        or failure.get("task_identity") != task_identity
        or failure.get("state") != ShardState.FAILED_FINAL.value
        or not isinstance(failure.get("reason"), str)
    ):
        raise ValueError("failure evidence does not match immutable manifest task")
    try:
        ErrorKind(str(failure.get("error_kind")))
    except ValueError as exc:
        raise ValueError("failure evidence has an unknown typed error kind") from exc
    return failure


def validate_shard_evidence(
    shard_dir: Path,
    checkpoint: Mapping[str, object],
    *,
    manifest_hash: str,
    task_identity: str,
    accepted_target: int,
) -> ShardState:
    if checkpoint.get("manifest_hash") != manifest_hash or checkpoint.get("task_identity") != task_identity:
        raise ValueError("checkpoint identity does not match manifest task")
    _strict_int(checkpoint.get("attempt"))
    accepted = _hash_list(checkpoint.get("accepted_pair_hashes"), field="accepted_pair_hashes")
    candidates = _hash_list(checkpoint.get("candidate_hashes"), field="candidate_hashes")
    states = _validated_event_states(
        shard_dir / "events.jsonl",
        manifest_hash=manifest_hash,
        task_identity=task_identity,
        checkpoint=checkpoint,
    )
    state = ShardState(str(checkpoint.get("state")))
    artifact_accepted = artifact_hashes(
        shard_dir / "accepted_pairs.jsonl",
        "pair_hash",
        required=bool(accepted) or state in {ShardState.COMPLETED, ShardState.INCOMPLETE},
    )
    artifact_candidates = artifact_hashes(
        shard_dir / "candidates.jsonl",
        "pair_hash",
        required=bool(candidates),
    )
    if accepted != artifact_accepted or candidates != artifact_candidates:
        raise ValueError("checkpoint hashes do not match canonical shard artifacts")
    stages = _validate_stage_evidence(shard_dir, checkpoint)
    if state is ShardState.COMPLETED:
        if len(accepted) != accepted_target:
            raise ValueError("completed shard accepted count does not match manifest target")
        required_tail = [ShardState.VALIDATING, ShardState.REDUCING, ShardState.COMPLETED]
        if states[-3:] != required_tail or not {"screening", "validation", "reduction"}.issubset(stages):
            raise ValueError("completed shard lacks the durable validation/reduction chain")
    return state


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected an integer")
    return value


def _hash_list(value: object, *, field: str) -> set[str]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    if not all(isinstance(item, str) and CONTENT_HASH.fullmatch(item) is not None for item in value):
        raise ValueError(f"{field} must contain lowercase 64-hex strings")
    hashes = set(value)
    if len(hashes) != len(value):
        raise ValueError(f"{field} must not contain duplicates")
    return hashes


def _validated_event_states(
    path: Path,
    *,
    manifest_hash: str,
    task_identity: str,
    checkpoint: Mapping[str, object],
) -> list[ShardState]:
    expected_previous = ShardState.PLANNED
    states: list[ShardState] = []
    expected_seq = 1
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, Mapping):
                raise TypeError("event must be an object")
            if event.get("manifest_hash") != manifest_hash or event.get("task_identity") != task_identity:
                raise ValueError("event identity does not match manifest task")
            if _strict_int(event.get("seq")) != expected_seq:
                raise ValueError("event sequence is not contiguous")
            previous = ShardState(str(event.get("previous_state")))
            state = ShardState(str(event.get("state")))
            if previous is not expected_previous or not can_transition(previous, state):
                raise ValueError("event state chain is invalid")
            _strict_int(event.get("attempt"))
            states.append(state)
            expected_previous = state
            expected_seq += 1
    checkpoint_seq = _strict_int(checkpoint.get("event_seq"))
    checkpoint_state = ShardState(str(checkpoint.get("state")))
    if checkpoint_seq != len(states):
        raise ValueError("checkpoint event sequence does not match event stream")
    if states:
        if states[-1] is not checkpoint_state:
            raise ValueError("checkpoint state does not match event stream")
    elif checkpoint_state is not ShardState.PLANNED:
        raise ValueError("non-planned checkpoint has no event stream")
    return states


def _validate_stage_evidence(shard_dir: Path, checkpoint: Mapping[str, object]) -> set[str]:
    evidence = checkpoint.get("stage_evidence", {})
    if not isinstance(evidence, Mapping):
        raise TypeError("checkpoint stage_evidence must be an object")
    verified: set[str] = set()
    root = shard_dir.resolve()
    for stage, reference in evidence.items():
        if stage not in {"screening", "validation", "reduction"} or not isinstance(reference, Mapping):
            raise ValueError("invalid stage evidence reference")
        relative = reference.get("path")
        digest = reference.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or CONTENT_HASH.fullmatch(digest) is None:
            raise ValueError("invalid stage evidence path or digest")
        path = (shard_dir / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("stage evidence escapes shard directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("stage evidence digest mismatch")
        verified.add(str(stage))
    return verified


__all__ = [
    "CONTENT_HASH",
    "artifact_hashes",
    "canonical_hash",
    "read_json_object",
    "validate_failure_evidence",
    "validate_shard_evidence",
]
