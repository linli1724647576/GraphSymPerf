import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import graph_sym_perf.experiment.worker as worker_module

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.artifacts.store import ContentAddressedStore, verify_candidate_record_reference
from graph_sym_perf.core import ExecutionResult, QueryExecutionError, TargetSystem
from graph_sym_perf.experiment.plan_models import CampaignPhase, RulePlanEntry, RuleStream, ShardTask
from graph_sym_perf.experiment.evidence_validation import candidate_artifact_hashes, canonical_hash
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.experiment.worker import (
    GeneratedShard,
    RuntimeFirstShardWorker,
    WorkerFailure,
    _gdsmith_connection,
    _generation_backend_provenance,
    _generation_query_limit,
    _graph_snapshot_hash,
    _query_hash,
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

    def connectivity_check(self):
        return True, "connected"

    def load_gdsmith_graph(self, statements):
        self.loaded = tuple(statements)

    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        self.executed.append((query, query_kind))
        manifest_values = {
            "graph_manifest_nodes": ("nodes", 1),
            "graph_manifest_relationships": ("relationships", 0),
            "graph_manifest_labels": ("labels", 1),
            "graph_manifest_relationship_types": ("types", 0),
            "graph_manifest_indexes": ("indexes", 0),
        }
        key, value = manifest_values.get(query_kind, ("n", 1))
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=({key: value},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )

    def profile(self, *_args, **_kwargs):  # pragma: no cover - must never be used
        raise AssertionError("runtime-first worker must not call profile")

    def close(self) -> None:
        self.closed = True

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint("a" * 64, server_version="test-dbms/1")

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch("e" * 64, "test")


class _DatabaseResetAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.database_resets = 0

    def reset_graph(self, *, timeout_seconds=None) -> bool:
        self.database_resets += 1
        return True

    def load_graph(self, corpus: GraphCorpus) -> None:
        self.loaded = corpus.statements

    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind == "shard_reset":
            raise AssertionError("database-level reset must replace graph-wide Cypher deletion")
        return super().execute(query, parameters, query_kind, **kwargs)


class _RuntimeGapAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **_kwargs)
        if query_kind != "runtime":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=5.0 if ("OR NOT" in query or "OR true" in query) else 1.0,
            summary=result.summary,
        )


class _FingerprintUnavailableAdapter(_Adapter):
    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint(None, "server_version_unavailable")


class _InvalidGraphManifestAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **kwargs)
        if query_kind != "graph_manifest_nodes":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=({"nodes": "unknown"},),
            elapsed_ms=result.elapsed_ms,
            summary=result.summary,
        )


class _FinalFingerprintDriftAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.fingerprint_reads = 0

    def dbms_fingerprint(self) -> DBMSFingerprint:
        self.fingerprint_reads += 1
        value = "a" * 64 if self.fingerprint_reads == 1 else "b" * 64
        return DBMSFingerprint(value, server_version="test-dbms/1")


class _FinalGraphDriftAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.node_manifest_reads = 0

    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **kwargs)
        if query_kind != "graph_manifest_nodes":
            return result
        self.node_manifest_reads += 1
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=({"nodes": self.node_manifest_reads},),
            elapsed_ms=result.elapsed_ms,
            summary=result.summary,
        )


class _LegacyGenerationAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        self.executed.append((query, query_kind))
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=({"n": 1},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )


class _EquivalenceMismatchAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **_kwargs)
        if query_kind != "acceptance_transformed":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=({"n": 999},),
            elapsed_ms=result.elapsed_ms,
            summary=result.summary,
        )


class _UnsupportedEquivalenceAdapter(_Adapter):
    class Unsupported:
        pass

    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **_kwargs)
        if not query_kind.startswith("acceptance_"):
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=({"n": self.Unsupported()},),
            elapsed_ms=result.elapsed_ms,
            summary=result.summary,
        )


class _OrdinaryQueryErrorAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind.startswith("acceptance_"):
            raise QueryExecutionError("invalid query for this generated graph")
        return super().execute(query, parameters, query_kind, **kwargs)


class _ConnectionLostAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind.startswith("acceptance_"):
            raise OSError("connection reset by peer")
        return super().execute(query, parameters, query_kind, **kwargs)


class _ScaryOrdinaryQueryErrorAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind.startswith("acceptance_"):
            raise QueryExecutionError("connection reset while reporting Java heap space")
        return super().execute(query, parameters, query_kind, **kwargs)


class _CloseFailureAdapter(_Adapter):
    def close(self) -> None:
        raise OSError("driver close failed")


class _SteppedClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self._last = values[-1]

    def __call__(self) -> float:
        self._last = next(self._values, self._last)
        return self._last


class _ManualClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _RejectThenInfrastructureAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind == "acceptance_base" and "n.id = 1" in query:
            raise QueryExecutionError("ordinary seed rejection")
        if query_kind == "acceptance_base" and "n.id = 2" in query:
            raise OSError("connection lost on the second seed")
        return super().execute(query, parameters, query_kind, **kwargs)


class _AcceptThenInfrastructureAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind == "acceptance_base" and "n.id = 2" in query:
            raise OSError("connection lost after one accepted seed")
        return super().execute(query, parameters, query_kind, **kwargs)


class _TimedQueryErrorAdapter(_Adapter):
    def __init__(self, clock: _ManualClock, elapsed: float) -> None:
        super().__init__()
        self.clock = clock
        self.elapsed = elapsed

    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind == "acceptance_base":
            self.clock.advance(self.elapsed)
            raise QueryExecutionError("ordinary query error at the deadline")
        return super().execute(query, parameters, query_kind, **kwargs)


