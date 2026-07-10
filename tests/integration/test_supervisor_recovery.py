import json
import hashlib
import os
import signal
import shutil
import time

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import RunPlan, ShardTask, SourceRevision
from graph_sym_perf.experiment.state import ShardJournal, ShardState
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle
from graph_sym_perf.experiment.planner import write_resolved_manifest
from graph_sym_perf.experiment.supervisor import (
    AttemptContext,
    ManifestMismatchError,
    ShardSupervisor,
    _manifest_hash,
    _task_identity,
)
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.experiment.worker import WorkerFailure
from graph_sym_perf.experiment.state import ErrorKind


def _hash_for(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _durable_result(
    context: AttemptContext,
    task,
    *,
    accepted_count: int | None = None,
    accepted_hashes: list[str] | None = None,
):
    count = accepted_count if accepted_count is not None else int(getattr(task, "accepted_query_pairs", 1000))
    task_label = getattr(getattr(task, "target", None), "value", str(task))
    hashes = accepted_hashes or [
        _hash_for(f"{task_label}:{getattr(task, 'shard_index', 0)}:{index}") for index in range(count)
    ]
    count = len(hashes)
    accepted_path = context.shard_dir / "accepted_pairs.jsonl"
    accepted_path.write_text("".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes), encoding="utf-8")
    stages = {}
    for stage in ("screening", "validation", "reduction"):
        path = context.attempt_dir / f"{stage}.json"
        path.write_text(json.dumps({"stage": stage, "task": task_label}), encoding="utf-8")
        stages[stage] = {
            "path": str(path.relative_to(context.shard_dir)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {"accepted_pairs": count, "accepted_pair_hashes": hashes, "stage_evidence": stages}


class _CompleteWorker:
    def __init__(self, *, fail_once_for=None, accepted_count=None) -> None:
        self.context = None
        self.fail_once_for = fail_once_for
        self.failed = False
        self.accepted_count = accepted_count

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        if task == self.fail_once_for and not self.failed:
            self.failed = True
            raise RuntimeError("retryable")
        assert self.context is not None
        count = self.accepted_count(task) if callable(self.accepted_count) else self.accepted_count
        return _durable_result(self.context, task, accepted_count=count)


def test_retryable_shard_failure_does_not_stop_other_tasks(tmp_path) -> None:
    tasks = (
        ShardTask.__new__(ShardTask),
    )
    # Use small stand-in tasks: supervisor only needs a stable task identity.
    plan = RunPlan("test", 2, ("bad", "good"), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, _CompleteWorker(fail_once_for="bad"))
    assert summary.completed == ("bad", "good")


def test_supervisor_uses_task_specific_accepted_pair_target(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 3

    plan = RunPlan("test", 2, (Task(),), 3, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path).run(plan, _CompleteWorker())
    assert len(summary.completed) == 1


class _Adapter:
    target = TargetSystem.NEO4J

    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        return ExecutionResult(
            system=self.target,
            query_kind=query_kind,
            rows=({"n": 1},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )

    def ensure_schema(self) -> None:
        return None

    def load_gdsmith_graph(self, _statements) -> None:
        return None

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint("a" * 64)

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch("e" * 64, "test")

    def close(self) -> None:
        return None


def test_retry_uses_a_new_attempt_directory_instead_of_reusing_the_shard_directory(tmp_path) -> None:
    calls = 0

    def generation_runner(_task, _raw_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected GDsmith crash")
        return GeneratedShard(
            graph_statements=("CREATE (n:Node {value: 1})",),
            query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith"},
        )

    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    plan = RunPlan("retry", 2, (task,), 1, SourceRevision(None, None, None))
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=generation_runner,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, worker)

    shard_dir = tmp_path / "shards" / "neo4j-000"
    assert summary.incomplete == (task,)
    assert (shard_dir / "attempt-000").is_dir()
    assert (shard_dir / "attempt-001").is_dir()


def test_accepted_count_above_the_exact_target_is_failed_not_completed(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 1000

    task = Task()
    plan = RunPlan("overflow", 2, (task,), 1000, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, lambda _task: {"accepted_pairs": 1001})

    assert summary.completed == ()
    assert summary.failed == (task,)


class _InflightDeadlineTask:
    target = TargetSystem.NEO4J
    shard_index = 0
    accepted_query_pairs = 1
    max_runtime_seconds = 1


class _WritesInflightThenBlocks:
    def __init__(self, run_dir) -> None:
        self.run_dir = run_dir

    def __call__(self, _task):
        attempt_dir = self.run_dir / "shards" / "neo4j-000" / "attempt-000"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "inflight.json").write_text(
            json.dumps(
                {
                    "query_hash": "blocked-query",
                    "query_kind": "runtime",
                    "started_at": time.time(),
                    "deadline": time.time() + 0.05,
                    "graph_hash": "graph",
                    "dbms_fingerprint": "dbms",
                }
            ),
            encoding="utf-8",
        )
        time.sleep(10)
        return {"accepted_pairs": 1}


def test_inflight_query_deadline_kills_a_permanently_blocked_worker_before_shard_timeout(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("query-timeout", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.6,
    ).run(plan, _WritesInflightThenBlocks(tmp_path))

    elapsed = time.monotonic() - started
    assert elapsed < 0.3
    assert summary.failed == (task,)
    assert "QUERY_TIMEOUT" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


class _StopsEntireWorkerProcess:
    def __call__(self, _task):
        os.kill(os.getpid(), signal.SIGSTOP)
        return {"accepted_pairs": 1}


def test_stale_heartbeat_kills_a_stopped_worker_before_shard_timeout(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("stale-heartbeat", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.8,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.08,
    ).run(plan, _StopsEntireWorkerProcess())

    assert time.monotonic() - started < 0.4
    assert summary.failed == (task,)
    shard_events = tmp_path / "shards" / "neo4j-000" / "events.jsonl"
    assert "WORKER_HUNG" in shard_events.read_text(encoding="utf-8")


def test_resume_rejects_a_different_immutable_manifest(tmp_path) -> None:
    task = _InflightDeadlineTask()
    original = RunPlan("manifest-a", 2, (task,), 1, SourceRevision("a", None, None))  # type: ignore[arg-type]
    changed = RunPlan("manifest-b", 2, (task,), 1, SourceRevision("b", None, None))  # type: ignore[arg-type]
    supervisor = ShardSupervisor(tmp_path, retry_limit=0)
    supervisor.run(original, lambda _task: {"accepted_pairs": 1, "accepted_pair_hashes": ["pair-a"]})

    with pytest.raises(ManifestMismatchError):
        supervisor.resume(changed, lambda _task: {"accepted_pairs": 1, "accepted_pair_hashes": ["pair-a"]})


def test_duplicate_pair_hashes_cannot_satisfy_exactly_once_target(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 2

    task = Task()
    plan = RunPlan("duplicates", 2, (task,), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(
        plan,
        lambda _task: {"accepted_pairs": 2, "accepted_pair_hashes": ["same", "same"]},
    )

    assert summary.failed == (task,)
    assert summary.completed == ()


class _ContextAwareResumeWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, _task):
        assert self.context is not None
        first = _hash_for("pair-a")
        assert self.context.accepted_pair_hashes == frozenset({first})
        return _durable_result(self.context, _task, accepted_hashes=[first, _hash_for("pair-b")])


def test_resume_recovers_checkpoint_and_fsynced_artifact_after_supervisor_sigkill(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 2, 2, 60)
    plan = RunPlan("sigkill", 2, (task,), 2, SourceRevision(None, None, None))
    shard_dir = tmp_path / "shards" / "neo4j-000"
    journal = ShardJournal(shard_dir, manifest_hash=_manifest_hash(plan), task_identity=_task_identity(task))
    journal.transition(ShardState.GENERATING, attempt=0)
    (shard_dir / "attempt-000").mkdir()
    with (shard_dir / "accepted_pairs.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pair_hash": _hash_for("pair-a")}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    summary = ShardSupervisor(tmp_path, retry_limit=1).resume(plan, _ContextAwareResumeWorker())

    assert summary.completed == (task,)
    checkpoint = json.loads((shard_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert len(checkpoint["accepted_pair_hashes"]) == 2
    assert checkpoint["attempt"] == 1


def _append_phase(path, phase: str, event: str) -> None:
    payload = json.dumps({"phase": phase, "event": event, "pid": os.getpid(), "time": time.monotonic()}) + "\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _LeaseProbeWorker:
    def __init__(self, trace_path) -> None:
        self.trace_path = trace_path
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        with self.context.generation_lease():
            _append_phase(self.trace_path, "generation", "start")
            time.sleep(0.08)
            _append_phase(self.trace_path, "generation", "end")
        with self.context.measurement_lease():
            _append_phase(self.trace_path, "measurement", "start")
            time.sleep(0.05)
            _append_phase(self.trace_path, "measurement", "end")
        return _durable_result(self.context, task)


def _peak_concurrency(records, phase: str) -> int:
    ordered = sorted(
        ((record["time"], 1 if record["event"] == "start" else -1) for record in records if record["phase"] == phase),
        key=lambda item: (item[0], item[1]),
    )
    active = 0
    peak = 0
    for _, delta in ordered:
        active += delta
        peak = max(peak, active)
    return peak


def test_scheduling_leases_allow_three_generation_systems_but_only_one_measurement_system(tmp_path) -> None:
    tasks = tuple(
        ShardTask(target, 0, index + 1, 1, 1, 60)
        for index, target in enumerate((TargetSystem.NEO4J, TargetSystem.MEMGRAPH, TargetSystem.FALKORDB))
    )
    plan = RunPlan("leases", 2, tasks, 3, SourceRevision(None, None, None))
    trace_path = tmp_path / "phase-trace.jsonl"

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=2,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.5,
        max_parallel_generation_systems=3,
        max_parallel_measurement_systems=1,
        max_parallel_shards_per_system=1,
    ).run(plan, _LeaseProbeWorker(trace_path))

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(summary.completed) == 3
    assert 2 <= _peak_concurrency(records, "generation") <= 3
    assert _peak_concurrency(records, "measurement") == 1


class _FailsAfterPersistingScreeningPhase:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, _task):
        assert self.context is not None
        self.context.transition(ShardState.LOADING)
        self.context.transition(ShardState.BUILDING_ACCEPTED_PAIRS)
        self.context.transition(ShardState.SCREENING)
        raise WorkerFailure(ErrorKind.DBMS_CRASHED, "restart during screening")


def test_parent_reloads_child_phase_journal_before_recording_failure(tmp_path) -> None:
    task = _InflightDeadlineTask()
    plan = RunPlan("phase-failure", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        max_parallel_generation_systems=1,
    ).run(plan, _FailsAfterPersistingScreeningPhase())

    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "neo4j-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert summary.failed == (task,)
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
    assert events[-1]["previous_state"] == "screening"
    assert events[-1]["error_kind"] == "DBMS_CRASHED"


class _ParallelRecoveryWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        return _durable_result(self.context, task)


def test_corrupt_artifact_fails_only_its_shard_and_other_future_continues(tmp_path) -> None:
    bad = ShardTask(TargetSystem.NEO4J, 0, 11, 1, 1, 60)
    good = ShardTask(TargetSystem.MEMGRAPH, 0, 12, 1, 1, 60)
    plan = RunPlan("corrupt-one", 2, (bad, good), 2, SourceRevision(None, None, None))
    bad_dir = tmp_path / "shards" / "neo4j-000"
    bad_dir.mkdir(parents=True)
    (bad_dir / "accepted_pairs.jsonl").write_text("{not-json}\n", encoding="utf-8")

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        max_parallel_generation_systems=2,
    ).resume(plan, _ParallelRecoveryWorker())

    assert summary.failed == (bad,)
    assert summary.completed == (good,)
    checkpoint = json.loads((bad_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "failed_final"
    events = (bad_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "INFRASTRUCTURE_ERROR" in events


class _DurableStageWorker:
    def __init__(self, *, result_hash_override: str | None = None) -> None:
        self.context = None
        self.result_hash_override = result_hash_override

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        pair_hash = _hash_for(f"{task.target.value}:{task.shard_index}")
        accepted_path = self.context.shard_dir / "accepted_pairs.jsonl"
        accepted_path.write_text(json.dumps({"pair_hash": pair_hash}) + "\n", encoding="utf-8")
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(json.dumps({"stage": stage, "task": task.target.value}), encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {
            "accepted_pairs": 1,
            "accepted_pair_hashes": [self.result_hash_override or pair_hash],
            "stage_evidence": stages,
        }


def test_missing_accepted_hashes_and_canonical_artifact_is_final_failure(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 21, 1, 1, 60)
    plan = RunPlan("missing-hashes", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, lambda _task: {"accepted_pairs": 1})

    assert summary.failed == (task,)
    assert summary.completed == ()


def test_canonical_and_result_hash_sets_must_match_exactly(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 22, 1, 1, 60)
    plan = RunPlan("hash-mismatch", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(
        plan,
        _DurableStageWorker(result_hash_override=_hash_for("different")),
    )

    assert summary.failed == (task,)
    assert summary.completed == ()


def test_complete_requires_durable_validation_and_reduction_stage_evidence(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 23, 1, 1, 60)
    plan = RunPlan("full-chain", 2, (task,), 1, SourceRevision(None, None, None))

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, _DurableStageWorker())

    events = [
        json.loads(line)
        for line in (tmp_path / "shards" / "neo4j-000" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert summary.completed == (task,)
    assert [event["state"] for event in events][-3:] == ["validating", "reducing", "completed"]


def test_resume_interrupted_attempt_three_is_atomically_failed_final(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 24, 1, 1, 60)
    plan = RunPlan("attempt-exhausted", 2, (task,), 1, SourceRevision(None, None, None))
    shard_dir = tmp_path / "shards" / "neo4j-000"
    journal = ShardJournal(shard_dir, manifest_hash=_manifest_hash(plan), task_identity=_task_identity(task))
    journal.transition(ShardState.GENERATING, attempt=3)
    (shard_dir / "attempt-003").mkdir()

    summary = ShardSupervisor(tmp_path, retry_limit=3).resume(plan, _DurableStageWorker())

    checkpoint = json.loads((shard_dir / "checkpoint.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (shard_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary.failed == (task,)
    assert checkpoint["state"] == "failed_final"
    assert checkpoint["attempt"] == 3
    assert events[-1]["state"] == "failed_final"
    assert events[-1]["error_kind"] == "WORKER_CRASHED"


def test_status_rejects_checkpoint_copied_from_another_manifest_task(tmp_path) -> None:
    tasks = (
        ShardTask(TargetSystem.NEO4J, 0, 31, 1, 1, 60),
        ShardTask(TargetSystem.MEMGRAPH, 0, 32, 1, 1, 60),
    )
    plan = RunPlan("status-identity", 2, tasks, 2, SourceRevision(None, None, None))
    run_id = "copied-checkpoint"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
    summary = ShardSupervisor(run_dir, retry_limit=0).run(plan, _DurableStageWorker())
    assert len(summary.completed) == 2
    neo = run_dir / "shards" / "neo4j-000"
    memgraph = run_dir / "shards" / "memgraph-000"
    for name in ("checkpoint.json", "events.jsonl", "accepted_pairs.jsonl"):
        shutil.copy2(neo / name, memgraph / name)

    status = ExperimentLifecycle(tmp_path).status(run_id)

    assert status.completed == 1
    assert status.failed == 1
    assert status.invalid == 1
