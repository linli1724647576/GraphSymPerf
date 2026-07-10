import json
import os
import signal
import time

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import RunPlan, ShardTask, SourceRevision
from graph_sym_perf.experiment.state import ShardJournal, ShardState
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.supervisor import AttemptContext, ManifestMismatchError, ShardSupervisor, _manifest_hash
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.experiment.worker import WorkerFailure
from graph_sym_perf.experiment.state import ErrorKind


def test_retryable_shard_failure_does_not_stop_other_tasks(tmp_path) -> None:
    tasks = (
        ShardTask.__new__(ShardTask),
    )
    # Use small stand-in tasks: supervisor only needs a stable task identity.
    plan = RunPlan("test", 2, ("bad", "good"), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]
    calls = []
    def worker(task):
        calls.append(task)
        if task == "bad" and calls.count("bad") == 1:
            raise RuntimeError("retryable")
        return {"accepted_pairs": 1000}
    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, worker)
    assert summary.completed == ("bad", "good")


def test_supervisor_uses_task_specific_accepted_pair_target(tmp_path) -> None:
    class Task:
        accepted_query_pairs = 3

    plan = RunPlan("test", 2, (Task(),), 3, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path).run(plan, lambda task: {"accepted_pairs": 3})
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
    assert summary.completed == (task,)
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
        assert self.context.accepted_pair_hashes == frozenset({"pair-a"})
        return {
            "accepted_pairs": 2,
            "accepted_pair_hashes": [*self.context.accepted_pair_hashes, "pair-b"],
        }


def test_resume_recovers_checkpoint_and_fsynced_artifact_after_supervisor_sigkill(tmp_path) -> None:
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 2, 2, 60)
    plan = RunPlan("sigkill", 2, (task,), 2, SourceRevision(None, None, None))
    shard_dir = tmp_path / "shards" / "neo4j-000"
    journal = ShardJournal(shard_dir, manifest_hash=_manifest_hash(plan))
    journal.transition(ShardState.GENERATING, attempt=0)
    (shard_dir / "attempt-000").mkdir()
    with (shard_dir / "accepted_pairs.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pair_hash": "pair-a"}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    summary = ShardSupervisor(tmp_path, retry_limit=1).resume(plan, _ContextAwareResumeWorker())

    assert summary.completed == (task,)
    checkpoint = json.loads((shard_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["accepted_pair_hashes"] == ["pair-a", "pair-b"]
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
        return {"accepted_pairs": 1, "accepted_pair_hashes": [task.target.value]}


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