class _TimedCloseAdapter(_Adapter):
    def __init__(self, clock: _ManualClock, elapsed: float) -> None:
        super().__init__()
        self.clock = clock
        self.elapsed = elapsed

    def close(self) -> None:
        self.clock.advance(self.elapsed)
        super().close()


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

    def reset_schema(self) -> None:
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
        return DBMSFingerprint(self.fingerprint, server_version="test-dbms/1")

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch(self.health_epoch, "test")


class _ConnectionPrimedFingerprintAdapter(_FingerprintAdapter):
    def __init__(self, fingerprint: str, health_epoch: str = "e" * 64) -> None:
        super().__init__(fingerprint, health_epoch)
        self.connected = False

    def connectivity_check(self):
        self.connected = True
        return True, "connected"

    def dbms_fingerprint(self) -> DBMSFingerprint:
        if not self.connected:
            return DBMSFingerprint(None, "server_version_unavailable")
        return super().dbms_fingerprint()


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


def _rule_plan_entry(
    *,
    graph_scale: int = 256,
    rule_id: str = "constant_true_disjunction",
    family: str = "bool",
    accepted_pair_quota: int = 1,
    max_generated_seed_budget: int = 2,
    max_entry_runtime_seconds: int = 600,
) -> RulePlanEntry:
    return RulePlanEntry(
        entry_id=f"p1:neo4j:discovery:{rule_id}:{graph_scale}:0",
        phase=CampaignPhase.P1,
        target=TargetSystem.NEO4J,
        rule_id=rule_id,
        family=family,
        tier="core",
        graph_scale=graph_scale,
        shard_index=0,
        shard_seed=7,
        accepted_pair_quota=accepted_pair_quota,
        max_generated_seed_budget=max_generated_seed_budget,
        max_variants_per_seed=1,
        proof_version="1",
        query_timeout_seconds=30,
        max_entry_runtime_seconds=max_entry_runtime_seconds,
        measurement_protocol_id="paired-runtime-v1",
        stream=RuleStream.DISCOVERY,
    )


def test_worker_graph_identity_matches_validation_snapshot_contract() -> None:
    statements = (" CREATE (a) ", "", "CREATE (b)")

    assert _graph_snapshot_hash(statements) == GraphSnapshot.from_statements(statements).content_hash
    assert GraphCorpus((" CREATE (a) ", "CREATE (b)")).content_hash == _graph_snapshot_hash(statements)


def test_age_generation_provenance_never_claims_native_provider_coverage() -> None:
    assert _generation_backend_provenance(TargetSystem.APACHE_AGE, "agensgraph") == "agensgraph_proxy"
    with pytest.raises(ValueError, match="AgensGraph proxy"):
        _generation_backend_provenance(TargetSystem.APACHE_AGE, "neo4j")


def test_arcadedb_gdsmith_uses_an_isolated_neo4j_proxy() -> None:
    connection = _gdsmith_connection(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_GDSMITH_PROXY_ARCADEDB_URI": "bolt://proxy.example:7698",
            "GRAPHSYMPERF_GDSMITH_PROXY_ARCADEDB_USER": "proxy-user",
            "GRAPHSYMPERF_GDSMITH_PROXY_ARCADEDB_PASSWORD": "proxy-secret",
        },
    )

    assert connection == ("neo4j", "proxy.example", 7698, "proxy-user", "proxy-secret")


def test_arcadedb_generation_provenance_never_claims_native_provider_coverage() -> None:
    assert _generation_backend_provenance(TargetSystem.ARCADEDB, "neo4j") == "neo4j_proxy"
    with pytest.raises(ValueError, match="Neo4j proxy"):
        _generation_backend_provenance(TargetSystem.ARCADEDB, "arcadedb")


def test_formal_gdsmith_seam_keeps_partial_stalled_corpus_and_deduplicates_ast(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) WHERE n.value = 1 RETURN n")

    def fake_run_gdsmith(**kwargs):
        assert kwargs["num_queries"] == 2
        assert kwargs["seed"] == 7
        assert kwargs["config"].max_raw_artifact_bytes == 350 * 1024 * 1024
        return SimpleNamespace(
            returncode=124,
            generation_stalled=True,
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
    assert generated.generator_metadata["generation_stop_reason"] == "NO_PROGRESS_TIMEOUT"
    assert generated.generator_metadata["duplicate_ast_records"] == 1


def test_worker_passes_rule_plan_graph_scale_to_gdsmith(monkeypatch, tmp_path) -> None:
    captured = {}
    record = SimpleNamespace(statement="MATCH (n) RETURN n")

    def fake_run_gdsmith(**kwargs):
        captured["config"] = kwargs["config"]
        return SimpleNamespace(
            returncode=0,
            process_returncode=0,
            fatal_stderr_markers=(),
            query_record_limit_reached=False,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
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

    generated = worker._generate_with_gdsmith(_rule_plan_entry(graph_scale=256), tmp_path / "raw")

    assert captured["config"].max_node_num == 256
    assert generated.generator_metadata["requested_max_nodes"] == 256


def test_schema_v3_entry_generation_uses_entry_artifact_budget_and_frozen_algorithm(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}
    record = SimpleNamespace(statement="MATCH (n) RETURN n")

    def fake_run_gdsmith(**kwargs):
        captured["config"] = kwargs["config"]
        return SimpleNamespace(
            returncode=0,
            process_returncode=0,
            fatal_stderr_markers=(),
            query_record_limit_reached=False,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        )

    monkeypatch.setattr("graph_sym_perf.experiment.worker.run_gdsmith", fake_run_gdsmith)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    worker._generate_with_gdsmith(_rule_plan_entry(), tmp_path / "raw")

    assert captured["config"].algorithm == "compared3"
    assert captured["config"].max_raw_artifact_bytes == 350 * 1024 * 1024


def test_schema_v3_entry_creates_adapter_from_entry_budgets(monkeypatch) -> None:
    captured = {}
    sentinel = object()

    def fake_create_adapter(system, run_config, environment, *, allow_create):
        captured.update(
            {
                "system": system,
                "run_config": run_config,
                "environment": environment,
                "allow_create": allow_create,
            }
        )
        return sentinel

    monkeypatch.setattr("graph_sym_perf.experiment.worker.create_adapter", fake_create_adapter)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        environment={"GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687"},
    )
    entry = _rule_plan_entry(
        accepted_pair_quota=3,
        max_generated_seed_budget=30,
        max_entry_runtime_seconds=600,
    )

    adapter = worker._create_adapter(entry)

    assert adapter is sentinel
    assert captured["system"].target is TargetSystem.NEO4J
    assert captured["run_config"].query_generation.max_cases == 30
    assert captured["run_config"].query_generation.gdsmith.max_node_num == entry.graph_scale
    assert captured["run_config"].equivalence.timeout_seconds == entry.query_timeout_seconds
    assert captured["allow_create"] is True


def test_formal_gdsmith_seam_retries_stalled_corpus_that_cannot_cover_target(
    tmp_path,
    monkeypatch,
) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=124,
            generation_stalled=True,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_GDSMITH_PROXY_ARCADEDB_URI": "bolt://localhost:7698",
        },
    )

    with pytest.raises(WorkerFailure) as exc_info:
        worker._generate_with_gdsmith(
            ShardTask(TargetSystem.ARCADEDB, 0, 7, 100, 1000, 60, "neo4j_proxy"),
            tmp_path,
        )

    assert exc_info.value.kind is ErrorKind.WORKER_HUNG
    assert "1 query records; cannot cover 100 accepted pairs" in str(exc_info.value)


