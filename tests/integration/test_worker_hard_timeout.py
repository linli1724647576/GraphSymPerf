import json
import time

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.experiment.process_control import terminate_process_group
from graph_sym_perf.experiment.supervisor import ShardSupervisor
from graph_sym_perf.experiment.worker import AdapterQueryProxy, WorkerFailure


def test_hard_timeout_terminates_the_entire_process_group(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("graph_sym_perf.experiment.process_control.os.getpgid", lambda pid: pid + 1)
    monkeypatch.setattr("graph_sym_perf.experiment.process_control.os.killpg", lambda pgid, signal: calls.append((pgid, signal)))
    terminate_process_group(41)
    assert calls and calls[0][0] == 42


class _QueryTask:
    target = TargetSystem.NEO4J
    shard_index = 0
    accepted_query_pairs = 1
    max_runtime_seconds = 1


class _PermanentBlockAdapter:
    def execute(self, *_args, **_kwargs):
        time.sleep(10)


class _ProxyWorker:
    def __init__(self) -> None:
        self.attempt_dir = None

    def bind_attempt_dir(self, attempt_dir) -> None:
        self.attempt_dir = attempt_dir

    def __call__(self, _task):
        proxy = AdapterQueryProxy(
            _PermanentBlockAdapter(),
            attempt_dir=self.attempt_dir,
            graph_hash="graph-a",
            dbms_fingerprint="dbms-a",
            default_timeout_seconds=0.05,
        )
        proxy.execute("RETURN 1", {}, "runtime", timeout_seconds=0.05)
        return {"accepted_pairs": 1}


def test_adapter_proxy_deadline_terminates_an_uncancellable_driver_call(tmp_path) -> None:
    task = _QueryTask()
    plan = RunPlan("proxy-timeout", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.6,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
    ).run(plan, _ProxyWorker())

    assert time.monotonic() - started < 0.3
    assert summary.failed == (task,)
    finding = json.loads((tmp_path / "reliability" / "findings.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert finding["kind"] == "QUERY_TIMEOUT"
    assert finding["query_hash"]
    assert finding["graph_hash"] == "graph-a"
    assert finding["dbms_fingerprint"] == "dbms-a"


class _TypedFailureWorker:
    def __call__(self, _task):
        raise WorkerFailure(ErrorKind.DBMS_CRASHED, "injected DBMS restart")


def test_isolated_worker_preserves_typed_failure_kind(tmp_path) -> None:
    task = _QueryTask()
    plan = RunPlan("typed-error", 2, (task,), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(tmp_path, retry_limit=0, process_isolation=True).run(plan, _TypedFailureWorker())

    assert summary.failed == (task,)
    assert "DBMS_CRASHED" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    finding = json.loads((tmp_path / "reliability" / "findings.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert finding["kind"] == "DBMS_CRASHED"
