"""Failure-isolating shard supervisor with durable recovery state."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from graph_sym_perf.core import to_jsonable
from graph_sym_perf.experiment.plan_models import RunPlan
from graph_sym_perf.experiment.process_monitor import ProcessMonitor, WorkerProcessError, bind_worker_attempt
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import (
    ErrorKind,
    ManifestMismatchError,
    ShardJournal,
    ShardState,
    _append_jsonl_fsync,
)
from graph_sym_perf.reliability.findings import ReliabilityFindingStore


@dataclass(frozen=True)
class RunSummary:
    completed: tuple[object, ...]
    incomplete: tuple[object, ...]
    failed: tuple[object, ...]


class ShardSupervisor:
    def __init__(
        self,
        run_dir: str | Path,
        *,
        retry_limit: int = 3,
        process_isolation: bool = False,
        hard_timeout_seconds: float | None = None,
        heartbeat_seconds: float = 10.0,
        stale_after_seconds: float = 90.0,
        max_parallel_generation_systems: int = 1,
        max_parallel_measurement_systems: int = 1,
        max_parallel_shards_per_system: int = 1,
    ) -> None:
        if retry_limit < 0 or retry_limit > 3:
            raise ValueError("retry_limit must be between 0 and 3")
        if heartbeat_seconds <= 0 or stale_after_seconds <= heartbeat_seconds:
            raise ValueError("stale_after_seconds must exceed heartbeat_seconds")
        if min(
            max_parallel_generation_systems,
            max_parallel_measurement_systems,
            max_parallel_shards_per_system,
        ) <= 0:
            raise ValueError("parallel scheduling limits must be positive")
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.retry_limit = retry_limit
        self.process_isolation = process_isolation
        self.hard_timeout_seconds = hard_timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_after_seconds = stale_after_seconds
        self.max_parallel_generation_systems = max_parallel_generation_systems
        self.max_parallel_measurement_systems = max_parallel_measurement_systems
        self.max_parallel_shards_per_system = max_parallel_shards_per_system
        self._event_lock = threading.Lock()
        self._reliability_lock = threading.Lock()

    def run(self, plan: RunPlan, worker: Callable[[object], dict]) -> RunSummary:
        return self._run(plan, worker, resume=False)

    def resume(self, plan: RunPlan, worker: Callable[[object], dict]) -> RunSummary:
        return self._run(plan, worker, resume=True)

    def _run(self, plan: RunPlan, worker: Callable[[object], dict], *, resume: bool) -> RunSummary:
        manifest_hash = _manifest_hash(plan)
        statuses: list[str | None] = [None] * len(plan.tasks)
        ordered_indices = sorted(
            range(len(plan.tasks)),
            key=lambda index: (int(getattr(plan.tasks[index], "shard_index", index)), index),
        )
        supports_phase_leases = callable(getattr(worker, "bind_attempt_context", None))
        max_workers = self.max_parallel_generation_systems if self.process_isolation and supports_phase_leases else 1
        if max_workers == 1:
            for index in ordered_indices:
                statuses[index] = self._run_task(plan.tasks[index], index, worker, manifest_hash, resume=resume)
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="shard-supervisor") as executor:
                futures = {
                    executor.submit(
                        self._run_task,
                        plan.tasks[index],
                        index,
                        worker,
                        manifest_hash,
                        resume=resume,
                    ): index
                    for index in ordered_indices
                }
                for future in as_completed(futures):
                    statuses[futures[future]] = future.result()
        return RunSummary(
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "completed"),
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "incomplete"),
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "failed"),
        )

    def _run_task(
        self,
        task: object,
        task_index: int,
        worker: Callable[[object], dict],
        manifest_hash: str,
        *,
        resume: bool,
    ) -> str:
        shard_dir = self._shard_dir(task, task_index)
        journal = ShardJournal(shard_dir, manifest_hash=manifest_hash)
        recovered_accepted = _artifact_hashes(shard_dir / "accepted_pairs.jsonl", "pair_hash")
        recovered_candidates = _artifact_hashes(shard_dir / "candidates.jsonl", "pair_hash")
        if recovered_accepted or recovered_candidates:
            journal.update_progress(
                attempt=max(0, _latest_attempt(shard_dir)),
                accepted_pair_hashes=recovered_accepted,
                candidate_hashes=recovered_candidates,
            )
        terminal_status = {
            ShardState.COMPLETED: "completed",
            ShardState.INCOMPLETE: "incomplete",
            ShardState.FAILED_FINAL: "failed",
        }.get(journal.state)
        if resume and terminal_status is not None:
            return terminal_status
        if resume and journal.state not in {ShardState.PLANNED, ShardState.FAILED_RETRYABLE}:
            journal.transition(
                ShardState.FAILED_RETRYABLE,
                attempt=_latest_attempt(shard_dir),
                error_kind=ErrorKind.WORKER_CRASHED,
                reason="supervisor resumed an interrupted shard attempt",
            )
        elif not resume and any(shard_dir.glob("attempt-*")):
            raise FileExistsError(f"shard already has attempt evidence; use resume: {shard_dir}")

        start_attempt = _latest_attempt(shard_dir) + 1 if resume else 0
        for attempt in range(start_attempt, self.retry_limit + 1):
            attempt_dir = shard_dir / f"attempt-{attempt:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            journal.transition(ShardState.GENERATING, attempt=attempt)
            self._aggregate_event(task, journal, attempt, ShardState.GENERATING)
            context = AttemptContext(
                attempt=attempt,
                attempt_dir=attempt_dir,
                shard_dir=shard_dir,
                manifest_hash=manifest_hash,
                target_key=_target_key(task, task_index),
                accepted_pair_hashes=journal.accepted_pair_hashes,
                candidate_hashes=journal.candidate_hashes,
                lease_root=self.run_dir / ".leases",
                max_parallel_generation_systems=self.max_parallel_generation_systems,
                max_parallel_measurement_systems=self.max_parallel_measurement_systems,
                max_parallel_shards_per_system=self.max_parallel_shards_per_system,
            )
            try:
                result = self._run_worker(worker, task, context)
            except WorkerProcessError as exc:
                journal = ShardJournal(shard_dir, manifest_hash=manifest_hash)
                self._record_failure(
                    task,
                    journal,
                    attempt,
                    exc.kind,
                    str(exc),
                    final=attempt >= self.retry_limit,
                    attempt_dir=attempt_dir,
                    inflight=exc.inflight,
                )
                if attempt >= self.retry_limit:
                    return "failed"
                continue
            except BaseException as exc:
                journal = ShardJournal(shard_dir, manifest_hash=manifest_hash)
                kind = _classify_exception(exc)
                self._record_failure(
                    task,
                    journal,
                    attempt,
                    kind,
                    str(exc),
                    final=attempt >= self.retry_limit,
                    attempt_dir=attempt_dir,
                )
                if attempt >= self.retry_limit:
                    return "failed"
                continue

            journal = ShardJournal(shard_dir, manifest_hash=manifest_hash)
            self._advance_success_states(task, journal, attempt)
            required_pairs = int(getattr(task, "accepted_query_pairs", 1000))
            accepted_pairs = int(result.get("accepted_pairs", 0))
            accepted_hashes = _result_hashes(result, "accepted_pair_hashes")
            candidate_hashes = _result_hashes(result, "candidate_hashes")
            if accepted_hashes is not None:
                journal.update_progress(
                    attempt=attempt,
                    accepted_pair_hashes=accepted_hashes,
                    candidate_hashes=candidate_hashes or (),
                    last_seed=_optional_task_seed(task),
                    generator_progress=_generator_progress(result),
                )
                if accepted_pairs != len(journal.accepted_pair_hashes):
                    self._record_failure(
                        task,
                        journal,
                        attempt,
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        "accepted-pair count disagrees with distinct content hashes",
                        final=True,
                    )
                    return "failed"
                accepted_pairs = len(journal.accepted_pair_hashes)
            if accepted_pairs < required_pairs:
                self._transition(task, journal, ShardState.INCOMPLETE, attempt)
                return "incomplete"
            if accepted_pairs == required_pairs:
                self._transition(task, journal, ShardState.COMPLETED, attempt)
                return "completed"
            self._record_failure(
                task,
                journal,
                attempt,
                ErrorKind.INFRASTRUCTURE_ERROR,
                f"invalid accepted-pair count: {accepted_pairs} != {required_pairs}",
                final=True,
            )
            return "failed"
        return "failed"

    def _advance_success_states(self, task: object, journal: ShardJournal, attempt: int) -> None:
        progress = (
            ShardState.GENERATING,
            ShardState.LOADING,
            ShardState.BUILDING_ACCEPTED_PAIRS,
            ShardState.SCREENING,
        )
        try:
            current_index = progress.index(journal.state)
        except ValueError as exc:
            raise RuntimeError(f"worker returned from invalid state {journal.state.value}") from exc
        for state in progress[current_index + 1 :]:
            self._transition(task, journal, state, attempt)

    def _record_failure(
        self,
        task: object,
        journal: ShardJournal,
        attempt: int,
        kind: ErrorKind,
        reason: str,
        *,
        final: bool,
        attempt_dir: Path | None = None,
        inflight: Mapping[str, object] | None = None,
    ) -> None:
        if attempt_dir is not None and (inflight or kind in {ErrorKind.WORKER_HUNG, ErrorKind.WORKER_CRASHED, ErrorKind.DBMS_CRASHED}):
            evidence = inflight or {}
            with self._reliability_lock:
                ReliabilityFindingStore(self.run_dir).record(
                    kind=kind,
                    query_hash=str(evidence.get("query_hash", "")),
                    graph_hash=str(evidence.get("graph_hash", "")),
                    dbms_fingerprint=str(evidence.get("dbms_fingerprint", "")),
                    dbms_version=str(evidence["dbms_version"]) if evidence.get("dbms_version") is not None else None,
                    attempt_evidence=str(attempt_dir.relative_to(self.run_dir)),
                )
        state = ShardState.FAILED_FINAL if final else ShardState.FAILED_RETRYABLE
        self._transition(task, journal, state, attempt, error_kind=kind, reason=reason)

    def _transition(
        self,
        task: object,
        journal: ShardJournal,
        state: ShardState,
        attempt: int,
        *,
        error_kind: ErrorKind | None = None,
        reason: str | None = None,
    ) -> None:
        journal.transition(state, attempt=attempt, error_kind=error_kind, reason=reason)
        self._aggregate_event(task, journal, attempt, state, error_kind=error_kind, reason=reason)

    def _aggregate_event(
        self,
        task: object,
        journal: ShardJournal,
        attempt: int,
        state: ShardState,
        *,
        error_kind: ErrorKind | None = None,
        reason: str | None = None,
    ) -> None:
        with self._event_lock:
            _append_jsonl_fsync(
                self.run_dir / "events.jsonl",
                {
                    "attempt": attempt,
                    "error_kind": error_kind.value if error_kind else None,
                    "reason": reason,
                    "seq": journal.event_seq,
                    "state": state.value,
                    "task": str(task),
                },
            )

    def _run_worker(self, worker: Callable[[object], dict], task: object, context: AttemptContext) -> dict:
        if not self.process_isolation:
            bind_worker_attempt(worker, context)
            result = worker(task)
            if not isinstance(result, dict):
                raise TypeError("worker must return a dictionary")
            return result
        return self._run_worker_in_process(worker, task, context)

    def _run_worker_in_process(self, worker: Callable[[object], dict], task: object, context: AttemptContext) -> dict:
        return ProcessMonitor(
            heartbeat_seconds=self.heartbeat_seconds,
            stale_after_seconds=self.stale_after_seconds,
            hard_timeout_seconds=self.hard_timeout_seconds,
        ).run(worker, task, context)

    def _shard_dir(self, task: object, task_index: int) -> Path:
        target = getattr(task, "target", None)
        target_value = getattr(target, "value", target)
        shard_index = getattr(task, "shard_index", task_index)
        if target_value is None:
            target_value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(task)).strip("-") or "task"
        return self.run_dir / "shards" / f"{target_value}-{int(shard_index):03d}"


def _manifest_hash(plan: RunPlan) -> str:
    payload = to_jsonable(plan)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _latest_attempt(shard_dir: Path) -> int:
    attempts = []
    for path in shard_dir.glob("attempt-*"):
        try:
            attempts.append(int(path.name.removeprefix("attempt-")))
        except ValueError:
            continue
    return max(attempts, default=-1)


def _artifact_hashes(path: Path, key: str) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    hashes: set[str] = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError(f"corrupt append-only artifact: {path}") from exc
        if isinstance(value, Mapping) and isinstance(value.get(key), str):
            hashes.add(str(value[key]))
    return hashes


def _target_key(task: object, task_index: int) -> str:
    target = getattr(task, "target", None)
    value = getattr(target, "value", target)
    if value is None:
        value = f"task-{task_index:03d}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value)).strip("-") or f"task-{task_index:03d}"


def _result_hashes(result: Mapping[str, object], key: str) -> set[str] | None:
    if key not in result:
        return None
    value = result[key]
    if not isinstance(value, list | tuple | set | frozenset) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a collection of strings")
    return set(value)


def _optional_task_seed(task: object) -> int | None:
    seed = getattr(task, "seed", None)
    return seed if isinstance(seed, int) and not isinstance(seed, bool) else None


def _generator_progress(result: Mapping[str, object]) -> Mapping[str, object]:
    value = result.get("generation", {})
    return value if isinstance(value, Mapping) else {}


def _classify_exception(exc: BaseException) -> ErrorKind:
    typed_kind = getattr(exc, "kind", None)
    if isinstance(typed_kind, ErrorKind):
        return typed_kind
    if isinstance(exc, TimeoutError):
        return ErrorKind.DRIVER_TIMEOUT
    if isinstance(exc, OSError):
        return ErrorKind.INFRASTRUCTURE_ERROR
    return ErrorKind.WORKER_CRASHED


__all__ = ["AttemptContext", "ManifestMismatchError", "RunSummary", "ShardSupervisor", "WorkerProcessError"]