def test_formal_gdsmith_seam_accepts_upstream_query_limit_sentinel(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=1,
            process_returncode=1,
            query_record_limit_reached=True,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
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
    assert generated.generator_metadata["generation_stop_reason"] == "QUERY_RECORD_LIMIT_REACHED"
    assert generated.generator_metadata["gdsmith_returncode"] == 1


def test_formal_gdsmith_seam_accepts_query_limit_after_recovered_connection_loss(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=1,
            process_returncode=0,
            fatal_stderr_markers=("DatabaseConnectionLost",),
            query_record_limit_reached=True,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
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
    assert generated.generator_metadata["generation_stop_reason"] == "QUERY_RECORD_LIMIT_REACHED"


@pytest.mark.parametrize("process_returncode", [0, 1])
def test_formal_gdsmith_seam_rejects_query_limit_with_oom_marker(
    tmp_path,
    monkeypatch,
    process_returncode,
) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=1,
            process_returncode=process_returncode,
            fatal_stderr_markers=("OutOfMemoryError",),
            query_record_limit_reached=True,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    with pytest.raises(RuntimeError, match="GDsmith failed with code 1"):
        worker._generate_with_gdsmith(
            ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60, "neo4j"),
            tmp_path,
        )


def test_formal_gdsmith_seam_rejects_ordinary_nonzero_exit(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=1,
            process_returncode=1,
            query_record_limit_reached=False,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    with pytest.raises(RuntimeError, match="GDsmith failed with code 1"):
        worker._generate_with_gdsmith(
            ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60, "neo4j"),
            tmp_path,
        )


def test_formal_gdsmith_seam_rejects_empty_query_limit_corpus(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=1,
            process_returncode=1,
            query_record_limit_reached=True,
            artifact_budget_exceeded=False,
            generation_stalled=False,
            parsed=SimpleNamespace(graph_statements=(), accepted_queries=()),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    with pytest.raises(RuntimeError, match="without a replayable query"):
        worker._generate_with_gdsmith(
            ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60, "neo4j"),
            tmp_path,
        )


def test_generation_query_limit_is_capability_driven() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    assert _generation_query_limit(
        ShardTask(TargetSystem.NEO4J, 0, 7, 1000, 10000, 60, "neo4j"),
        spec,
    ) == 2000
    assert _generation_query_limit(
        ShardTask(TargetSystem.AGENSGRAPH, 0, 7, 1000, 10000, 60, "agensgraph"),
        spec,
    ) == 5000
    assert _generation_query_limit(
        ShardTask(TargetSystem.ARCADEDB, 0, 7, 1000, 10000, 60, "arcadedb"),
        spec,
    ) == 5000
    assert _generation_query_limit(
        ShardTask(TargetSystem.LADYBUGDB, 0, 7, 1000, 10000, 60, "neo4j_proxy"),
        spec,
    ) == 10000
    assert _generation_query_limit(
        ShardTask(TargetSystem.ARCADEDB, 0, 7, 10, 100, 60, "arcadedb"),
        spec,
    ) == 100


def test_rule_entry_generation_uses_its_full_immutable_seed_budget() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    entry = _rule_plan_entry(accepted_pair_quota=1, max_generated_seed_budget=17)

    assert _generation_query_limit(entry, spec) == 17


def test_formal_gdsmith_seam_keeps_replayable_corpus_at_raw_artifact_budget(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(statement="MATCH (n) RETURN n")

    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=125,
            artifact_budget_exceeded=True,
            generation_stalled=False,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:Node {id: 1})",),
                accepted_queries=(record,),
            ),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    generated = worker._generate_with_gdsmith(
        ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1000, 60, "neo4j"),
        tmp_path,
    )

    assert generated.query_texts == (record.statement,)
    assert generated.generator_metadata["generation_stop_reason"] == "RAW_ARTIFACT_BUDGET_REACHED"
    assert generated.generator_metadata["generated_query_record_limit"] == 2


