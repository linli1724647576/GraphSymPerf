"""Formal run lifecycle around an immutable plan and isolated shard workers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from graph_sym_perf.experiment.plan_models import ExperimentSpec
from graph_sym_perf.experiment.planner import build_run_plan, resolved_manifest_payload, write_resolved_manifest
from graph_sym_perf.experiment.state import ManifestMismatchError, ShardState, can_transition
from graph_sym_perf.experiment.supervisor import RunSummary, ShardSupervisor


@dataclass(frozen=True)
class LifecycleResult:
    run_id: str
    run_dir: Path
    summary: RunSummary


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    total: int
    completed: int
    incomplete: int
    failed: int
    active: int
    invalid: int


class ExperimentLifecycle:
    def __init__(
        self,
        output_root: str | Path = "runs",
        *,
        process_isolation: bool = True,
        hard_timeout_seconds: float | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.process_isolation = process_isolation
        self.hard_timeout_seconds = hard_timeout_seconds

    def run(self, spec: ExperimentSpec, worker: Callable[[object], dict], *, run_id: str | None = None) -> LifecycleResult:
        plan = build_run_plan(spec)
        identifier = run_id or f"{spec.name}-{uuid4().hex[:12]}"
        run_dir = self.output_root / identifier
        if run_dir.exists():
            raise FileExistsError(f"run id already exists: {identifier}")
        run_dir.mkdir(parents=True)
        write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec),
        ).run(plan, worker)
        self._write_summary(identifier, run_dir, summary)
        return LifecycleResult(identifier, run_dir, summary)

    def resume(
        self,
        spec: ExperimentSpec,
        worker: Callable[[object], dict],
        *,
        run_id: str,
    ) -> LifecycleResult:
        run_dir = self.output_root / run_id
        if not (run_dir / "resolved_manifest.json").is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        plan = build_run_plan(spec)
        stored_manifest = json.loads((run_dir / "resolved_manifest.json").read_text(encoding="utf-8"))
        if stored_manifest != resolved_manifest_payload(plan):
            raise ManifestMismatchError("run resume refused: immutable manifest does not match current plan")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec),
        ).resume(plan, worker)
        self._write_summary(run_id, run_dir, summary)
        return LifecycleResult(run_id, run_dir, summary)

    def status(self, run_id: str) -> RunStatus:
        run_dir = self.output_root / run_id
        manifest_path = run_dir / "resolved_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
            raise ValueError("resolved manifest must contain an exact task list")
        tasks = manifest["tasks"]
        total = len(tasks)
        manifest_hash = _canonical_hash(manifest)
        counts = {"completed": 0, "incomplete": 0, "failed_final": 0, "active": 0, "invalid": 0}
        expected_dirs: set[Path] = set()
        for task in tasks:
            if not isinstance(task, dict):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            try:
                shard_dir = run_dir / "shards" / _manifest_shard_name(task)
                task_identity = _canonical_hash(task)
            except (KeyError, TypeError, ValueError):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            if shard_dir in expected_dirs:
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            expected_dirs.add(shard_dir)
            checkpoint_path = shard_dir / "checkpoint.json"
            if not checkpoint_path.is_file():
                counts["active"] += 1
                continue
            try:
                checkpoint = _read_json_object(checkpoint_path)
                state = _validate_shard_evidence(
                    shard_dir,
                    checkpoint,
                    manifest_hash=manifest_hash,
                    task_identity=task_identity,
                    accepted_target=_strict_int(task["accepted_query_pairs"]),
                )
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            if state in {"completed", "incomplete", "failed_final"}:
                counts[state] += 1
            elif state in {
                "planned",
                "generating",
                "loading",
                "building_accepted_pairs",
                "screening",
                "validating",
                "reducing",
                "failed_retryable",
            }:
                counts["active"] += 1
            else:
                counts["failed_final"] += 1
                counts["invalid"] += 1
        for extra in (run_dir / "shards").glob("*"):
            if extra.is_dir() and extra not in expected_dirs and (extra / "checkpoint.json").exists():
                counts["invalid"] += 1
        return RunStatus(
            run_id=run_id,
            total=total,
            completed=counts["completed"],
            incomplete=counts["incomplete"],
            failed=counts["failed_final"],
            active=counts["active"],
            invalid=counts["invalid"],
        )

    def _supervisor_options(self, spec: ExperimentSpec) -> dict[str, object]:
        return {
            "retry_limit": int(spec.scheduling["shard_retry_limit"]),
            "process_isolation": self.process_isolation,
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "heartbeat_seconds": float(spec.scheduling["heartbeat_seconds"]),
            "stale_after_seconds": float(spec.scheduling["stale_after_seconds"]),
            "max_parallel_generation_systems": int(spec.scheduling["max_parallel_generation_systems"]),
            "max_parallel_measurement_systems": int(spec.scheduling["max_parallel_measurement_systems"]),
            "max_parallel_shards_per_system": int(spec.scheduling["max_parallel_shards_per_system"]),
        }

    @staticmethod
    def _write_summary(identifier: str, run_dir: Path, summary: RunSummary) -> None:
        (run_dir / "run_summary.json").write_text(
            json.dumps(
                {
                    "run_id": identifier,
                    "completed_count": len(summary.completed),
                    "incomplete_count": len(summary.incomplete),
                    "failed_count": len(summary.failed),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


__all__ = ["ExperimentLifecycle", "LifecycleResult", "RunStatus"]


_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected an integer")
    return value


def _manifest_shard_name(task: dict[str, object]) -> str:
    target = task["target"]
    shard_index = _strict_int(task["shard_index"])
    if not isinstance(target, str) or not target:
        raise TypeError("manifest task target must be a non-empty string")
    safe_target = re.sub(r"[^a-zA-Z0-9_.-]+", "-", target).strip("-")
    if not safe_target:
        raise ValueError("manifest task target has no safe shard name")
    return f"{safe_target}-{shard_index:03d}"


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON evidence must be an object: {path}")
    return value


def _hash_list(value: object, *, field: str) -> set[str]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    if not all(isinstance(item, str) and _CONTENT_HASH.fullmatch(item) is not None for item in value):
        raise ValueError(f"{field} must contain lowercase 64-hex strings")
    hashes = set(value)
    if len(hashes) != len(value):
        raise ValueError(f"{field} must not contain duplicates")
    return hashes


def _artifact_hashes(path: Path, key: str, *, required: bool) -> set[str]:
    if not path.is_file():
        if required:
            raise OSError(f"missing shard artifact: {path}")
        return set()
    hashes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"artifact record must be an object: {path}")
        value = record.get(key)
        if not isinstance(value, str) or _CONTENT_HASH.fullmatch(value) is None:
            raise ValueError(f"artifact has invalid {key}: {path}")
        if value in hashes:
            raise ValueError(f"artifact has duplicate {key}: {path}")
        hashes.add(value)
    return hashes


def _validated_event_states(
    path: Path,
    *,
    manifest_hash: str,
    task_identity: str,
    checkpoint: dict[str, object],
) -> list[ShardState]:
    expected_previous = ShardState.PLANNED
    states: list[ShardState] = []
    expected_seq = 1
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
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


def _validate_stage_evidence(shard_dir: Path, checkpoint: dict[str, object]) -> set[str]:
    evidence = checkpoint.get("stage_evidence", {})
    if not isinstance(evidence, dict):
        raise TypeError("checkpoint stage_evidence must be an object")
    verified: set[str] = set()
    root = shard_dir.resolve()
    for stage, reference in evidence.items():
        if stage not in {"screening", "validation", "reduction"} or not isinstance(reference, dict):
            raise ValueError("invalid stage evidence reference")
        relative = reference.get("path")
        digest = reference.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or _CONTENT_HASH.fullmatch(digest) is None:
            raise ValueError("invalid stage evidence path or digest")
        path = (shard_dir / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("stage evidence escapes shard directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("stage evidence digest mismatch")
        verified.add(stage)
    return verified


def _validate_shard_evidence(
    shard_dir: Path,
    checkpoint: dict[str, object],
    *,
    manifest_hash: str,
    task_identity: str,
    accepted_target: int,
) -> str:
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
    artifact_accepted = _artifact_hashes(
        shard_dir / "accepted_pairs.jsonl",
        "pair_hash",
        required=bool(accepted) or state in {ShardState.COMPLETED, ShardState.INCOMPLETE},
    )
    artifact_candidates = _artifact_hashes(
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
    return state.value
