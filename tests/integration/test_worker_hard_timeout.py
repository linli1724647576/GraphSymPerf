import json
import hashlib
import time
from pathlib import Path

import pytest

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.process_monitor import ProcessMonitor, WorkerProcessError
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.experiment.process_control import terminate_process_group
from graph_sym_perf.experiment.supervisor import ShardSupervisor
from graph_sym_perf.experiment.worker import AdapterQueryProxy, WorkerFailure
from graph_sym_perf.sanitization import EvidencePayload, unwrap_evidence_payload


def test_hard_timeout_terminates_the_entire_process_group(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("graph_sym_perf.experiment.process_control.os.killpg", lambda pgid, signal: calls.append((pgid, signal)))
    terminate_process_group(42)
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

    assert time.monotonic() - started < 0.8
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


def _attempt_context(tmp_path: Path) -> AttemptContext:
    shard_dir = tmp_path / "shards" / "neo4j-000"
    attempt_dir = shard_dir / "attempt-000"
    attempt_dir.mkdir(parents=True)
    return AttemptContext(
        attempt=0,
        attempt_dir=attempt_dir,
        shard_dir=shard_dir,
        manifest_hash="a" * 64,
        target_key="neo4j",
        accepted_pair_hashes=frozenset(),
        candidate_hashes=frozenset(),
        lease_root=tmp_path / ".leases",
        max_parallel_generation_systems=1,
        max_parallel_measurement_systems=1,
        max_parallel_shards_per_system=1,
    )


class _LargeResultWorker:
    def __call__(self, _task):
        hashes = [hashlib.sha256(f"pair-{index}".encode()).hexdigest() for index in range(1000)]
        return {"accepted_pairs": 1000, "accepted_pair_hashes": hashes}


class _SensitiveStructuredResultWorker:
    def __call__(self, _task):
        return {
            "accepted_pairs": 1,
            "diagnostics": {
                "NEO4J_AUTH": "neo4j/ipc-auth-secret",
                "nested": [
                    {"vendor_password": "ipc-password"},
                    "bolt://neo4j:ipc-uri-secret@db.internal:7687 /Users/private/result",
                ],
            },
            "query": "MATCH (n) RETURN n",
            "result_rows": [{"value": 7}],
        }


class _SensitiveSealedResultWorker:
    def __call__(self, _task):
        body = {
            "payload": {
                "message": (
                    "Authorization: Custom Scheme sealed-ipc-secret; retry=true"
                ),
            },
        }
        return {
            "accepted_pairs": 1,
            "nested": {**body, "content_hash": canonical_hash(body)},
        }


class _FieldSemanticResultWorker:
    def __call__(self, _task):
        evidence = {
            "query": "MATCH (n) WHERE auth = 'public' AND token = 'visible' RETURN n",
            "params": {
                "password": "literal-parameter",
                "message": "Authorization: literal parameter",
            },
            "results": [
                "Authorization: literal result",
                {"value": "/Users/example/literal result", "password": "row-secret"},
            ],
            "rows": [
                {
                    "content_hash": "literal-content-hash-column",
                    "receipt": "literal-receipt-column",
                    "signature": "literal-signature-column",
                    "password": "literal-password-column",
                    "token": "literal-token-column",
                    "message": "auth=literal-row-auth",
                    "bolt://literal-key": "literal-key-value",
                }
            ],
        }
        return {
            "accepted_pairs": 1,
            "evidence": EvidencePayload(evidence),
            "query_text": "password=ordinary-query-secret RETURN n",
            "records": [{"password": "ordinary-row-secret"}],
            "diagnostics": {
                "message": (
                    "token=probe-token auth=probe-auth; "
                    "config.neo4j.Authorization = Signature keyId=public "
                    "probe-dotted-auth; retry=true; "
                    'failed at "file:///Users/sakuraq/My URI Folder/probe output.json"'
                ),
                "password_value": "hunter2",
                "authorization_header": "Bearer opaque-header",
            },
        }


def test_large_worker_result_is_persisted_by_reference_without_queue_deadlock(tmp_path) -> None:
    context = _attempt_context(tmp_path)
    monitor = ProcessMonitor(
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
        hard_timeout_seconds=0.5,
    )
    started = time.monotonic()

    result = monitor.run(_LargeResultWorker(), object(), context)

    assert time.monotonic() - started < 0.5
    assert len(result["accepted_pair_hashes"]) == 1000
    assert (context.attempt_dir / "result.json").is_file()


def test_worker_result_is_recursively_sanitized_before_ipc_reference(tmp_path) -> None:
    context = _attempt_context(tmp_path)
    monitor = ProcessMonitor(
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
        hard_timeout_seconds=0.5,
    )

    result = monitor.run(_SensitiveStructuredResultWorker(), object(), context)

    assert result["query"] == "MATCH (n) RETURN n"
    assert result["result_rows"] == [{"value": 7}]
    persisted = (context.attempt_dir / "result.json").read_text(encoding="utf-8")
    for forbidden in (
        "ipc-auth-secret",
        "ipc-password",
        "ipc-uri-secret",
        "/Users/private",
        "bolt://",
    ):
        assert forbidden not in persisted


def test_worker_rejects_sealed_result_that_would_need_boundary_sanitization(
    tmp_path,
) -> None:
    context = _attempt_context(tmp_path)
    monitor = ProcessMonitor(
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
        hard_timeout_seconds=0.5,
    )

    with pytest.raises(WorkerProcessError) as captured:
        monitor.run(_SensitiveSealedResultWorker(), object(), context)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR
    assert "sealed-ipc-secret" not in str(captured.value)
    assert not (context.attempt_dir / "result.json").exists()


def test_process_monitor_preserves_evidence_fields_and_cleans_diagnostics(
    tmp_path,
) -> None:
    context = _attempt_context(tmp_path)
    monitor = ProcessMonitor(
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
        hard_timeout_seconds=0.5,
    )

    result = monitor.run(_FieldSemanticResultWorker(), object(), context)

    evidence = unwrap_evidence_payload(result["evidence"])
    assert evidence["query"] == (
        "MATCH (n) WHERE auth = 'public' AND token = 'visible' RETURN n"
    )
    assert evidence["params"] == {
        "password": "literal-parameter",
        "message": "Authorization: literal parameter",
    }
    assert evidence["results"][0] == "Authorization: literal result"
    assert evidence["results"][1]["value"] == "/Users/example/literal result"
    assert evidence["results"][1]["password"] == "row-secret"
    assert evidence["rows"] == [
        {
            "content_hash": "literal-content-hash-column",
            "receipt": "literal-receipt-column",
            "signature": "literal-signature-column",
            "password": "literal-password-column",
            "token": "literal-token-column",
            "message": "auth=literal-row-auth",
            "bolt://literal-key": "literal-key-value",
        }
    ]
    assert result["records"] == [{"password": "<redacted>"}]
    persisted = (context.attempt_dir / "result.json").read_text(encoding="utf-8")
    for forbidden in (
        "ordinary-query-secret",
        "ordinary-row-secret",
        "probe-token",
        "probe-auth",
        "probe-dotted-auth",
        "keyId",
        "hunter2",
        "opaque-header",
        "/Users/sakuraq",
        "My URI Folder",
    ):
        assert forbidden not in persisted


class _MarkerWorker:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __call__(self, _task):
        self.marker.write_text("ran", encoding="utf-8")
        return {"accepted_pairs": 0}


def test_bootstrap_timeout_kills_only_child_before_verified_process_group(monkeypatch, tmp_path) -> None:
    context = _attempt_context(tmp_path)
    marker = tmp_path / "worker-ran"
    killpg_calls = []

    def delayed_bootstrap() -> tuple[int, int]:
        time.sleep(1)
        return (123, 123)

    monkeypatch.setattr("graph_sym_perf.experiment.process_monitor._establish_process_group", delayed_bootstrap, raising=False)
    monkeypatch.setattr(
        "graph_sym_perf.experiment.process_monitor.terminate_process_group",
        lambda pgid: killpg_calls.append(pgid),
    )
    monitor = ProcessMonitor(
        heartbeat_seconds=0.01,
        stale_after_seconds=0.2,
        hard_timeout_seconds=1.0,
    )
    monitor.bootstrap_timeout_seconds = 0.05

    with pytest.raises(Exception, match="bootstrap"):
        monitor.run(_MarkerWorker(marker), object(), context)

    assert killpg_calls == []
    assert not marker.exists()