def test_formal_gdsmith_seam_rejects_empty_corpus_at_raw_artifact_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.run_gdsmith",
        lambda **_kwargs: SimpleNamespace(
            returncode=125,
            artifact_budget_exceeded=True,
            generation_stalled=False,
            parsed=SimpleNamespace(graph_statements=(), accepted_queries=()),
        ),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://localhost:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )

    with pytest.raises(RuntimeError, match="without a replayable query"):
        worker._generate_with_gdsmith(
            ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1000, 60, "neo4j"),
            tmp_path,
        )


@pytest.mark.parametrize(
    ("target", "environment", "backend", "planned_backend"),
    [
        (
            TargetSystem.AGENSGRAPH,
            {
                "GRAPHSYMPERF_AGENSGRAPH_URI": "postgresql://postgres:secret@127.0.0.1:5432/postgres",
            },
            "agensgraph",
            "agensgraph",
        ),
        (
            TargetSystem.APACHE_AGE,
            {
                "GRAPHSYMPERF_GDSMITH_PROXY_APACHE_AGE_URI": (
                    "postgresql://postgres:secret@127.0.0.1:5433/postgres"
                ),
            },
            "agensgraph",
            "agensgraph_proxy",
        ),
        (
            TargetSystem.ARCADEDB,
            {
                "GRAPHSYMPERF_GDSMITH_PROXY_ARCADEDB_URI": "bolt://127.0.0.1:7698",
            },
            "neo4j",
            "neo4j_proxy",
        ),
        (
            TargetSystem.LADYBUGDB,
            {
                "GRAPHSYMPERF_GDSMITH_PROXY_LADYBUGDB_URI": "bolt://127.0.0.1:7697",
            },
            "neo4j",
            "neo4j_proxy",
        ),
    ],
    ids=["agensgraph", "apache-age-proxy", "arcadedb-proxy", "ladybugdb-proxy"],
)
def test_single_label_generation_backends_are_constrained_at_the_source(
    tmp_path,
    monkeypatch,
    target,
    environment,
    backend,
    planned_backend,
) -> None:
    record = SimpleNamespace(statement="MATCH (n:L0) RETURN n.id")

    def fake_run_gdsmith(**kwargs):
        assert kwargs["backend"] == backend
        assert kwargs["config"].backend_extra_args == {backend: ("--label-num", "1")}
        return SimpleNamespace(
            returncode=0,
            parsed=SimpleNamespace(
                graph_statements=("CREATE (n:L0 {id: 1})",),
                accepted_queries=(record,),
            ),
        )

    monkeypatch.setattr("graph_sym_perf.experiment.worker.run_gdsmith", fake_run_gdsmith)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        environment=environment,
    )

    generated = worker._generate_with_gdsmith(
        ShardTask(target, 0, 7, 1, 2, 60, planned_backend),
        tmp_path,
    )

    assert generated.query_texts == (record.statement,)


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
    assert result["acceptance_generated_seeds"] == 1
    assert result["generated_query_records"] == 2
    assert adapter.loaded == shard.graph_statements
    assert adapter.closed
    assert all(kind != "profile" for _, kind in adapter.executed)
    records = [json.loads(line) for line in (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["equivalence"] == "complete_typed_equivalent" for record in records)
    assert all(
        set(record) <= {
            "schema_version", "level", "measurement_protocol", "pair_hash", "query_hash",
            "rule_id", "site", "equivalence", "screen_runtime_ms", "screen_ratio", "error_kind",
        }
        for record in records
    )
    assert all(record["measurement_protocol"] == "accepted-equivalence-v3" for record in records)
    identity = json.loads(
        (tmp_path / "shards" / "neo4j-000" / "accepted_identity.json").read_text(encoding="utf-8")
    )
    assert identity["graph_snapshot_hash"] == result["graph_snapshot_ref"]["content_hash"]
    assert identity["dbms_fingerprint"] == "a" * 64


def test_worker_writes_observed_graph_manifest_and_binds_summary_identity(tmp_path) -> None:
    statements = ("CREATE (n:Node {id: 1, value: 1})",)
    shard = GeneratedShard(
        graph_statements=statements,
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": 256},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    manifest_path = tmp_path / "shards" / "neo4j-000" / "attempt-000" / "graph_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "actual_nodes": 1,
        "actual_relationships": 0,
        "graph_fingerprint": GraphSnapshot.from_statements(statements).content_hash,
        "index_count": 0,
        "label_count": 1,
        "relationship_type_count": 0,
        "requested_max_nodes": 256,
    }
    assert result["requested_max_nodes"] == 256
    assert result["actual_nodes"] == 1
    assert result["graph_fingerprint"] == manifest["graph_fingerprint"]
    identity = json.loads(
        (tmp_path / "shards" / "neo4j-000" / "accepted_identity.json").read_text(encoding="utf-8")
    )
    assert identity["graph_snapshot_hash"] == manifest["graph_fingerprint"]


def test_worker_fails_closed_when_observed_graph_manifest_is_invalid(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": 128},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _InvalidGraphManifestAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert captured.value.kind is ErrorKind.INFRASTRUCTURE_ERROR
    assert "graph manifest" in str(captured.value)
    assert not (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl").exists()


def test_worker_classifies_invalid_requested_graph_scale_as_infrastructure_failure(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": "128"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert captured.value.kind is ErrorKind.INFRASTRUCTURE_ERROR
    assert "requested_max_nodes" in str(captured.value)


def test_legacy_custom_generation_without_requested_scale_does_not_fabricate_manifest(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _LegacyGenerationAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert result["acceptance_status"] == "complete"
    assert "requested_max_nodes" not in result
    assert not (
        tmp_path / "shards" / "neo4j-000" / "attempt-000" / "graph_manifest.json"
    ).exists()


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


@pytest.mark.parametrize(
    ("entry", "query"),
    (
        (
            _rule_plan_entry(rule_id="null_partition_tautology", family="bool"),
            "MATCH (n:Node) WHERE n.value = 1 RETURN n.value",
        ),
        (
            _rule_plan_entry(rule_id="pattern_order_symmetry", family="sym"),
            "MATCH (a:Node), (b:Node) RETURN a, b",
        ),
    ),
)
def test_worker_builds_each_rule_plan_entry_with_its_own_certified_rule(
    tmp_path,
    entry,
    query,
) -> None:
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
            query_texts=(query,),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 1
    assert result["acceptance_status"] == "complete"
    assert result["terminal_state"] == "building_accepted_pairs"
    assert result["rule_id"] == entry.rule_id
    assert result["stream"] == "discovery"
    assert result["rule_stats"] == {
        "accepted": {entry.rule_id: 1},
        "not_applicable": {},
    }


def test_worker_never_transfers_an_underfilled_rule_budget(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="pattern_order_symmetry",
        family="sym",
        accepted_pair_quota=2,
        max_generated_seed_budget=1,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 0
    assert result["terminal_state"] == "underfilled"
    assert result["rule_id"] == "pattern_order_symmetry"
    assert result["reject_reasons"] == {"RULE_NOT_APPLICABLE": 1}


def test_worker_stops_at_the_entry_wall_clock_deadline(tmp_path) -> None:
    entry = _rule_plan_entry(max_entry_runtime_seconds=600)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
        clock=_SteppedClock(0.0, 0.0, 601.0),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 0
    assert result["terminal_state"] == "resource_exhausted"


def test_rule_entry_counts_only_real_generated_query_records(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        max_generated_seed_budget=5,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "underfilled"
    assert result["acceptance_generated_seeds"] == 1
    assert result["generated_query_records"] == 1
    assert result["reject_reasons"] == {"RULE_NOT_APPLICABLE": 1}


def test_worker_quarantines_one_core_entry_immediately_on_typed_mismatch(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _EquivalenceMismatchAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 0
    assert result["terminal_state"] == "quarantined"
    counterexample = next(tmp_path.glob("entries/*/rule_quarantine.jsonl")).read_text(encoding="utf-8")
    assert "semantic_proof_sha256" in counterexample
    assert entry.rule_id in counterexample


def test_worker_rejects_ordinary_query_error_without_failing_the_entry_infrastructure(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        max_generated_seed_budget=1,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _OrdinaryQueryErrorAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "underfilled"
    assert result["reject_reasons"] == {"QUERY_EXECUTION_ERROR": 1}


def test_worker_classifies_connection_loss_as_entry_infrastructure_failure(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _ConnectionLostAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["accepted_pairs"] == 0


def test_query_execution_error_is_ordinary_even_when_message_mentions_infrastructure(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        max_generated_seed_budget=1,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _ScaryOrdinaryQueryErrorAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "underfilled"
    assert result["reject_reasons"] == {"QUERY_EXECUTION_ERROR": 1}


@pytest.mark.parametrize("failure_site", ("generation", "adapter_factory", "adapter_close"))
def test_rule_entry_lifecycle_infrastructure_failures_return_typed_incomplete_summary(
    tmp_path,
    failure_site,
) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )

    def generate(_entry, _raw_dir):
        if failure_site == "generation":
            raise MemoryError("GDsmith exhausted memory")
        return generated

    def create(_entry):
        if failure_site == "adapter_factory":
            raise OSError("driver connection failed")
        return _CloseFailureAdapter() if failure_site == "adapter_close" else _Adapter()

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=generate,
        adapter_factory=create,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["acceptance_status"] == "incomplete"
    assert result["infrastructure_error_kind"] == ErrorKind.INFRASTRUCTURE_ERROR.value
    assert result["infrastructure_reason"]


@pytest.mark.parametrize("failure_site", ("generation", "adapter_factory"))
def test_early_lifecycle_failure_summary_preserves_durable_progress(tmp_path, failure_site) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=2,
        max_generated_seed_budget=2,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(
            "MATCH (n) WHERE n.id = 1 RETURN n",
            "MATCH (n) WHERE n.id = 2 RETURN n",
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _AcceptThenInfrastructureAdapter(),
    )
    first.bind_run_dir(tmp_path)
    first_result = first(entry)
    assert first_result["accepted_pairs"] == 1
    assert first_result["acceptance_generated_seeds"] == 1

    if failure_site == "generation":
        next(tmp_path.glob("entries/*/generation_corpus.json")).unlink()

    def fail_generation(_entry, _raw_dir):
        raise MemoryError("generation initialization failed")

    def fail_adapter(_entry):
        raise OSError("adapter initialization failed")

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=fail_generation if failure_site == "generation" else lambda *_args: generated,
        adapter_factory=fail_adapter,
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["accepted_pairs"] == 1
    assert result["acceptance_generated_seeds"] == 1
    assert result["reject_reasons"] == {}
    assert result["rule_stats"]["accepted"] == {entry.rule_id: 1}


@pytest.mark.parametrize("adapter_type", (_FinalFingerprintDriftAdapter, _FinalGraphDriftAdapter))
def test_final_identity_drift_invalidates_completed_rule_entry_acceptance(tmp_path, adapter_type) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: adapter_type(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 1
    assert result["terminal_state"] == "infrastructure_failed"
    assert result["acceptance_status"] == "incomplete"
    assert result["acceptance_stop_reason"] == "INFRASTRUCTURE_FAILED"
    assert result["acceptance_invalidated"] is True


def test_rule_entry_retry_resumes_durable_accepted_pairs_without_double_counting(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    for _attempt in range(2):
        worker = RuntimeFirstShardWorker(
            load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
            generation_runner=lambda _entry, _raw_dir: generated,
            adapter_factory=lambda _entry: _Adapter(),
        )
        worker.bind_run_dir(tmp_path)
        result = worker(entry)
        assert result["accepted_pairs"] == 1
        assert result["acceptance_status"] == "complete"
        assert result["terminal_state"] == "building_accepted_pairs"

    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["query_a"] and records[0]["query_b"]
    assert records[0]["parameters"] == {}
    assert records[0]["certificate"]["canonical_rule_id"] == entry.rule_id
    assert records[0]["transform_metadata"]["semantic_proof_sha256"]


def test_rule_entry_retry_keeps_reject_progress_and_does_not_repeat_completed_seed(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=1,
        max_generated_seed_budget=2,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(
            "MATCH (n) WHERE n.id = 1 RETURN n",
            "MATCH (n) WHERE n.id = 2 RETURN n",
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _RejectThenInfrastructureAdapter(),
        clock=_ManualClock(10.0),
    )
    first.bind_run_dir(tmp_path)

    first_result = first(entry)

    assert first_result["terminal_state"] == "infrastructure_failed"
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    first_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert first_progress["last_query_ordinal"] == 1
    assert first_progress["reject_counts"] == {"QUERY_EXECUTION_ERROR": 1}

    retry_adapter = _Adapter()
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: retry_adapter,
        clock=_ManualClock(1000.0),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    accepted_base_queries = [query for query, kind in retry_adapter.executed if kind == "acceptance_base"]
    assert accepted_base_queries == ["MATCH (n) WHERE n.id = 2 RETURN n"]
    assert result["acceptance_status"] == "complete"
    assert result["acceptance_generated_seeds"] == 2
    assert result["reject_reasons"] == {"QUERY_EXECUTION_ERROR": 1}
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["elapsed_seconds"] < 10.0


def test_rule_entry_retry_enforces_cumulative_elapsed_budget_without_absolute_clock(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        max_generated_seed_budget=1,
        max_entry_runtime_seconds=5,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first_clock = _ManualClock(20.0)
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _TimedQueryErrorAdapter(first_clock, 5.0),
        clock=first_clock,
    )
    first.bind_run_dir(tmp_path)
    first(entry)

    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["elapsed_seconds"] == 5.0
    assert progress["last_query_ordinal"] == 1

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("exhausted retry must not regenerate"),
        adapter_factory=lambda *_args: pytest.fail("exhausted retry must not reconnect"),
        clock=_ManualClock(5000.0),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "resource_exhausted"
    assert result["acceptance_generated_seeds"] == 1
    assert result["reject_reasons"] == {"QUERY_EXECUTION_ERROR": 1}
    assert json.loads(progress_path.read_text(encoding="utf-8"))["elapsed_seconds"] == 5.0


def test_rule_entry_progress_includes_final_identity_and_adapter_close_elapsed(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    clock = _ManualClock(50.0)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _TimedCloseAdapter(clock, 3.0),
        clock=clock,
    )
    worker.bind_run_dir(tmp_path)

    worker(entry)

    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["elapsed_seconds"] == 3.0


def test_retry_recovers_only_the_validated_single_record_crash_window(tmp_path, monkeypatch) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    real_persist = worker_module._persist_entry_progress

    class SimulatedProcessCrash(BaseException):
        pass

    crashed = False

    def crash_after_accepted_append(path, **kwargs):
        nonlocal crashed
        if kwargs["accepted_hashes"] and not crashed:
            crashed = True
            raise SimulatedProcessCrash("accepted JSONL fsynced before progress replace")
        return real_persist(path, **kwargs)

    monkeypatch.setattr(worker_module, "_persist_entry_progress", crash_after_accepted_append)
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)

    with pytest.raises(SimulatedProcessCrash):
        first(entry)

    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    record = json.loads(records_path.read_text(encoding="utf-8"))
    assert record["source_query_ordinal"] == 1
    assert not next(tmp_path.glob("entries/*")).joinpath("acceptance_progress.json").exists()

    monkeypatch.setattr(worker_module, "_persist_entry_progress", real_persist)
    retry_adapter = _Adapter()
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: retry_adapter,
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["acceptance_status"] == "complete"
    assert [kind for _, kind in retry_adapter.executed if kind == "acceptance_base"] == []
    progress = json.loads(
        next(tmp_path.glob("entries/*/acceptance_progress.json")).read_text(encoding="utf-8")
    )
    assert progress["last_query_ordinal"] == 1
    assert progress["accepted_pair_hashes"] == [record["pair_hash"]]


@pytest.mark.parametrize("corruption", ("progress_ahead", "non_contiguous", "multi_step"))
def test_retry_rejects_non_single_step_accepted_progress_divergence(tmp_path, corruption) -> None:
    quota = 2 if corruption == "multi_step" else 1
    queries = (
        "MATCH (n) WHERE n.id = 1 RETURN n",
        (
            "MATCH (n) WHERE n.id = 2 RETURN n"
            if corruption == "multi_step"
            else "MATCH (n) WHERE n.id = 1 RETURN n"
        ),
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=quota,
        max_generated_seed_budget=2,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    if corruption == "progress_ahead":
        records_path.write_text("", encoding="utf-8")
    else:
        progress_path.unlink()
        if corruption == "non_contiguous":
            record = json.loads(records_path.read_text(encoding="utf-8"))
            record["source_query_ordinal"] = 2
            records_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize("probe", ("duplicate_query_ordinal_ahead", "rehashed_progress_behind"))
def test_retry_rejects_durable_ordinal_ahead_of_matching_progress_hash_set(tmp_path, probe) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=1,
        max_generated_seed_budget=2,
    )
    duplicate_query = "MATCH (n) WHERE n.id = 1 RETURN n"
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(duplicate_query, duplicate_query),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    if probe == "duplicate_query_ordinal_ahead":
        record = json.loads(records_path.read_text(encoding="utf-8"))
        record["source_query_ordinal"] = 2
        records_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    else:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        body = {key: value for key, value in progress.items() if key != "content_hash"}
        body["last_query_ordinal"] = 0
        body["attempted_query_records"] = 0
        progress_path.write_text(
            json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
            encoding="utf-8",
        )

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize("corruption", ("truncated", "non_object"))
def test_retry_treats_malformed_accepted_jsonl_as_evidence_integrity_failure(tmp_path, corruption) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    if corruption == "truncated":
        records_path.write_text(
            records_path.read_text(encoding="utf-8")[:-7],
            encoding="utf-8",
        )
    else:
        records_path.write_text("[]\n", encoding="utf-8")

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize(
    "tamper",
    (
        "query_b",
        "certificate_hash",
        "proof_theorem",
        "pair_hash",
        "query_hash",
        "entry_id",
        "family",
        "tier",
        "stream",
        "site",
        "parameters",
        "extra_metadata",
        "source_query_ordinal",
    ),
)
def test_rule_entry_retry_recomputes_every_accepted_record_binding(tmp_path, tamper) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/accepted_pairs.jsonl"))
    record = json.loads(records_path.read_text(encoding="utf-8"))
    if tamper == "query_b":
        record["query_b"] += " "
    elif tamper == "certificate_hash":
        record["certificate"]["semantic_proof_hash"] = "0" * 64
    elif tamper == "proof_theorem":
        record["transform_metadata"]["semantic_proof"]["theorem"] = "Forged"
    elif tamper in {"pair_hash", "query_hash"}:
        record[tamper] = "0" * 64
    elif tamper == "parameters":
        record["parameters"]["forged"] = 1
    elif tamper == "extra_metadata":
        record["transform_metadata"]["forged"] = "metadata"
    else:
        record[tamper] = "forged"
    records_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_worker_quarantines_each_core_rule_after_dynamic_equivalence_mismatch(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(
            "MATCH (n) WHERE n.id = 1 RETURN n",
            "MATCH (n) WHERE n.id = 2 RETURN n",
        ),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _EquivalenceMismatchAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 2, 60))

    quarantine = (tmp_path / "shards" / "neo4j-000" / "rule_quarantine.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert result["accepted_pairs"] == 0
    assert result["reject_reasons"] == {"TYPED_EQUIVALENCE_FAILED": 4}
    assert len(quarantine) == 4


def test_worker_does_not_quarantine_rule_when_typed_comparison_is_incomplete(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _UnsupportedEquivalenceAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert result["accepted_pairs"] == 0
    assert result["reject_reasons"] == {"UNSUPPORTED_TYPED_RESULT_VALUE": 2}
    assert not (tmp_path / "shards" / "neo4j-000" / "rule_quarantine.jsonl").exists()


def test_worker_skips_query_bound_to_a_prior_hard_timeout(tmp_path) -> None:
    query = "MATCH (n) WHERE n.id = 1 RETURN n"
    graph_statements = ("CREATE (n:Node {id: 1})",)
    shard_dir = tmp_path / "shards" / "neo4j-000"
    shard_dir.mkdir(parents=True)
    (shard_dir / "query_timeout_quarantine.jsonl").write_text(
        json.dumps(
            {
                "attempt": 0,
                "dbms_fingerprint": "a" * 64,
                "error_kind": "QUERY_TIMEOUT",
                "graph_hash": _graph_snapshot_hash(graph_statements),
                "query_hash": _query_hash(query),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = _Adapter()
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: GeneratedShard(
            graph_statements=graph_statements,
            query_texts=(query,),
            generator_metadata={"backend": "gdsmith"},
        ),
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert result["accepted_pairs"] == 0
    assert result["reject_reasons"] == {"QUERY_TIMEOUT_QUARANTINED": 2}
    assert not any(kind.startswith("acceptance_") for _query, kind in adapter.executed)


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
    assert duplicate["reject_reasons"] == {"CROSS_SHARD_DUPLICATE_AST": 2}


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
    assert records[0]["rule_id"] == "constant_true_disjunction"
    assert records[0]["safety_certificate"]["proof_version"] == "kleene_or_true_v1"
    assert records[0]["graph_snapshot_hash"]
    assert records[0]["dbms_fingerprint"] == "a" * 64
    assert records[0]["graph_snapshot_ref"]["content_hash"] == records[0]["graph_snapshot_hash"]
    assert len(records[0]["discovery_paired_timing"]) == 5
    assert records[0]["candidate_hash"] in result["candidate_hashes"]
    assert records[0]["candidate_hash"] != records[0]["pair_hash"]
    assert records[0]["content_hashes"]["graph_snapshot"] == records[0]["graph_snapshot_hash"]
    store = ContentAddressedStore(
        tmp_path / "shards" / "neo4j-000" / "artifacts",
        budget_bytes=500 * 1024 * 1024,
    )
    assert verify_candidate_record_reference(store, records[0]).payload["candidate_hash"] == records[0]["candidate_hash"]
    candidate_path = tmp_path / "shards" / "neo4j-000" / "candidates.jsonl"
    forged = dict(records[0])
    forged["query_a"] += " "
    candidate_path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    with pytest.raises((OSError, ValueError), match="candidate|query text"):
        candidate_artifact_hashes(tmp_path / "shards" / "neo4j-000", required=True)


def test_formal_worker_runs_every_candidate_through_terminal_validation_and_reduction(tmp_path) -> None:
    observed = []

    class _Pipeline:
        def process(self, candidate, **context):
            observed.append((candidate, context))
            return {
                "candidate_hash": candidate.candidate_hash,
                "validation": {"stage": "rejected", "reason_codes": ["ratio_below_threshold"]},
                "reduction": {"status": "not_applicable", "reason": "validation_not_passed"},
            }

    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _RuntimeGapAdapter(),
        candidate_pipeline=_Pipeline(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert len(observed) == 1
    assert set(result["stage_evidence"]) == {"screening", "validation", "reduction"}
    attempt = tmp_path / "shards" / "neo4j-000" / "attempt-000"
    validation = json.loads((attempt / "validation.json").read_text(encoding="utf-8"))
    reduction = json.loads((attempt / "reduction.json").read_text(encoding="utf-8"))
    assert validation["candidate_count"] == 1
    assert validation["terminal_count"] == 1
    assert reduction["terminal_count"] == 1
    assert reduction["outcomes"][0]["status"] == "not_applicable"

    class _MustNotRunAgain:
        def process(self, *_args, **_kwargs):
            raise AssertionError("resume must reuse the verified terminal candidate outcome")

    resume_attempt = tmp_path / "shards" / "neo4j-000" / "attempt-001"
    resume_attempt.mkdir()
    resumed_worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
        candidate_pipeline=_MustNotRunAgain(),
    )
    resumed_worker.bind_run_dir(tmp_path)
    resumed_worker.bind_attempt_dir(resume_attempt)

    resumed = resumed_worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert resumed["candidate_hashes"] == result["candidate_hashes"]
    assert (tmp_path / "shards" / "neo4j-000" / "candidate-outcomes").is_dir()


def test_resume_does_not_reinterpret_a_durable_non_candidate_as_a_new_candidate(tmp_path) -> None:
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )

    class _NoCandidatePipeline:
        def process(self, *_args, **_kwargs):
            raise AssertionError("a durable non-candidate must never enter validation")

    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _Adapter(),
        candidate_pipeline=_NoCandidatePipeline(),
    )
    first.bind_run_dir(tmp_path)
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)

    initial = first(task)

    attempt = tmp_path / "shards" / "neo4j-000" / "attempt-001"
    attempt.mkdir()
    resumed_worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: _RuntimeGapAdapter(),
        candidate_pipeline=_NoCandidatePipeline(),
    )
    resumed_worker.bind_run_dir(tmp_path)
    resumed_worker.bind_attempt_dir(attempt)
    resumed = resumed_worker(task)

    assert initial["screened_candidates"] == resumed["screened_candidates"] == 0
    assert (tmp_path / "shards" / "neo4j-000" / "screened_non_candidates.jsonl").is_file()


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
        "schema_reset",
        "schema_setup",
        "graph_load",
        "acceptance_base",
        "acceptance_transformed",
        "runtime",
    }
    assert all(record["query_hash"] and record["deadline"] > record["started_at"] for record in records)
    assert all(
        record["deadline"] - record["started_at"]
        == pytest.approx(record["operation_timeout_seconds"] + record["cancellation_grace_seconds"])
        for record in records
    )
    assert not (attempt_dir / "inflight.json").exists()
    assert load_adapter.closed and measurement_adapter.closed


def test_worker_prefers_database_level_reset_over_graph_wide_delete() -> None:
    adapter = _DatabaseResetAdapter()
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda *_args: None,
        adapter_factory=lambda _task: adapter,
    )

    worker._reset_and_load(adapter, ("CREATE (n:Node {id: 1})",))

    assert adapter.database_resets == 1
    assert adapter.loaded == ("CREATE (n:Node {id: 1})",)


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


def test_measurement_adapter_connects_before_reading_its_dbms_fingerprint(tmp_path) -> None:
    load = _FingerprintAdapter("a" * 64)
    measurement = _ConnectionPrimedFingerprintAdapter("a" * 64)
    adapters = iter((load, measurement))
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (n) WHERE n.value = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith"},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: next(adapters),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert measurement.connected
    assert result["acceptance_status"] == "complete"


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


def test_retry_reuses_the_sealed_generation_corpus_instead_of_regenerating_a_new_graph(tmp_path) -> None:
    calls = 0
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_texts=("MATCH (a:Node), (b:Node) RETURN a, b",),
        generator_metadata={"backend": "gdsmith", "generation_seed": 7},
    )

    def generate(_task, _raw_dir):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("retry must not invoke GDsmith again")
        return generated

    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=generate,
        adapter_factory=lambda _task: (_ for _ in ()).throw(RuntimeError("injected load crash")),
    )
    first.bind_run_dir(tmp_path)
    with pytest.raises(RuntimeError, match="injected load crash"):
        first(task)

    shard_dir = tmp_path / "shards" / "neo4j-000"
    assert (shard_dir / "generation_corpus.json").is_file()

    attempt = shard_dir / "attempt-001"
    attempt.mkdir()
    second = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=generate,
        adapter_factory=lambda _task: _Adapter(),
    )
    second.bind_run_dir(tmp_path)
    second.bind_attempt_dir(attempt)

    result = second(task)

    assert calls == 1
    assert result["graph_snapshot_ref"]["content_hash"] == _graph_snapshot_hash(generated.graph_statements)
    reuse = json.loads((attempt / "generation_reuse.json").read_text(encoding="utf-8"))
    assert reuse["reused"] is True


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
