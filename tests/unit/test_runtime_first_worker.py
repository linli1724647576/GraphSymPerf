import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.experiment.worker import (
    GeneratedShard,
    RuntimeFirstShardWorker,
    WorkerFailure,
    _generation_backend_provenance,
    _graph_snapshot_hash,
)
from graph_sym_perf.generation.corpus import GraphCorpus
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot


class _Adapter:
    target = TargetSystem.NEO4J

    def __init__(self) -> None:
        self.loaded = ()
        self.executed = []
        self.closed = False

    def ensure_schema(self) -> None:
        return None

    def load_gdsmith_graph(self, statements):
        self.loaded = tuple(statements)

    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        self.executed.append((query, query_kind))
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=({"n": 1},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )

    def profile(self, *_args, **_kwargs):  # pragma: no cover - must never be used
        raise AssertionError("runtime-first worker must not call profile")

    def close(self) -> None:
        self.closed = True

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint("a" * 64)

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch("e" * 64, "test")


class _RuntimeGapAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **_kwargs)
        if query_kind != "runtime":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=5.0 if "OR NOT" in query else 1.0,
            summary=result.summary,
        )


class _FingerprintUnavailableAdapter(_Adapter):
    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint(None, "server_version_unavailable")


class _InflightCheckingAdapter(_Adapter):
    def __init__(self, attempt_dir) -> None:
        super().__init__()
        self.attempt_dir = attempt_dir
        self.inflight_records = []

    def _capture_inflight(self) -> None:
        self.inflight_records.append(json.loads((self.attempt_dir / "inflight.json").read_text(encoding="utf-8")))

    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        self._capture_inflight()
        return super().execute(query, parameters, query_kind, **kwargs)

    def ensure_schema(self) -> None:
        self._capture_inflight()

    def load_gdsmith_graph(self, statements) -> None:
        self._capture_inflight()
        return super().load_gdsmith_graph(statements)


class _FingerprintAdapter(_RuntimeGapAdapter):
    def __init__(self, fingerprint: str, health_epoch: str = "e" * 64) -> None:
        super().__init__()
        self.fingerprint = fingerprint
        self.health_epoch = health_epoch

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint(self.fingerprint)

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch(self.health_epoch, "test")


class _LeaseContext:
    def __init__(self) -> None:
        self.phases = []

    @contextmanager
    def generation_lease(self):
        self.phases.append("generation:start")
        try:
            yield
        finally:
            self.phases.append("generation:end")

    @contextmanager
    def measurement_lease(self):
        self.phases.append("measurement:start")
        try:
            yield
        finally:
            self.phases.append("measurement:end")


def test_worker_graph_identity_matches_validation_snapshot_contract() -> None:
    statements = (" CREATE (a) ", "", "CREATE (b)")

    assert _graph_snapshot_hash(statements) == GraphSnapshot.from_statements(statements).content_hash
    assert GraphCorpus((" CREATE (a) ", "CREATE (b)")).content_hash == _graph_snapshot_hash(statements)


def test_age_generation_provenance_never_claims_native_provider_coverage() -> None:
    assert _generation_backend_provenance(TargetSystem.APACHE_AGE, "agensgraph") == "agensgraph_proxy"
    with pytest.raises(ValueError, match="AgensGraph proxy"):
        _generation_backend_provenance(TargetSystem.APACHE_AGE, "neo4j")


def test_formal_gdsmith_seam_keeps_partial_timeout_corpus_and_deduplicates_ast(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) WHERE n.value = 1 RETURN n")

    def fake_run_gdsmith(**kwargs):
        assert kwargs["num_queries"] == 2
        assert kwargs["seed"] == 7
        return SimpleNamespace(
            returncode=124,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
                accepted_queries=(record, record),
            ),
        )

    monkeypatch.setattr("graph_sym_perf.experiment.worker.run_gdsmith", fake_run_gdsmith)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    generated = worker._generate_with_gdsmith(
        ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60, "neo4j"),
        tmp_path,
    )

    assert generated.query_texts == (record.statement,)
    assert generated.generator_metadata["generation_stop_reason"] == "TIME_BUDGET_EXHAUSTED"
    assert generated.generator_metadata["duplicate_ast_records"] == 1


def test_worker_accepts_only_complete_equivalent_gdsmith_pairs_and_writes_minimal_records(tmp_path) -> None:
    adapter = _Adapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=(
            "MATCH (n) WHERE n.value = 1 RETURN n",
            "MATCH (n) WHERE n.value = 2 RETURN n",
        ),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 2, 2, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(task)

    assert result["accepted_pairs"] == 2
    assert result["acceptance_status"] == "complete"
    assert result["acceptance_stop_reason"] == "ACCEPTED_TARGET_REACHED"
    assert result["acceptance_generated_seeds"] == 2
    assert result["generated_query_records"] == 2
    assert adapter.loaded == shard.graph_statements
    assert adapter.closed
    assert all(kind != "profile" for _, kind in adapter.executed)
    records = [json.loads(line) for line in (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["equivalence"] == "complete_typed_equivalent" for record in records)


def test_worker_records_incomplete_when_seed_bound_cannot_fill_budget(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60))

    assert result["accepted_pairs"] == 0
    assert result["acceptance_status"] == "incomplete"
    assert result["acceptance_stop_reason"] == "MAX_GENERATED_SEEDS_EXHAUSTED"
    assert result["acceptance_generated_seeds"] == 2


