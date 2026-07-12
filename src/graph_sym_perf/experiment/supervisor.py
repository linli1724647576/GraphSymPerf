"""Failure-isolating shard supervisor with durable recovery state."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from graph_sym_perf.artifacts.store import enforce_shard_budget
from graph_sym_perf.core import to_jsonable
from graph_sym_perf.experiment.evidence_validation import (
    CONTENT_HASH,
    artifact_hashes,
    candidate_artifact_hashes,
    canonical_hash,
    read_json_object,
    validate_failure_evidence,
    validate_shard_evidence,
)
from graph_sym_perf.experiment.plan_models import RunPlan
from graph_sym_perf.experiment.planner import resolved_manifest_hash
from graph_sym_perf.experiment.process_monitor import ProcessMonitor, WorkerProcessError, bind_worker_attempt
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import (
    ErrorKind,
    ManifestMismatchError,
    ShardJournal,
    ShardState,
    _append_jsonl_fsync,
    _atomic_write_json,
    can_transition,
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
        shard_budget_bytes: int = 500 * 1024 * 1024,
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
        if shard_budget_bytes <= 64 * 1024:
            raise ValueError("shard budget must leave at least 64 KiB for terminal evidence")
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
        self.shard_budget_bytes = shard_budget_bytes
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
                statuses[index] = self._run_task_guarded(plan.tasks[index], index, worker, manifest_hash, resume=resume)
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="shard-supervisor") as executor:
                futures = {
                    executor.submit(
                        self._run_task_guarded,
                        plan.tasks[index],
                        index,
                        worker,
                        manifest_hash,
                        resume=resume,
                    ): index
                    for index in ordered_indices
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        statuses[index] = future.result()
                    except ManifestMismatchError:
                        raise
                    except BaseException as exc:
                        statuses[index] = self._fail_unhandled_task(plan.tasks[index], index, manifest_hash, exc)
        return RunSummary(
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "completed"),
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "incomplete"),
            tuple(task for task, status in zip(plan.tasks, statuses, strict=True) if status == "failed"),
        )

    def _run_task_guarded(
        self,
        task: object,
        task_index: int,
        worker: Callable[[object], dict],
        manifest_hash: str,
        *,
        resume: bool,
    ) -> str:
        try:
            return self._run_task(task, task_index, worker, manifest_hash, resume=resume)
        except ManifestMismatchError:
            raise
        except BaseException as exc:
            return self._fail_unhandled_task(task, task_index, manifest_hash, exc)

    def _fail_unhandled_task(
        self,
        task: object,
        task_index: int,
        manifest_hash: str,
        exc: BaseException,
    ) -> str:
        shard_dir = self._shard_dir(task, task_index)
        task_identity = _task_identity(task)
        reason = f"unhandled shard infrastructure error: {type(exc).__name__}: {exc}"
        try:
            journal = ShardJournal(shard_dir, manifest_hash=manifest_hash, task_identity=task_identity)
            if journal.state is ShardState.FAILED_FINAL:
                return "failed"
            if can_transition(journal.state, ShardState.FAILED_FINAL):
                self._record_failure(
                    task,
                    journal,
                    max(0, _latest_attempt(shard_dir)),
                    ErrorKind.INFRASTRUCTURE_ERROR,
                    reason,
                    final=True,
                )
                return "failed"
        except ManifestMismatchError:
            raise
        except BaseException as journal_exc:
            reason = f"{reason}; journal unavailable: {type(journal_exc).__name__}: {journal_exc}"
        self._persist_isolated_failure(shard_dir, manifest_hash, task_identity, reason)
        return "failed"

    def _persist_isolated_failure(
        self,
        shard_dir: Path,
        manifest_hash: str,
        task_identity: str,
        reason: str,
    ) -> None:
        payload = {
            "error_kind": ErrorKind.INFRASTRUCTURE_ERROR.value,
            "manifest_hash": manifest_hash,
            "reason": reason,
            "state": ShardState.FAILED_FINAL.value,
            "task_identity": task_identity,
            "timestamp": time.time(),
        }
        try:
            _atomic_write_json(shard_dir / "failure.json", payload)
        except OSError:
            pass

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
        task_identity = _task_identity(task)
        if validate_failure_evidence(
            shard_dir / "failure.json",
            manifest_hash=manifest_hash,
            task_identity=task_identity,
        ) is not None:
            return "failed"
        journal = ShardJournal(shard_dir, manifest_hash=manifest_hash, task_identity=task_identity)
        terminal_status = {
            ShardState.COMPLETED: "completed",
            ShardState.INCOMPLETE: "incomplete",
            ShardState.FAILED_FINAL: "failed",
        }.get(journal.state)
        if resume and terminal_status is not None:
            self._enforce_shard_artifact_budget(shard_dir)
            checkpoint = read_json_object(journal.checkpoint_path)
            validate_shard_evidence(
                shard_dir,
                checkpoint,
                manifest_hash=manifest_hash,
                task_identity=task_identity,
                accepted_target=int(getattr(task, "accepted_query_pairs", 1000)),
            )
            return terminal_status
        recovered_accepted = artifact_hashes(shard_dir / "accepted_pairs.jsonl", "pair_hash")
        recovered_candidates = candidate_artifact_hashes(shard_dir)
        if recovered_accepted or recovered_candidates:
            journal.update_progress(
                attempt=max(0, _latest_attempt(shard_dir)),
                accepted_pair_hashes=recovered_accepted,
                candidate_hashes=recovered_candidates,
            )
        if resume:
            latest_attempt = _latest_attempt(shard_dir)
            if latest_attempt >= self.retry_limit and journal.state is not ShardState.PLANNED:
                self._transition(
                    task,
                    journal,
                    ShardState.FAILED_FINAL,
                    latest_attempt,
                    error_kind=ErrorKind.WORKER_CRASHED,
                    reason="resume found an interrupted final attempt with no retries remaining",
                )
                return "failed"
            if journal.state not in {ShardState.PLANNED, ShardState.FAILED_RETRYABLE}:
                self._transition(
                    task,
                    journal,
                    ShardState.FAILED_RETRYABLE,
                    latest_attempt,
                    error_kind=ErrorKind.WORKER_CRASHED,
                    reason="supervisor resumed an interrupted shard attempt",
                )
        elif not resume and any(shard_dir.glob("attempt-*")):
            raise FileExistsError(f"shard already has attempt evidence; use resume: {shard_dir}")

        start_attempt = _latest_attempt(shard_dir) + 1 if resume else 0
        for attempt in range(start_attempt, self.retry_limit + 1):
            self._enforce_shard_artifact_budget(shard_dir)
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
                task_identity=task_identity,
            )
            try:
                result = self._run_worker(worker, task, context)
            except WorkerProcessError as exc:
                journal = ShardJournal(shard_dir, manifest_hash=manifest_hash, task_identity=task_identity)
                final = attempt >= self.retry_limit or not _is_retryable_error(exc.kind)
                self._record_failure(
                    task,
                    journal,
                    attempt,
                    exc.kind,
                    str(exc),
                    final=final,
                    attempt_dir=attempt_dir,
                    inflight=exc.inflight,
                )
                self._enforce_shard_artifact_budget(shard_dir)
                if final:
                    return "failed"
                continue
            except BaseException as exc:
                journal = ShardJournal(shard_dir, manifest_hash=manifest_hash, task_identity=task_identity)
                kind = _classify_exception(exc)
                final = attempt >= self.retry_limit or not _is_retryable_error(kind)
                self._record_failure(
                    task,
                    journal,
                    attempt,
                    kind,
                    str(exc),
                    final=final,
                    attempt_dir=attempt_dir,
                )
                self._enforce_shard_artifact_budget(shard_dir)
                if final:
                    return "failed"
                continue

            journal = ShardJournal(shard_dir, manifest_hash=manifest_hash, task_identity=task_identity)
            self._enforce_shard_artifact_budget(shard_dir)
            self._advance_success_states(task, journal, attempt)
            required_pairs = int(getattr(task, "accepted_query_pairs", 1000))
            accepted_pairs = int(result.get("accepted_pairs", 0))
            accepted_hashes = _result_hashes(result, "accepted_pair_hashes", required=True)
            candidate_hashes = _result_hashes(result, "candidate_hashes", required=False) or set()
            canonical_hashes = artifact_hashes(
                shard_dir / "accepted_pairs.jsonl",
                "pair_hash",
                required=True,
            )
            canonical_candidates = candidate_artifact_hashes(
                shard_dir,
                required=bool(candidate_hashes),
            )
            if accepted_hashes != canonical_hashes:
                raise ValueError("accepted result hashes do not match canonical artifact hashes")
            if candidate_hashes != canonical_candidates:
                raise ValueError("candidate result hashes do not match content-addressed candidate artifacts")
            journal.update_progress(
                attempt=attempt,
                accepted_pair_hashes=accepted_hashes,
                candidate_hashes=candidate_hashes,
                last_seed=_optional_task_seed(task),
                generator_progress=_generator_progress(result),
            )
            if journal.accepted_pair_hashes != frozenset(canonical_hashes):
                raise ValueError("checkpoint accepted hashes do not match canonical artifact hashes")
            if accepted_pairs != len(accepted_hashes):
                raise ValueError("accepted-pair count disagrees with unique content hashes")
            accepted_pairs = len(accepted_hashes)
            if accepted_pairs < required_pairs:
                self._transition(task, journal, ShardState.INCOMPLETE, attempt)
                self._enforce_shard_artifact_budget(shard_dir)
                return "incomplete"
            if accepted_pairs == required_pairs:
                stages = _verified_stage_evidence(result, shard_dir)
                journal.update_progress(attempt=attempt, stage_evidence=stages)
                if "reduction" in stages and "validation" not in stages:
                    raise ValueError("reduction evidence exists without validation evidence")
                if "validation" not in stages:
                    self._transition(task, journal, ShardState.INCOMPLETE, attempt)
                    self._enforce_shard_artifact_budget(shard_dir)
                    return "incomplete"
                self._transition(task, journal, ShardState.VALIDATING, attempt)
                if "reduction" not in stages:
                    self._transition(task, journal, ShardState.INCOMPLETE, attempt)
                    self._enforce_shard_artifact_budget(shard_dir)
                    return "incomplete"
                self._transition(task, journal, ShardState.REDUCING, attempt)
                if "screening" not in stages:
                    raise ValueError("completed shard is missing durable screening evidence")
                self._transition(task, journal, ShardState.COMPLETED, attempt)
                self._enforce_shard_artifact_budget(shard_dir)
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

    def _enforce_shard_artifact_budget(self, shard_dir: Path) -> None:
        report = enforce_shard_budget(
            shard_dir,
            budget_bytes=self.shard_budget_bytes,
            nonessential_paths=tuple(sorted(shard_dir.glob("attempt-*/gdsmith_raw"))),
            reserve_bytes=64 * 1024,
        )
        for warning in report.warnings:
            _append_jsonl_fsync(shard_dir / "artifact_warnings.jsonl", {"warning": warning})

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
        if attempt_dir is not None and kind is ErrorKind.QUERY_TIMEOUT and inflight:
            query_hash = inflight.get("query_hash")
            if isinstance(query_hash, str) and CONTENT_HASH.fullmatch(query_hash) is not None:
                _append_jsonl_fsync(
                    attempt_dir.parent / "query_timeout_quarantine.jsonl",
                    {
                        "attempt": attempt,
                        "dbms_fingerprint": str(inflight.get("dbms_fingerprint", "")),
                        "error_kind": kind.value,
                        "graph_hash": str(inflight.get("graph_hash", "")),
                        "query_hash": query_hash,
                    },
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
    return resolved_manifest_hash(plan)


def _task_identity(task: object) -> str:
    return canonical_hash(to_jsonable(task))


def _latest_attempt(shard_dir: Path) -> int:
    attempts = []
    for path in shard_dir.glob("attempt-*"):
        try:
            attempts.append(int(path.name.removeprefix("attempt-")))
        except ValueError:
            continue
    return max(attempts, default=-1)


def _target_key(task: object, task_index: int) -> str:
    target = getattr(task, "target", None)
    value = getattr(target, "value", target)
    if value is None:
        value = f"task-{task_index:03d}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value)).strip("-") or f"task-{task_index:03d}"


def _result_hashes(result: Mapping[str, object], key: str, *, required: bool) -> set[str] | None:
    if key not in result:
        if required:
            raise ValueError(f"{key} is required")
        return None
    value = result[key]
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) and CONTENT_HASH.fullmatch(item) is not None for item in value
    ):
        raise TypeError(f"{key} must be a list of lowercase 64-hex strings")
    hashes = set(value)
    if len(hashes) != len(value):
        raise ValueError(f"{key} must not contain duplicates")
    return hashes


def _verified_stage_evidence(result: Mapping[str, object], shard_dir: Path) -> dict[str, dict[str, str]]:
    raw = result.get("stage_evidence", {})
    if not isinstance(raw, Mapping):
        raise TypeError("stage_evidence must be a mapping")
    verified: dict[str, dict[str, str]] = {}
    root = shard_dir.resolve()
    for stage, reference in raw.items():
        if stage not in {"screening", "validation", "reduction"} or not isinstance(reference, Mapping):
            raise ValueError("invalid stage evidence reference")
        relative = reference.get("path")
        expected = reference.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str) or CONTENT_HASH.fullmatch(expected) is None:
            raise ValueError("invalid stage evidence path or hash")
        path = (shard_dir / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("stage evidence path escapes shard directory")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OSError(f"cannot read durable {stage} evidence") from exc
        if actual != expected:
            raise ValueError(f"durable {stage} evidence hash mismatch")
        verified[str(stage)] = {"path": relative, "sha256": expected}
    return verified


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
    if isinstance(exc, ValueError):
        return ErrorKind.EVIDENCE_INTEGRITY_ERROR
    return ErrorKind.WORKER_CRASHED


def _is_retryable_error(kind: ErrorKind) -> bool:
    return kind is not ErrorKind.EVIDENCE_INTEGRITY_ERROR


__all__ = ["AttemptContext", "ManifestMismatchError", "RunSummary", "ShardSupervisor", "WorkerProcessError"]