def test_worker_rejects_same_gdsmith_ast_across_shards_in_one_run(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    spec = load_experiment_spec("config/experiment.yaml")
    first = RuntimeFirstShardWorker(
        spec,
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    second = RuntimeFirstShardWorker(
        spec,
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    second.bind_run_dir(tmp_path)

    accepted = first(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))
    duplicate = second(ShardTask(TargetSystem.NEO4J, 1, 8, 1, 1, 60))

    assert accepted["accepted_pairs"] == 1
    assert duplicate["accepted_pairs"] == 0
    assert duplicate["reject_reasons"] == {"CROSS_SHARD_DUPLICATE_AST": 1}


def test_worker_persists_discovery_slow_query_for_validation_replay(tmp_path) -> None:
    adapter = _RuntimeGapAdapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(task)

    records = [
        json.loads(line)
        for line in (tmp_path / "shards" / "neo4j-000" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert result["screened_candidates"] == 1
    assert records[0]["discovery_slow_query"] == "b"
    assert records[0]["parameters"] == {}
    assert records[0]["graph_snapshot_hash"]
    assert records[0]["dbms_fingerprint"] == "a" * 64


def test_worker_rejects_shard_when_dbms_fingerprint_is_unavailable(tmp_path) -> None:
    adapter = _FingerprintUnavailableAdapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(task)

    assert result["accepted_pairs"] == 0
    assert result["screened_candidates"] == 0
    assert result["reject_reasons"] == {"DBMS_FINGERPRINT_UNAVAILABLE": 1}
    assert result["dbms_fingerprint"] == {"available": False, "value": None, "reason": "server_version_unavailable"}
    assert not (tmp_path / "shards" / "neo4j-000" / "candidates.jsonl").exists()


def test_worker_wraps_acceptance_and_runtime_execute_calls_with_atomic_inflight_state(tmp_path) -> None:
    attempt_dir = tmp_path / "shards" / "neo4j-000" / "attempt-000"
    attempt_dir.mkdir(parents=True)
    load_adapter = _InflightCheckingAdapter(attempt_dir)
    measurement_adapter = _InflightCheckingAdapter(attempt_dir)
    adapters = iter((load_adapter, measurement_adapter))
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: next(adapters),
    )
    worker.bind_run_dir(tmp_path)
    worker.bind_attempt_dir(attempt_dir)

    worker(task)

    records = load_adapter.inflight_records + measurement_adapter.inflight_records
    assert {record["query_kind"] for record in records} >= {
        "shard_reset",
        "schema_setup",
        "graph_load",
        "acceptance_base",
        "acceptance_transformed",
        "runtime",
    }
    assert all(record["query_hash"] and record["deadline"] > record["started_at"] for record in records)
    assert not (attempt_dir / "inflight.json").exists()
    assert load_adapter.closed and measurement_adapter.closed


def test_dbms_fingerprint_change_at_measurement_reconnect_invalidates_runtime_candidates(tmp_path) -> None:
    first = _FingerprintAdapter("a" * 64)
    restarted = _FingerprintAdapter("b" * 64)
    adapters = iter((first, restarted))
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: next(adapters),
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        worker(task)

    assert captured.value.kind is ErrorKind.DBMS_CRASHED
    assert first.closed and restarted.closed
    assert not (tmp_path / "shards" / "neo4j-000" / "candidates.jsonl").exists()
    invalidation = json.loads(
        (tmp_path / "shards" / "neo4j-000" / "attempt-000" / "runtime_invalidated.json").read_text(encoding="utf-8")
    )
    assert invalidation["before"] == "a" * 64
    assert invalidation["after"] == "b" * 64


def test_same_version_restart_changes_health_epoch_and_invalidates_runtime_candidates(tmp_path) -> None:
    first = _FingerprintAdapter("a" * 64, "1" * 64)
    restarted = _FingerprintAdapter("a" * 64, "2" * 64)
    adapters = iter((first, restarted))
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: next(adapters),
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        worker(task)

    assert captured.value.kind is ErrorKind.DBMS_CRASHED
    invalidation = json.loads(
        (tmp_path / "shards" / "neo4j-000" / "attempt-000" / "runtime_invalidated.json").read_text(
            encoding="utf-8"
        )
    )
    assert invalidation["fingerprint_before"] == invalidation["fingerprint_after"] == "a" * 64
    assert invalidation["health_epoch_before"] == "1" * 64
    assert invalidation["health_epoch_after"] == "2" * 64


def test_formal_worker_acquires_generation_and_measurement_phase_leases(tmp_path) -> None:
    context = _LeaseContext()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)
    worker.bind_attempt_context(context)  # type: ignore[arg-type]

    worker(task)

    assert context.phases == [
        "generation:start",
        "generation:end",
        "measurement:start",
        "measurement:end",
    ]
