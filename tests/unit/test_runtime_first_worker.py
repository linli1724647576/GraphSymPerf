import json
import hashlib
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

import graph_sym_perf.experiment.worker as worker_module

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import ContentAddressedStore, verify_candidate_record_reference
from graph_sym_perf.core import (
    ExecutionResult,
    QueryExecutionError,
    QueryTimeoutError,
    TargetSystem,
    stable_json_dumps,
)
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    FrozenMapping,
    RulePlanEntry,
    RuleStream,
    ShardTask,
)
from graph_sym_perf.experiment.evidence_validation import candidate_artifact_hashes, canonical_hash
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.experiment.worker import (
    FormalRuntimeFirstShardWorker,
    GeneratedShard,
    RuntimeFirstShardWorker,
    WorkerFailure,
    _gdsmith_connection,
    _generation_backend_provenance,
    _generation_query_limit,
    _graph_snapshot_hash,
    _query_hash,
    _load_terminal_candidate_outcome,
    _validated_pair_yield,
)
from graph_sym_perf.generation.corpus import GraphCorpus
from graph_sym_perf.runtime.models import (
    NoiseCalibration,
    PairedSample,
    RuntimeEvidence,
    RuntimeVerdict,
)
from graph_sym_perf.runtime.screening import (
    DurableTimeoutQueue,
    Stage2Candidate,
    Stage2Decision,
    TimeoutQueueRecord,
    select_control_validation_pairs,
)
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.session_runner import validation_schedule_seed
from graph_sym_perf.validation.models import (
    EquivalenceEvidence,
    SessionLifecycle,
    ValidationSession,
    canonical_validation_report_hash,
)
from tests.helpers.regression_gate import (
    build_verified_regression_gate_template,
    install_verified_regression_gate,
)
from tests.unit.test_artifact_policy import _candidate_payload, _validation_payload


def test_validated_pair_yield_uses_all_core_accepted_pairs() -> None:
    point, ci_low = _validated_pair_yield(successes=1, accepted_pairs=200)

    assert point == pytest.approx(0.005)
    assert 0.0 < ci_low < point


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


def _install_valid_noise_stub(monkeypatch) -> None:
    noise = NoiseCalibration(
        valid=True,
        sample_count=200,
        p95_delta_ms=0.1,
        p99_delta_ms=0.2,
        median_order_bias_ms=0.0,
        long_tail_ratio=1.0,
        reason_codes=(),
    )
    result = (noise, "f" * 64)
    probe_hashes = tuple(f"{index:064x}" for index in range(1, 21))

    def fixed_noise(*_args, **kwargs):
        if kwargs["anchored_noise_floor_id"] is None:
            kwargs["persist_intent"]("f" * 64, probe_hashes)
        return result

    monkeypatch.setattr(worker_module, "_load_entry_noise", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        worker_module,
        "_load_or_calibrate_entry_noise",
        fixed_noise,
    )


@pytest.fixture(scope="module")
def _verified_regression_gate_template(tmp_path_factory):
    return build_verified_regression_gate_template(
        tmp_path_factory.mktemp("runtime-worker-regression-gate")
    )


@pytest.fixture(autouse=True)
def _verified_regression_gate(
    monkeypatch,
    _verified_regression_gate_template,
) -> None:
    install_verified_regression_gate(
        monkeypatch,
        worker_module,
        _verified_regression_gate_template,
    )


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


class _Stage2WideGapAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **_kwargs) -> ExecutionResult:
        result = super().execute(query, parameters, query_kind, **_kwargs)
        if query_kind != "runtime":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=31.0 if "OR true" in query else 1.0,
            summary=result.summary,
        )


class _NoRuntimeAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs) -> ExecutionResult:
        if query_kind == "runtime":
            raise AssertionError("durable Stage-2 evidence must be reused")
        return super().execute(query, parameters, query_kind, **kwargs)


class _InterruptedNoiseAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs):
        result = super().execute(query, parameters, query_kind, **kwargs)
        if query_kind != "runtime":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=float("nan"),
            summary=result.summary,
        )


class _NonzeroBalancedNoiseAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.runtime_calls = 0

    def execute(self, query, parameters, query_kind, **kwargs):
        result = super().execute(query, parameters, query_kind, **kwargs)
        if query_kind != "runtime":
            return result
        self.runtime_calls += 1
        elapsed = (1.0, 1.1, 1.1, 1.0)[(self.runtime_calls - 1) % 4]
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=elapsed,
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


def test_formal_schema_v3_worker_installs_production_candidate_pipeline(tmp_path) -> None:
    worker = FormalRuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        environment={},
    )

    worker.bind_run_dir(tmp_path)

    assert worker._candidate_pipeline is not None
    assert type(worker._candidate_pipeline).__name__ == "ProductionCandidatePipeline"


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
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    assert result["terminal_state"] == "stage2_complete"
    assert result["rule_id"] == entry.rule_id
    assert result["stream"] == "discovery"
    assert result["rule_stats"] == {
        "accepted": {entry.rule_id: 1},
        "not_applicable": {},
    }
    entry_dir = next(tmp_path.glob("entries/*"))
    records = [
        json.loads(line)
        for line in (entry_dir / "pair_runtime.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["pair_hash"] == result["accepted_pair_hashes"][0]
    assert not (entry_dir / "accepted_pairs.jsonl").exists()
    assert "query_a" not in records[0] and "query_b" not in records[0]


def test_rule_entry_stage1_timeout_is_durably_counted_without_query_text(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")

    class Stage1TimeoutAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime" and "IS NULL" in query:
                raise QueryTimeoutError("stage1 timeout")
            return super().execute(query, parameters, query_kind, **kwargs)

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: Stage1TimeoutAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    record = json.loads(next(tmp_path.glob("entries/*/pair_runtime.jsonl")).read_text())
    assert result["accepted_pairs"] == 1
    assert result["terminal_state"] == "stage2_complete"
    assert record["timeout_state"] == "b_timeout"
    assert record["runtime_direction"] == "b"
    assert record["samples_us"]
    assert "query_a" not in record and "query_b" not in record
    timeout_queue = [
        json.loads(line)
        for line in next(tmp_path.glob("entries/*/timeout_queue.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(timeout_queue) == 1
    assert timeout_queue[0]["stage"] == "stage1"
    assert timeout_queue[0]["timeout_side"] == "b"


def test_rule_entry_measures_only_stage1_top_k_with_fixed_stage2_protocol(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 5)
    )
    adapter = _Stage2WideGapAdapter()
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=queries,
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: adapter,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    entry_dir = next(tmp_path.glob("entries/*"))
    candidate_paths = sorted(entry_dir.glob("candidates/*/candidate.json"))
    compact_records = [
        json.loads(line)
        for line in (entry_dir / "pair_runtime.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    selection = json.loads((entry_dir / "stage2_selection.json").read_text(encoding="utf-8"))
    runtime_calls = [kind for _query, kind in adapter.executed if kind == "runtime"]

    assert result["terminal_state"] == "stage2_complete"
    assert result["stage1_selected_pairs"] == 3
    assert result["stage2_measured_pairs"] == 3
    assert result["stage2_promoted_pairs"] == 3
    assert len(compact_records) == 4
    assert len(candidate_paths) == 3
    assert len(runtime_calls) == 4 * (2 * 2 + 3 * 2) + 3 * (3 * 2 + 12 * 2)
    assert len(selection["selected_pair_hashes"]) == 2
    assert selection["validation_limit"] == 2
    assert not (entry_dir / "accepted_pairs.jsonl").exists()

    candidate = json.loads(candidate_paths[0].read_text(encoding="utf-8"))
    assert candidate["entry_identity"]["entry_id"] == entry.entry_id
    assert candidate["evidence_identity"] == {
        "dbms_fingerprint": "a" * 64,
        "dbms_health_epoch": "e" * 64,
        "graph_snapshot_hash": _graph_snapshot_hash(("CREATE (n:Node {id: 1})",)),
    }
    assert candidate["noise_floor_id"] == "f" * 64
    assert len(candidate["stage1_record_digest"]) == 64
    assert candidate["stage2_protocol"]["warmups"] == 3
    assert candidate["stage2_protocol"]["pairs"] == 12
    assert candidate["stage2_protocol"]["require_exact_balance"] is True
    assert candidate["stage2_protocol"]["bootstrap_resamples"] == 10_000
    assert len(candidate["stage2_protocol"]["seed_sha256"]) == 64
    assert len(candidate["stage2_evidence"]["paired_samples"]) == 12
    assert sum(value["order"] == "AB" for value in candidate["stage2_evidence"]["paired_samples"]) == 6
    assert sum(value["order"] == "BA" for value in candidate["stage2_evidence"]["paired_samples"]) == 6
    assert candidate["stage2_decision"]["promote"] is True
    assert candidate["query_a"] and candidate["query_b"]
    assert candidate["safety_certificate"]
    assert candidate["graph_manifest"]["graph_fingerprint"] == candidate["evidence_identity"]["graph_snapshot_hash"]
    body = {key: value for key, value in candidate.items() if key != "content_hash"}
    assert candidate["content_hash"] == canonical_hash(body)


def test_rule_entry_accepts_complete_zero_noise_calibration_and_uses_epsilon_score(
    tmp_path,
    monkeypatch,
) -> None:
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    zero_noise = NoiseCalibration(
        valid=True,
        sample_count=200,
        p95_delta_ms=0.0,
        p99_delta_ms=0.0,
        median_order_bias_ms=0.0,
        long_tail_ratio=1.0,
        reason_codes=(),
    )
    def fixed_zero_noise(*_args, **kwargs):
        if kwargs["anchored_noise_floor_id"] is None:
            kwargs["persist_intent"](
                "f" * 64,
                tuple(f"{index:064x}" for index in range(1, 21)),
            )
        return zero_noise, "f" * 64

    monkeypatch.setattr(
        worker_module,
        "_load_or_calibrate_entry_noise",
        fixed_zero_noise,
    )
    monkeypatch.setattr(
        worker_module,
        "_load_entry_noise",
        lambda *_args, **_kwargs: (zero_noise, "f" * 64),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    candidate = json.loads(
        next(tmp_path.glob("entries/*/candidates/*/candidate.json")).read_text(
            encoding="utf-8"
        )
    )
    assert result["terminal_state"] == "stage2_complete"
    assert candidate["noise_normalized_score"] == pytest.approx(30_000.0)


@pytest.mark.parametrize(
    ("entry_change", "config_change"),
    (
        ({"query_timeout_seconds": 31}, None),
        ({"accepted_pair_quota": 2, "max_generated_seed_budget": 2}, None),
        (None, ("stage1", "pairs", 4)),
        (None, ("stage2", "relative_ratio", 1.6)),
    ),
)
def test_stage2_resume_rejects_any_entry_or_screening_policy_drift(
    tmp_path,
    monkeypatch,
    entry_change,
    config_change,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"

    retry_entry = replace(entry, **entry_change) if entry_change else entry
    retry_spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    if config_change:
        section, key, value = config_change
        runtime = {name: dict(policy) for name, policy in retry_spec.runtime.items()}
        runtime[section][key] = value
        retry_spec = replace(retry_spec, runtime=FrozenMapping(runtime))
    retry = RuntimeFirstShardWorker(
        retry_spec,
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(retry_entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_stage2_progress_binds_ordered_candidates_and_complete_policy(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=tuple(
                f"MATCH (n) WHERE n.id = {index} RETURN n" for index in range(1, 5)
            ),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    assert worker(entry)["terminal_state"] == "stage2_complete"

    entry_dir = next(tmp_path.glob("entries/*"))
    progress = json.loads((entry_dir / "stage2_progress.json").read_text(encoding="utf-8"))
    assert progress["schema_version"] == 1
    assert progress["entry_payload"] == worker_module.to_jsonable(asdict(entry))
    assert progress["entry_payload_hash"] == canonical_hash(progress["entry_payload"])
    assert progress["selected_pair_hashes"] == [
        item["pair_hash"] for item in progress["candidate_ledger"]
    ]
    assert progress["candidate_intent"] is None
    assert progress["screening_policy"]["normalization"] == {
        "denominator": "max(noise_p99_ms,0.001)",
        "epsilon_ms": 0.001,
    }
    assert progress["screening_policy"]["stage2"]["warmups"] == 3
    assert progress["screening_policy"]["stage2"]["pairs"] == 12
    assert progress["screening_policy"]["stage2"]["bootstrap_resamples"] == 10_000
    assert progress["content_hash"] == canonical_hash(
        {key: value for key, value in progress.items() if key != "content_hash"}
    )


def test_stage2_resume_fails_closed_when_candidates_predate_progress_ledger(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    (entry_dir / "stage2_progress.json").unlink(missing_ok=True)

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize("corruption", ("ledger_reorder", "digest_drift", "orphan_tail"))
def test_stage2_progress_rejects_nontransactional_candidate_history(
    tmp_path,
    monkeypatch,
    corruption,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=tuple(
            f"MATCH (n) WHERE n.id = {index} RETURN n" for index in range(1, 5)
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "stage2_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    if corruption == "ledger_reorder":
        progress["candidate_ledger"][:2] = reversed(progress["candidate_ledger"][:2])
    elif corruption == "digest_drift":
        progress["candidate_ledger"][0]["candidate_digest"] = "0" * 64
    else:
        progress["candidate_ledger"].pop()
        progress["candidate_intent"] = None
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    progress["content_hash"] = canonical_hash(body)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_stage2_progress_recovers_only_exact_intent_bound_durable_tail(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=tuple(
            f"MATCH (n) WHERE n.id = {index} RETURN n" for index in range(1, 5)
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "stage2_progress.json"
    selection_path = entry_dir / "stage2_selection.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    tail = progress["candidate_ledger"].pop()
    progress["candidate_intent"] = {
        "selected_index": len(progress["candidate_ledger"]),
        "pair_hash": tail["pair_hash"],
        "candidate_digest": tail["candidate_digest"],
    }
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    progress["content_hash"] = canonical_hash(body)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    selection_path.unlink()

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    assert retry(entry)["terminal_state"] == "stage2_complete"

    recovered = json.loads(progress_path.read_text(encoding="utf-8"))
    assert recovered["candidate_ledger"][-1] == tail
    assert recovered["candidate_intent"] is None


def _prepare_missing_stage2_candidate_intent(tmp_path, monkeypatch):
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=tuple(
            f"MATCH (n) WHERE n.id = {index} RETURN n" for index in range(1, 5)
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "stage2_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    tail = progress["candidate_ledger"].pop()
    progress["candidate_intent"] = {
        "selected_index": len(progress["candidate_ledger"]),
        "pair_hash": tail["pair_hash"],
        "candidate_digest": tail["candidate_digest"],
    }
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    progress["content_hash"] = canonical_hash(body)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    (entry_dir / "stage2_selection.json").unlink()
    pair_dir = entry_dir / "candidates" / tail["pair_hash"]
    (pair_dir / "candidate.json").unlink()
    return entry, generated, entry_dir, progress_path, pair_dir


def test_missing_candidate_intent_rejects_a_nonempty_unknown_pair_directory(
    tmp_path,
    monkeypatch,
) -> None:
    entry, generated, _entry_dir, _progress_path, pair_dir = (
        _prepare_missing_stage2_candidate_intent(tmp_path, monkeypatch)
    )
    (pair_dir / "unknown.bin").write_bytes(b"foreign")
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR
    assert (pair_dir / "unknown.bin").exists()


def test_missing_candidate_clear_crash_removes_empty_dir_then_third_retry_recovers(
    tmp_path,
    monkeypatch,
) -> None:
    entry, generated, _entry_dir, _progress_path, pair_dir = (
        _prepare_missing_stage2_candidate_intent(tmp_path, monkeypatch)
    )
    real_persist = worker_module._persist_stage2_progress
    crashed = False

    class SimulatedClearCrash(BaseException):
        pass

    def crash_after_clear(path, **kwargs):
        nonlocal crashed
        result = real_persist(path, **kwargs)
        if kwargs["candidate_intent"] is None and not crashed:
            crashed = True
            raise SimulatedClearCrash("cleared intent before remeasurement")
        return result

    monkeypatch.setattr(worker_module, "_persist_stage2_progress", crash_after_clear)
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    with pytest.raises(SimulatedClearCrash):
        retry(entry)

    assert not pair_dir.exists()
    monkeypatch.setattr(worker_module, "_persist_stage2_progress", real_persist)
    third = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    third.bind_run_dir(tmp_path)
    assert third(entry)["terminal_state"] == "stage2_complete"


def test_missing_candidate_cleanup_is_idempotent_after_a_predelete_crash(
    tmp_path,
    monkeypatch,
) -> None:
    entry, generated, _entry_dir, _progress_path, _pair_dir = (
        _prepare_missing_stage2_candidate_intent(tmp_path, monkeypatch)
    )

    class SimulatedPredeleteCrash(BaseException):
        pass

    real_remove = worker_module._remove_empty_stage2_candidate_dir
    monkeypatch.setattr(
        worker_module,
        "_remove_empty_stage2_candidate_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SimulatedPredeleteCrash("before empty directory removal")
        ),
        raising=False,
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(SimulatedPredeleteCrash):
        retry(entry)

    monkeypatch.setattr(
        worker_module,
        "_remove_empty_stage2_candidate_dir",
        real_remove,
    )
    recovered = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    recovered.bind_run_dir(tmp_path)
    assert recovered(entry)["terminal_state"] == "stage2_complete"


def test_stage2_resource_partial_ledger_is_terminal_under_monotonic_budget(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="constant_true_disjunction",
        family="bool",
        accepted_pair_quota=4,
        max_generated_seed_budget=4,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=tuple(
            f"MATCH (n) WHERE n.id = {index} RETURN n" for index in range(1, 5)
        ),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )

    class MonotonicStage2Deadline:
        exhausted = False

        def __call__(self) -> float:
            candidate_count = len(
                tuple(tmp_path.glob("entries/*/candidates/*/candidate.json"))
            )
            if candidate_count == 1:
                self.exhausted = True
            return 601.0 if self.exhausted else 0.0

    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        clock=MonotonicStage2Deadline(),
    )
    first.bind_run_dir(tmp_path)

    first_result = first(entry)

    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "stage2_progress.json"
    partial = json.loads(progress_path.read_text(encoding="utf-8"))
    anchored_path = next(entry_dir.glob("candidates/*/candidate.json"))
    anchored_bytes = anchored_path.read_bytes()
    assert first_result["terminal_state"] == "resource_exhausted"
    assert len(partial["candidate_ledger"]) == 1
    assert partial["candidate_intent"] is None
    assert not (entry_dir / "stage2_selection.json").exists()

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda *_args: pytest.fail("terminal RESOURCE must not reconnect"),
        clock=lambda: 1000.0,
    )
    retry.bind_run_dir(tmp_path)

    retry_result = retry(entry)

    terminal = json.loads(progress_path.read_text(encoding="utf-8"))
    assert retry_result["terminal_state"] == "resource_exhausted"
    assert len(terminal["candidate_ledger"]) == 1
    assert terminal["candidate_intent"] is None
    assert anchored_path.read_bytes() == anchored_bytes
    assert not (entry_dir / "stage2_selection.json").exists()


@pytest.mark.parametrize("corruption", ("query", "sample"))
def test_rule_entry_reuses_valid_stage2_candidates_and_rejects_corruption(
    tmp_path,
    monkeypatch,
    corruption,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)

    assert first(entry)["terminal_state"] == "stage2_complete"

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    assert retry(entry)["terminal_state"] == "stage2_complete"

    candidate_path = next(tmp_path.glob("entries/*/candidates/*/candidate.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if corruption == "query":
        candidate["query_a"] += " "
    else:
        for sample in candidate["stage2_evidence"]["paired_samples"]:
            sample["b_ms"] += 100.0
        body = {key: value for key, value in candidate.items() if key != "content_hash"}
        candidate["content_hash"] = canonical_hash(body)
        selection_path = next(tmp_path.glob("entries/*/stage2_selection.json"))
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        selection["candidate_digests"][0]["content_hash"] = candidate[
            "content_hash"
        ]
        selection_body = {
            key: value for key, value in selection.items() if key != "content_hash"
        }
        selection["content_hash"] = canonical_hash(selection_body)
        selection_path.write_text(json.dumps(selection), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    corrupt_retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    corrupt_retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        corrupt_retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_stage2_one_sided_query_timeout_enters_only_the_durable_timeout_queue(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class Stage2TimeoutAdapter(_Adapter):
        def __init__(self) -> None:
            super().__init__()
            self.transformed_runtime_calls = 0

        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime" and "OR true" in query:
                self.transformed_runtime_calls += 1
                if self.transformed_runtime_calls > 5:
                    raise QueryTimeoutError("Stage-2 transformed side timeout")
            result = super().execute(query, parameters, query_kind, **kwargs)
            if query_kind == "runtime" and "OR true" in query:
                return ExecutionResult(
                    system=result.system,
                    query_kind=result.query_kind,
                    rows=result.rows,
                    elapsed_ms=31.0,
                    summary=result.summary,
                )
            return result

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: Stage2TimeoutAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    entry_dir = next(tmp_path.glob("entries/*"))
    queued = [
        json.loads(line)
        for line in (entry_dir / "timeout_queue.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    candidate = json.loads(next(entry_dir.glob("candidates/*/candidate.json")).read_text(encoding="utf-8"))
    assert result["terminal_state"] == "stage2_complete"
    assert result["stage2_promoted_pairs"] == 0
    assert len(queued) == 1
    assert queued[0]["entry_id"] == entry.entry_id
    assert queued[0]["pair_hash"] == candidate["pair_hash"]
    assert queued[0]["timeout_side"] == "b"
    assert queued[0]["stage"] == "stage2"
    assert len(queued[0]["evidence_digest"]) == 64
    assert candidate["stage2_decision"]["promote"] is False


def test_stage2_any_single_side_timeout_is_queued_even_below_eighty_percent() -> None:
    samples = tuple(
        PairedSample(
            "AB" if index % 2 == 0 else "BA",
            1.0,
            None if index == 0 else 2.0,
            b_timeout=index == 0,
        )
        for index in range(12)
    )
    evidence = RuntimeEvidence(
        verdict=RuntimeVerdict.INCONCLUSIVE,
        query_a_hash="a" * 64,
        query_b_hash="b" * 64,
        paired_samples=samples,
        median_ratio=None,
        median_delta_ms=None,
        ci_ratio_low=None,
        ci_ratio_high=None,
        direction_consistency=0.0,
        relative_mad=None,
        reason_codes=("insufficient_timeout_dominance",),
    )

    assert worker_module._runtime_timeout_side(evidence) == "b"


def test_stage2_both_side_timeout_is_never_a_performance_queue_record() -> None:
    samples = tuple(
        PairedSample(
            "AB" if index % 2 == 0 else "BA",
            None if index == 0 else 1.0,
            None if index == 1 else 2.0,
            a_timeout=index == 0,
            b_timeout=index == 1,
        )
        for index in range(12)
    )
    evidence = RuntimeEvidence(
        verdict=RuntimeVerdict.INCONCLUSIVE,
        query_a_hash="a" * 64,
        query_b_hash="b" * 64,
        paired_samples=samples,
        median_ratio=None,
        median_delta_ms=None,
        ci_ratio_low=None,
        ci_ratio_high=None,
        direction_consistency=0.0,
        relative_mad=None,
        reason_codes=("both_sides_timeout",),
    )

    assert worker_module._runtime_timeout_side(evidence) is None


def test_stage2_driver_timeout_is_infrastructure_and_never_a_performance_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class Stage2DriverTimeoutAdapter(_Adapter):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_calls = 0

        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime":
                self.runtime_calls += 1
                if self.runtime_calls > 10:
                    raise TimeoutError("driver deadline")
            result = super().execute(query, parameters, query_kind, **kwargs)
            if query_kind == "runtime" and "OR true" in query:
                return ExecutionResult(
                    system=result.system,
                    query_kind=result.query_kind,
                    rows=result.rows,
                    elapsed_ms=31.0,
                    summary=result.summary,
                )
            return result

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: Stage2DriverTimeoutAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.DRIVER_TIMEOUT.value
    assert not tuple(tmp_path.glob("entries/*/timeout_queue.jsonl"))
    findings = [
        json.loads(line)
        for line in (tmp_path / "reliability" / "findings.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert findings[-1]["kind"] == ErrorKind.DRIVER_TIMEOUT.value


def test_p2_stage2_selection_caps_validation_at_five_candidates(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    entry = replace(
        _rule_plan_entry(
            rule_id="constant_true_disjunction",
            family="bool",
            accepted_pair_quota=6,
            max_generated_seed_budget=6,
        ),
        entry_id="p2:neo4j:discovery:constant_true_disjunction:256:0",
        phase=CampaignPhase.P2,
    )
    monkeypatch.setattr(
        worker_module,
        "stage1_top_k",
        lambda records, *, noise_p99_ms, policy: tuple(
            record.pair_hash for record in records
        ),
    )
    worker = RuntimeFirstShardWorker(
        spec,
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=tuple(
                f"MATCH (n) WHERE n.id = {index} RETURN n"
                for index in range(1, 7)
            ),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    selection = json.loads(
        next(tmp_path.glob("entries/*/stage2_selection.json")).read_text(
            encoding="utf-8"
        )
    )
    assert result["stage2_promoted_pairs"] == 6
    assert result["validation_selected_pairs"] == 5
    assert selection["validation_limit"] == 5
    assert len(selection["selected_pair_hashes"]) == 5


def _control_candidate(
    ordinal: int,
    *,
    gate_hit: bool,
    score: float,
) -> Stage2Candidate:
    evidence = RuntimeEvidence(
        RuntimeVerdict.PASS,
        "a" * 64,
        "b" * 64,
        (),
        2.0,
        score,
        1.5,
        2.5,
        1.0,
        0.0,
        (),
    )
    return Stage2Candidate(
        f"{ordinal:064x}",
        Stage2Decision(gate_hit, False, ()),
        score,
        evidence,
    )


def test_control_selection_validates_gate_hits_or_one_top_score() -> None:
    candidates = (
        _control_candidate(1, gate_hit=True, score=2.0),
        _control_candidate(2, gate_hit=False, score=100.0),
        _control_candidate(3, gate_hit=True, score=3.0),
        _control_candidate(4, gate_hit=True, score=1.0),
    )

    assert select_control_validation_pairs(candidates) == (candidates[2], candidates[0])

    no_hits = tuple(
        _control_candidate(index, gate_hit=False, score=score)
        for index, score in ((1, 1.0), (2, 5.0), (3, 3.0))
    )
    assert select_control_validation_pairs(no_hits) == (no_hits[1],)


class _IndependentValidationRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_session(
        self,
        candidate,
        session_number,
        *,
        expected_slow_query,
        noise_floor_ms,
        timer_resolution_ms,
    ):
        self.calls.append((candidate, session_number))
        a_ms, b_ms = (
            (40.0, 10.0) if expected_slow_query == "a" else (10.0, 40.0)
        )
        samples = tuple(
            PairedSample("AB" if index % 2 == 0 else "BA", a_ms, b_ms)
            for index in range(20)
        )
        evidence = RuntimeEvidence(
            RuntimeVerdict.PASS,
            hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest(),
            hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest(),
            samples,
            4.0,
            30.0,
            4.0,
            4.0,
            1.0,
            0.0,
            (),
            slow_query=expected_slow_query,
            graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
            dbms_fingerprint=candidate.pair.dbms_fingerprint,
            measurement_source="ordinary_execute",
            measurement_protocol="ordinary-execute-20-pair-v1",
            slower_side=expected_slow_query,
            ci_delta_low_ms=30.0,
            ci_delta_high_ms=30.0,
        )
        control_samples = tuple(
            PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0)
            for index in range(20)
        )
        control = RuntimeEvidence(
            RuntimeVerdict.PASS,
            "c" * 64,
            "c" * 64,
            control_samples,
            1.0,
            0.0,
            1.0,
            1.0,
            0.5,
            0.0,
            (),
        )
        return EquivalenceEvidence(True, True, ()), ValidationSession(
            f"session-{session_number}",
            10_000 + session_number,
            candidate.pair.graph_snapshot_hash,
            candidate.pair.dbms_fingerprint,
            evidence,
            (control, control),
            lifecycle=SessionLifecycle(
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                f"validation-connection-{session_number}",
            ),
            schedule_seed=validation_schedule_seed(
                candidate.candidate_hash,
                session_number,
            ),
        )


class _RejectedIndependentValidationRunner(_IndependentValidationRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(
            candidate,
            session_number,
            **kwargs,
        )
        mismatched_side = "a" if candidate.discovery_slow_query == "b" else "b"
        return equivalence, replace(
            session,
            evidence=replace(
                session.evidence,
                slow_query=mismatched_side,
                slower_side=mismatched_side,
            ),
        )


class _ControlRuntimeGapAdapter(_Adapter):
    def execute(self, query, parameters, query_kind, **kwargs):
        result = super().execute(query, parameters, query_kind, **kwargs)
        if query_kind != "runtime":
            return result
        return ExecutionResult(
            system=result.system,
            query_kind=result.query_kind,
            rows=result.rows,
            elapsed_ms=31.0 if "<-" in query else 1.0,
            summary=result.summary,
        )


def test_worker_validates_discovery_promotions_through_injected_runner(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    runner = _IndependentValidationRunner()
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: runner,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["stage2_promoted_pairs"] == 1
    assert result["validation_selected_pairs"] == 1
    assert result["validation_passed_pairs"] == 1
    assert len(runner.calls) == 2
    evidence = json.loads(next(tmp_path.glob("entries/*/validation/*.json")).read_text(encoding="utf-8"))
    assert evidence["submission_path"] == "relative"
    assert evidence["pooled_ratio_ci_low"] >= 2.0
    assert "control_gate_hit" not in evidence
    assert len(evidence["stage2_candidate_digest"]) == 64
    candidate_artifact = json.loads(
        next(tmp_path.glob("entries/*/candidates/*/candidate.json")).read_text(
            encoding="utf-8"
        )
    )
    assert evidence["stage2_candidate_digest"] == candidate_artifact["content_hash"]
    sealed_report = evidence["report"]
    assert sealed_report["validation_protocol"] == "independent-two-session-v1"
    assert sealed_report["validation_policy"] == worker_module.to_jsonable(
        asdict(runner.calls[0][0].validation_policy)
    )
    assert sealed_report["equivalence"] == {
        "complete_result": True,
        "equivalent": True,
        "reason_codes": [],
    }
    assert len(sealed_report["sessions"]) == 2
    assert all(
        len(session["evidence"]["paired_samples"]) == 20
        and len(session["negative_controls"]) == 2
        and session["lifecycle"]["connection_id"]
        for session in sealed_report["sessions"]
    )

    class _MustNotRunValidation:
        def run_session(self, *_args, **_kwargs):
            raise AssertionError("resume must reuse the identity-bound validation artifact")

    resumed = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _MustNotRunValidation(),
    )
    resumed.bind_run_dir(tmp_path)

    resumed_result = resumed(entry)

    assert resumed_result["validation_passed_pairs"] == 1


def test_worker_continues_validated_discovery_candidate_through_terminal_pipeline(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    observed = []

    class _Pipeline:
        def process(self, *_args, **_kwargs):
            raise AssertionError("schema-v3 must not repeat original validation")

        def process_validated(self, candidate, validation, **context):
            observed.append((candidate, validation, context))
            return {
                "candidate_hash": candidate.candidate_hash,
                "validation": {
                    "candidate_hash": candidate.candidate_hash,
                    "stage": validation.stage.value,
                    "validation_ref": {"content_hash": "1" * 64},
                },
                "reduction": {
                    "candidate_hash": candidate.candidate_hash,
                    "promotion": "not_applicable",
                    "status": "reduced_valid",
                },
            }

    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _IndependentValidationRunner(),
        candidate_pipeline=_Pipeline(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "completed"
    assert result["pipeline_terminal_candidates"] == 1
    assert result["submit_ready_issues"] == 0
    assert len(observed) == 1
    assert observed[0][1].stage.value == "validation_passed"
    assert observed[0][2]["task"] == entry
    task8_ref = observed[0][2]["task8_validation_ref"]
    assert task8_ref["schema_version"] == 2
    assert task8_ref["report_hash"] == canonical_validation_report_hash(observed[0][1])
    task8_path = tmp_path / task8_ref["relative_path"]
    task8_payload = json.loads(task8_path.read_text(encoding="utf-8"))
    assert task8_ref["content_hash"] == task8_payload["content_hash"]
    assert task8_payload["candidate_hash"] == observed[0][0].candidate_hash
    assert next(tmp_path.glob("entries/*/candidate-outcomes/*.json")).is_file()


def test_worker_never_sends_control_validation_to_candidate_pipeline(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)

    class _Pipeline:
        def process_validated(self, *_args, **_kwargs):
            raise AssertionError("CONTROL validation is diagnostic and cannot enter reduction")

    entry = replace(
        _rule_plan_entry(rule_id="edge_notation_symmetry", family="sym"),
        entry_id="p1:neo4j:control:edge_notation_symmetry:256:0",
        stream=RuleStream.CONTROL,
        tier="control",
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=(
                "CREATE (a:Node {id: 1}), (b:Node {id: 2}), (a)-[:R]->(b)",
            ),
            query_texts=("MATCH (n)-[:R]->(m) RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _ControlRuntimeGapAdapter(),
        validation_runner_factory=lambda _entry: _IndependentValidationRunner(),
        candidate_pipeline=_Pipeline(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "completed"
    assert result["control_validation_pass"] is True
    assert result["pipeline_terminal_candidates"] == 0
    assert result["submit_ready_issues"] == 0
    assert not list(tmp_path.glob("entries/*/candidate-outcomes/*.json"))


@pytest.mark.parametrize(
    "tamper",
    (
        "content_hash",
        "candidate_hash",
        "stage2_evidence_digest",
        "stage2_candidate_digest",
        "stage2_candidate_typed_digest",
        "stream",
        "discovery_control_fields",
        "entry_identity",
        "report_equivalence",
        "report_process_identity",
        "report_negative_control_invalid",
        "report_negative_control_empty",
        "report_negative_control_schedule",
        "report_validation_policy",
        "report_validation_protocol",
        "report_metadata",
        "report_missing_equivalence",
        "report_non_finite",
        "report_runtime_enum",
        "report_process_type",
        "report_schedule_seed_same",
        "report_schedule_seed_none",
        "report_schedule_seed_swapped",
        "legacy_schema",
        "raw_samples_and_summary",
    ),
)
def test_worker_validation_resume_rejects_tampered_identity_bound_artifact(
    tmp_path,
    monkeypatch,
    tamper,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _IndependentValidationRunner(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["validation_passed_pairs"] == 1

    path = next(tmp_path.glob("entries/*/validation/*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    report = body["report"]
    if tamper == "content_hash":
        payload["content_hash"] = "0" * 64
    elif tamper == "entry_identity":
        body["entry_identity"]["entry_id"] = "foreign-entry"
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "discovery_control_fields":
        body["control_gate_hit"] = True
        body["control_validation_pass"] = True
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_equivalence":
        report["equivalence"]["equivalent"] = False
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_process_identity":
        for sessions in (body["sessions"], report["sessions"]):
            sessions[1]["process_id"] = sessions[0]["process_id"]
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper.startswith("report_negative_control_"):
        control_index = 0 if tamper.endswith("invalid") else 1
        for sessions in (body["sessions"], report["sessions"]):
            control = sessions[0]["negative_controls"][control_index]
            if tamper.endswith("invalid"):
                control["verdict"] = "invalid"
            elif tamper.endswith("empty"):
                control["paired_samples"] = []
            else:
                control["paired_samples"][10]["order"] = "BA"
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_validation_policy":
        report["validation_policy"]["noise_floor_ms"] = 999.0
        report["validation_policy"]["timer_resolution_ms"] = 999.0
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_validation_protocol":
        report["validation_protocol"] = "forged-validation-protocol"
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_metadata":
        report["metadata"]["absolute_also_passed"] = True
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_missing_equivalence":
        report.pop("equivalence")
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_non_finite":
        for sessions in (body["sessions"], report["sessions"]):
            sessions[0]["evidence"]["paired_samples"][0]["a_ms"] = float("nan")
        payload = {**body, "content_hash": "0" * 64}
    elif tamper == "report_runtime_enum":
        for sessions in (body["sessions"], report["sessions"]):
            sessions[0]["evidence"]["verdict"] = 1
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "report_process_type":
        for sessions in (body["sessions"], report["sessions"]):
            sessions[0]["process_id"] = "10001"
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper.startswith("report_schedule_seed_"):
        for sessions in (body["sessions"], report["sessions"]):
            if tamper.endswith("same"):
                sessions[0]["schedule_seed"] = 123
                sessions[1]["schedule_seed"] = 123
            elif tamper.endswith("none"):
                sessions[0]["schedule_seed"] = None
            else:
                sessions[0]["schedule_seed"], sessions[1]["schedule_seed"] = (
                    sessions[1]["schedule_seed"],
                    sessions[0]["schedule_seed"],
                )
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "legacy_schema":
        body["schema_version"] = 1
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "stage2_candidate_typed_digest":
        candidate_artifact = json.loads(
            next(tmp_path.glob("entries/*/candidates/*/candidate.json")).read_text(
                encoding="utf-8"
            )
        )
        typed_candidate = {
            "pair_hash": candidate_artifact["pair_hash"],
            "decision": {
                key: value
                for key, value in candidate_artifact["stage2_decision"].items()
                if key != "promote"
            },
            "noise_normalized_score": candidate_artifact["noise_normalized_score"],
            "evidence": candidate_artifact["stage2_evidence"],
        }
        body["stage2_candidate_digest"] = canonical_hash(typed_candidate)
        payload = {**body, "content_hash": canonical_hash(body)}
    elif tamper == "raw_samples_and_summary":
        for sessions in (body["sessions"], report["sessions"]):
            for session in sessions:
                for sample in session["evidence"]["paired_samples"]:
                    sample["b_ms"] = 400.0
                session["evidence"].update(
                    {
                        "median_ratio": 40.0,
                        "median_delta_ms": 390.0,
                        "ci_ratio_low": 40.0,
                        "ci_ratio_high": 40.0,
                        "ci_delta_low_ms": 390.0,
                        "ci_delta_high_ms": 390.0,
                    }
                )
        report.update(
            {
                "validated_ratio": 40.0,
                "pooled_delta_ms": 390.0,
                "pooled_ratio_ci_low": 40.0,
                "pooled_delta_ci_low_ms": 390.0,
                "magnitude_stability": "stable",
            }
        )
        report["metadata"]["absolute_also_passed"] = True
        body.update(
            {
                "validated_ratio": 40.0,
                "pooled_delta_ms": 390.0,
                "pooled_ratio_ci_low": 40.0,
                "pooled_delta_ci_low_ms": 390.0,
            }
        )
        payload = {**body, "content_hash": canonical_hash(body)}
    else:
        body[tamper] = "0" * 64 if tamper != "stream" else "control"
        payload = {**body, "content_hash": canonical_hash(body)}
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    class _MustNotRunValidation:
        def run_session(self, *_args, **_kwargs):
            raise AssertionError("tampered validation evidence must fail before replay")

    resumed = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _MustNotRunValidation(),
    )
    resumed.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        resumed(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR
    assert "validation" in str(captured.value)


def test_worker_validation_resume_rejects_rehashed_forged_pass_without_sessions(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _IndependentValidationRunner(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["validation_passed_pairs"] == 1

    path = next(tmp_path.glob("entries/*/validation/*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    body.pop("sessions")
    body.update(
        {
            "stage": "validation_passed",
            "submission_path": "relative",
            "validated_ratio": 999.0,
            "pooled_delta_ms": 999.0,
            "pooled_ratio_ci_low": 999.0,
            "pooled_delta_ci_low_ms": 999.0,
        }
    )
    path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )

    class _MustNotRunValidation:
        def run_session(self, *_args, **_kwargs):
            raise AssertionError("forged validation evidence must fail before replay")

    resumed = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _MustNotRunValidation(),
    )
    resumed.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        resumed(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_worker_control_validation_is_diagnostic_and_never_promotes_or_ranks(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    monkeypatch.setattr(
        "graph_sym_perf.validation.promotion.promote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CONTROL validation must never call promotion")
        ),
    )
    runner = _IndependentValidationRunner()
    entry = replace(
        _rule_plan_entry(rule_id="edge_notation_symmetry", family="sym"),
        entry_id="p1:neo4j:control:edge_notation_symmetry:256:0",
        stream=RuleStream.CONTROL,
        tier="control",
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=(
                "CREATE (a:Node {id: 1}), (b:Node {id: 2}), (a)-[:R]->(b)",
            ),
            query_texts=("MATCH (n)-[:R]->(m) RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _ControlRuntimeGapAdapter(),
        validation_runner_factory=lambda _entry: runner,
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["stage2_promoted_pairs"] == 0
    assert result["control_gate_hit"] is True
    assert result["control_validation_pass"] is True
    assert result.get("submit_ready_issues", 0) == 0
    assert "candidate_ranking" not in result
    assert len(runner.calls) == 2
    evidence = json.loads(next(tmp_path.glob("entries/*/validation/*.json")).read_text(encoding="utf-8"))
    assert evidence["control_gate_hit"] is True
    assert evidence["control_validation_pass"] is True
    assert "submission_candidate_rank" not in evidence


@pytest.mark.parametrize(
    "tamper",
    (
        "string",
        "gate_mismatch",
        "validation_mismatch",
        "missing",
    ),
)
def test_worker_control_validation_resume_rejects_diagnostic_field_tampering(
    tmp_path,
    monkeypatch,
    tamper,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = replace(
        _rule_plan_entry(rule_id="edge_notation_symmetry", family="sym"),
        entry_id="p1:neo4j:control:edge_notation_symmetry:256:0",
        stream=RuleStream.CONTROL,
        tier="control",
    )
    generated = GeneratedShard(
        graph_statements=(
            "CREATE (a:Node {id: 1}), (b:Node {id: 2}), (a)-[:R]->(b)",
        ),
        query_texts=("MATCH (n)-[:R]->(m) RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    validation_runner = (
        _RejectedIndependentValidationRunner()
        if tamper == "string"
        else _IndependentValidationRunner()
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _ControlRuntimeGapAdapter(),
        validation_runner_factory=lambda _entry: validation_runner,
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["control_validation_pass"] is (tamper != "string")

    path = next(tmp_path.glob("entries/*/validation/*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    if tamper == "string":
        body["control_validation_pass"] = "yes"
    elif tamper == "gate_mismatch":
        body["control_gate_hit"] = False
    elif tamper == "validation_mismatch":
        body["control_validation_pass"] = False
    else:
        body.pop("control_gate_hit")
    path.write_text(
        json.dumps(
            {**body, "content_hash": canonical_hash(body)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    class _MustNotRunValidation:
        def run_session(self, *_args, **_kwargs):
            raise AssertionError("tampered CONTROL evidence must fail before replay")

    resumed = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _ControlRuntimeGapAdapter(),
        validation_runner_factory=lambda _entry: _MustNotRunValidation(),
    )
    resumed.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        resumed(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_worker_validation_runner_failure_is_terminal_infrastructure(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class _FailingRunner:
        def run_session(self, *_args, **_kwargs):
            raise RuntimeError("VALIDATION_SESSION_FAILED: empty result queue")

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
        validation_runner_factory=lambda _entry: _FailingRunner(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.INFRASTRUCTURE_ERROR.value
    assert "independent validation process failed" in result["infrastructure_reason"]
    assert not tuple(tmp_path.glob("entries/*/validation/*.json"))


def test_corrupt_timeout_queue_fails_closed_on_resume(tmp_path, monkeypatch) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class Stage1TimeoutAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime" and "OR true" in query:
                raise QueryTimeoutError("one-sided timeout")
            return super().execute(query, parameters, query_kind, **kwargs)

    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: Stage1TimeoutAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"

    queue_path = next(tmp_path.glob("entries/*/timeout_queue.jsonl"))
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write('{"truncated"')
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value


def test_timeout_queue_resume_rejects_a_valid_but_foreign_record(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class Stage1TimeoutAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime" and "OR true" in query:
                raise QueryTimeoutError("one-sided timeout")
            return super().execute(query, parameters, query_kind, **kwargs)

    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: Stage1TimeoutAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    DurableTimeoutQueue(entry_dir / "timeout_queue.jsonl").append(
        TimeoutQueueRecord(
            entry_id=entry.entry_id,
            pair_hash="1" * 64,
            timeout_side="a",
            stage="stage1",
            evidence_digest="2" * 64,
            graph_snapshot_hash=_graph_snapshot_hash(generated.graph_statements),
            dbms_fingerprint="a" * 64,
            dbms_health_epoch="e" * 64,
            noise_floor_id="f" * 64,
        )
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.EVIDENCE_INTEGRITY_ERROR.value


def test_timeout_queue_is_not_committed_when_final_health_identity_drifts(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")

    class Stage1TimeoutThenDriftAdapter(_FinalFingerprintDriftAdapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "runtime" and "OR true" in query:
                raise QueryTimeoutError("one-sided timeout")
            return super().execute(query, parameters, query_kind, **kwargs)

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: Stage1TimeoutThenDriftAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert not tuple(tmp_path.glob("entries/*/timeout_queue.jsonl"))
    findings = [
        json.loads(line)
        for line in (tmp_path / "reliability" / "findings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert findings[-1]["scope"] == "entry"
    assert findings[-1]["query_hash"] is None
    assert findings[-1]["entry_id"] == entry.entry_id


def test_runtime_artifact_failure_never_advances_accepted_count(tmp_path, monkeypatch) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    monkeypatch.setattr(
        "graph_sym_perf.runtime.records.CompactRuntimeStore._write_line",
        lambda _self, _line: (_ for _ in ()).throw(OSError("disk full")),
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["accepted_pairs"] == 0
    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.INFRASTRUCTURE_ERROR.value


def test_raw_timeout_is_driver_infrastructure_not_query_timeout(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")

    class DriverTimeoutAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            if query_kind == "acceptance_base":
                raise TimeoutError("driver socket timeout")
            return super().execute(query, parameters, query_kind, **kwargs)

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: DriverTimeoutAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["infrastructure_error_kind"] == ErrorKind.DRIVER_TIMEOUT.value
    assert result["reject_reasons"] == {}


@pytest.mark.parametrize("sample_case", ("at_bound", "above_bound", "huge_integer"))
def test_live_acceptance_runtime_is_bounded_before_pending_progress(
    tmp_path,
    monkeypatch,
    sample_case,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    elapsed_ms = {
        "at_bound": 30_000.0,
        "above_bound": 30_000.001,
        "huge_integer": 10**1000,
    }[sample_case]

    class AcceptanceRuntimeAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            result = super().execute(query, parameters, query_kind, **kwargs)
            if not query_kind.startswith("acceptance_"):
                return result
            return ExecutionResult(
                system=result.system,
                query_kind=result.query_kind,
                rows=result.rows,
                elapsed_ms=elapsed_ms,
                summary=result.summary,
            )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: AcceptanceRuntimeAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    first = worker(entry)
    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "acceptance_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))

    if sample_case == "at_bound":
        assert first["terminal_state"] == "stage2_complete"
        assert progress["accepted_record_ledger"][0]["acceptance_a_us"] == 30_000_000
        assert progress["accepted_record_ledger"][0]["acceptance_b_us"] == 30_000_000
        return

    assert first["terminal_state"] == "infrastructure_failed"
    assert first["infrastructure_error_kind"] == ErrorKind.INFRASTRUCTURE_ERROR.value
    assert progress["pending_acceptances"] == []
    assert progress["accepted_pair_hashes"] == []
    assert progress["accepted_record_ledger"] == []
    assert progress["stage1_intent"] is None
    assert not (entry_dir / "pair_runtime.jsonl").exists()

    retry = worker(entry)
    retried_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert retry["terminal_state"] == "infrastructure_failed"
    assert retried_progress["pending_acceptances"] == []
    assert retried_progress["accepted_pair_hashes"] == []
    assert retried_progress["accepted_record_ledger"] == []
    assert retried_progress["stage1_intent"] is None
    assert not (entry_dir / "pair_runtime.jsonl").exists()


@pytest.mark.parametrize(
    "sample_case",
    ("at_bound", "above_bound", "huge_float", "huge_integer"),
)
def test_live_stage1_runtime_conversion_failure_is_controlled_infrastructure(
    tmp_path,
    monkeypatch,
    sample_case,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    elapsed_ms = {
        "at_bound": 30_000.0,
        "above_bound": 30_000.001,
        "huge_float": 1e307,
        "huge_integer": 10**1000,
    }[sample_case]

    class Stage1RuntimeAdapter(_Adapter):
        def execute(self, query, parameters, query_kind, **kwargs):
            result = super().execute(query, parameters, query_kind, **kwargs)
            if query_kind != "runtime":
                return result
            return ExecutionResult(
                system=result.system,
                query_kind=result.query_kind,
                rows=result.rows,
                elapsed_ms=elapsed_ms,
                summary=result.summary,
            )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: Stage1RuntimeAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    first = worker(entry)
    entry_dir = next(tmp_path.glob("entries/*"))
    progress_path = entry_dir / "acceptance_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))

    if sample_case == "at_bound":
        assert first["terminal_state"] == "stage2_complete"
        assert (entry_dir / "pair_runtime.jsonl").exists()
        return

    assert first["terminal_state"] == "infrastructure_failed"
    assert first["infrastructure_error_kind"] == ErrorKind.INFRASTRUCTURE_ERROR.value
    assert len(progress["pending_acceptances"]) == 1
    assert progress["accepted_pair_hashes"] == []
    assert progress["accepted_record_ledger"] == []
    assert progress["stage1_intent"] is None
    assert not (entry_dir / "pair_runtime.jsonl").exists()

    retry = worker(entry)
    retried_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert retry["terminal_state"] == "infrastructure_failed"
    assert len(retried_progress["pending_acceptances"]) == 1
    assert retried_progress["accepted_pair_hashes"] == []
    assert retried_progress["accepted_record_ledger"] == []
    assert retried_progress["stage1_intent"] is None
    assert not (entry_dir / "pair_runtime.jsonl").exists()


def test_noise_calibration_rejects_fewer_than_twenty_distinct_accepted_probes(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["accepted_pairs"] == 0
    assert not tuple(tmp_path.glob("entries/*/pair_runtime.jsonl"))


@pytest.mark.parametrize("state", ("pending_only", "intent_only"))
def test_schema3_nonempty_uncommitted_state_cannot_upgrade(tmp_path, state) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    body = {
        "schema_version": 3,
        "entry_id": entry.entry_id,
        "elapsed_seconds": 0.0,
        "last_query_ordinal": 1,
        "attempted_query_records": 1,
        "reject_counts": {},
        "accepted_pair_hashes": [],
        "pending_acceptances": [],
        "legacy_migration_sha256": None,
        "evidence_identity": {
            "dbms_fingerprint": "a" * 64,
            "dbms_health_epoch": "e" * 64,
            "graph_snapshot_hash": "g" * 64,
        },
        "stage1_intent": None,
    }
    if state == "pending_only":
        body["pending_acceptances"] = [
            {
                "acceptance_a_us": 1,
                "acceptance_b_us": 1,
                "pair_hash": "1" * 64,
                "source_query_ordinal": 1,
            }
        ]
    else:
        body["stage1_intent"] = {
            "pair_hash": "1" * 64,
            "record_digest": "2" * 64,
            "source_query_ordinal": 1,
        }
    path = tmp_path / "acceptance_progress.json"
    path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(WorkerFailure) as captured:
        worker_module._load_entry_progress(path, entry=entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_noise_artifact_freezes_exact_integer_samples_and_progress_anchor(tmp_path) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=queries,
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _NonzeroBalancedNoiseAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    noise = json.loads((entry_dir / "noise_calibration.json").read_text(encoding="utf-8"))
    assert len(noise["probe_pair_hashes"]) == 20
    assert [item["pair_hash"] for item in noise["probe_samples_us"]] == noise[
        "probe_pair_hashes"
    ]
    assert all(len(item["samples_us"]) == 10 for item in noise["probe_samples_us"])
    assert all(
        type(value) is int and value >= 0
        for item in noise["probe_samples_us"]
        for _order, a_us, b_us in item["samples_us"]
        for value in (a_us, b_us)
    )
    assert not {"queries", "query_texts", "probe_query_texts"}.intersection(noise)
    progress = json.loads((entry_dir / "acceptance_progress.json").read_text(encoding="utf-8"))
    assert progress["noise_floor_id"] == noise["content_hash"]
    assert progress["noise_intent"] is None


def test_complete_invalid_noise_is_anchored_as_auditable_evidence(tmp_path) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )

    class OrderBiasedNoiseAdapter(_Adapter):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_calls = 0

        def execute(self, query, parameters, query_kind, **kwargs):
            result = super().execute(query, parameters, query_kind, **kwargs)
            if query_kind != "runtime":
                return result
            self.runtime_calls += 1
            return ExecutionResult(
                system=result.system,
                query_kind=result.query_kind,
                rows=result.rows,
                elapsed_ms=10.0 if self.runtime_calls % 2 else 20.0,
                summary=result.summary,
            )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=queries,
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: OrderBiasedNoiseAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    entry_dir = next(tmp_path.glob("entries/*"))
    noise = json.loads((entry_dir / "noise_calibration.json").read_text(encoding="utf-8"))
    assert noise["calibration"]["valid"] is False
    assert noise["calibration"]["reason_codes"] == ["aa_order_bias"]
    assert len(noise["probe_samples_us"]) == 20
    progress = json.loads((entry_dir / "acceptance_progress.json").read_text(encoding="utf-8"))
    assert progress["noise_floor_id"] == noise["content_hash"]
    assert progress["noise_intent"] is None
    assert not (entry_dir / "pair_runtime.jsonl").exists()


def test_incomplete_noise_writes_separate_typed_failure_artifact(tmp_path) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=queries,
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _InterruptedNoiseAdapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    entry_dir = next(tmp_path.glob("entries/*"))
    assert not (entry_dir / "noise_calibration.json").exists()
    failure = json.loads(
        (entry_dir / "noise_calibration_failure.json").read_text(encoding="utf-8")
    )
    assert failure["reason_codes"] == ["aa_probe_inconclusive"]
    assert failure["protocol_version"] == "aa-noise-20x10-v1"
    assert failure["content_hash"] == canonical_hash(
        {key: value for key, value in failure.items() if key != "content_hash"}
    )
    assert not (entry_dir / "pair_runtime.jsonl").exists()
    progress = json.loads(
        (entry_dir / "acceptance_progress.json").read_text(encoding="utf-8")
    )
    assert progress["schema_version"] == 5
    assert progress["noise_failure_id"] == failure["content_hash"]
    assert progress["noise_failure_intent"] is None

    retry = worker(entry)
    assert retry["terminal_state"] == "infrastructure_failed"
    assert retry["infrastructure_error_kind"] == result["infrastructure_error_kind"]
    assert retry["infrastructure_reason"] == result["infrastructure_reason"]
    assert json.loads(
        (entry_dir / "noise_calibration_failure.json").read_text(encoding="utf-8")
    ) == failure


def test_retry_recovers_exact_noise_failure_artifact_intent_window(
    tmp_path,
    monkeypatch,
) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    real_persist = worker_module._persist_entry_progress

    class SimulatedNoiseFailureAnchorCrash(BaseException):
        pass

    crashed = False

    def crash_before_failure_anchor(path, **kwargs):
        nonlocal crashed
        if kwargs.get("noise_failure_id") is not None and not crashed:
            crashed = True
            raise SimulatedNoiseFailureAnchorCrash(
                "noise failure artifact fsynced before progress anchor"
            )
        return real_persist(path, **kwargs)

    monkeypatch.setattr(worker_module, "_persist_entry_progress", crash_before_failure_anchor)
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _InterruptedNoiseAdapter(),
    )
    first.bind_run_dir(tmp_path)

    with pytest.raises(SimulatedNoiseFailureAnchorCrash):
        first(entry)

    entry_dir = next(tmp_path.glob("entries/*"))
    failure = json.loads(
        (entry_dir / "noise_calibration_failure.json").read_text(encoding="utf-8")
    )
    crashed_progress = json.loads(
        (entry_dir / "acceptance_progress.json").read_text(encoding="utf-8")
    )
    assert crashed_progress["noise_failure_id"] is None
    assert crashed_progress["noise_failure_intent"] == {
        "noise_failure_id": failure["content_hash"]
    }

    monkeypatch.setattr(worker_module, "_persist_entry_progress", real_persist)
    retry_adapter = _Adapter()
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: retry_adapter,
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    recovered = json.loads(
        (entry_dir / "acceptance_progress.json").read_text(encoding="utf-8")
    )
    assert recovered["noise_failure_id"] == failure["content_hash"]
    assert recovered["noise_failure_intent"] is None
    assert not any(kind == "runtime" for _query, kind in retry_adapter.executed)


@pytest.mark.parametrize(
    "corruption",
    ("delete", "tamper", "intent_only", "anchor_only", "artifact_only"),
)
def test_retry_rejects_broken_noise_failure_transaction(tmp_path, corruption) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _InterruptedNoiseAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "infrastructure_failed"
    entry_dir = next(tmp_path.glob("entries/*"))
    failure_path = entry_dir / "noise_calibration_failure.json"
    progress_path = entry_dir / "acceptance_progress.json"
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["noise_failure_id"] == failure["content_hash"]
    body = {key: value for key, value in progress.items() if key != "content_hash"}

    if corruption in {"delete", "intent_only", "anchor_only"}:
        failure_path.unlink()
    elif corruption == "tamper":
        failure_body = {
            key: value for key, value in failure.items() if key != "content_hash"
        }
        failure_body["error_reason"] = "self-consistently rewritten"
        failure_path.write_text(
            json.dumps(
                {**failure_body, "content_hash": canonical_hash(failure_body)},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    if corruption == "intent_only":
        body["noise_failure_id"] = None
        body["noise_failure_intent"] = {
            "noise_failure_id": failure["content_hash"]
        }
    elif corruption == "artifact_only":
        body["noise_failure_id"] = None
        body["noise_failure_intent"] = None
    elif corruption == "anchor_only":
        body["noise_failure_intent"] = None
    progress_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_schema4_without_noise_failure_evidence_upgrades_safely(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    entry_dir = tmp_path / "entries" / worker_module._entry_directory_name(entry.entry_id)
    entry_dir.mkdir(parents=True)
    body = {
        "schema_version": 4,
        "entry_id": entry.entry_id,
        "elapsed_seconds": 0.0,
        "last_query_ordinal": 0,
        "attempted_query_records": 0,
        "reject_counts": {},
        "accepted_pair_hashes": [],
        "pending_acceptances": [],
        "legacy_migration_sha256": None,
        "evidence_identity": None,
        "stage1_intent": None,
        "accepted_record_ledger": [],
        "noise_floor_id": None,
        "noise_intent": None,
    }
    (entry_dir / "acceptance_progress.json").write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    assert worker(entry)["terminal_state"] == "stage2_complete"
    upgraded = json.loads(
        (entry_dir / "acceptance_progress.json").read_text(encoding="utf-8")
    )
    assert upgraded["schema_version"] == 5
    assert upgraded["noise_failure_id"] is None
    assert upgraded["noise_failure_intent"] is None


def test_schema4_cannot_claim_an_unbound_noise_failure_artifact(tmp_path) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _InterruptedNoiseAdapter(),
    )
    worker.bind_run_dir(tmp_path)
    assert worker(entry)["terminal_state"] == "infrastructure_failed"
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    body["schema_version"] = 4
    body.pop("noise_failure_id")
    body.pop("noise_failure_intent")
    progress_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(WorkerFailure) as captured:
        worker(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_retry_recovers_only_an_exact_noise_artifact_intent_window(
    tmp_path,
    monkeypatch,
) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    real_persist = worker_module._persist_entry_progress

    class SimulatedNoiseAnchorCrash(BaseException):
        pass

    crashed = False

    def crash_before_noise_anchor(path, **kwargs):
        nonlocal crashed
        if kwargs["noise_floor_id"] is not None and not kwargs["accepted_record_ledger"] and not crashed:
            crashed = True
            raise SimulatedNoiseAnchorCrash("noise artifact fsynced before progress anchor")
        return real_persist(path, **kwargs)

    monkeypatch.setattr(worker_module, "_persist_entry_progress", crash_before_noise_anchor)
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _NonzeroBalancedNoiseAdapter(),
    )
    first.bind_run_dir(tmp_path)

    with pytest.raises(SimulatedNoiseAnchorCrash):
        first(entry)

    entry_dir = next(tmp_path.glob("entries/*"))
    noise = json.loads((entry_dir / "noise_calibration.json").read_text(encoding="utf-8"))
    crashed_progress = json.loads(
        (entry_dir / "acceptance_progress.json").read_text(encoding="utf-8")
    )
    assert crashed_progress["noise_floor_id"] is None
    assert crashed_progress["noise_intent"] == {
        "noise_floor_id": noise["content_hash"],
        "probe_pair_hashes": noise["probe_pair_hashes"],
    }

    monkeypatch.setattr(worker_module, "_persist_entry_progress", real_persist)
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _NonzeroBalancedNoiseAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "stage2_complete"
    recovered = json.loads((entry_dir / "acceptance_progress.json").read_text(encoding="utf-8"))
    assert recovered["noise_floor_id"] == noise["content_hash"]
    assert recovered["noise_intent"] is None


@pytest.mark.parametrize(
    "tamper",
    ("forged_summary", "replacement_probes", "raw_samples", "swapped_order"),
)
def test_retry_recomputes_noise_artifact_from_expected_probes_and_integer_samples(
    tmp_path,
    tamper,
) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
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
    assert first(entry)["terminal_state"] == "stage2_complete"
    noise_path = next(tmp_path.glob("entries/*/noise_calibration.json"))
    payload = json.loads(noise_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    if tamper == "forged_summary":
        body["calibration"]["p95_delta_ms"] = 100.0
        body["calibration"]["p99_delta_ms"] = 0.0
    elif tamper == "replacement_probes":
        body["probe_pair_hashes"][0] = "0" * 64
        body["probe_samples_us"][0]["pair_hash"] = "0" * 64
    elif tamper == "raw_samples":
        body["probe_samples_us"][0]["samples_us"][0][1] += 100_000
        body["calibration"] = worker_module.to_jsonable(
            asdict(
                worker_module.summarize_aa_noise_samples(
                    [value["samples_us"] for value in body["probe_samples_us"]]
                )
            )
        )
    else:
        first_sample = body["probe_samples_us"][0]["samples_us"][0]
        first_sample[0] = "BA" if first_sample[0] == "AB" else "AB"
    noise_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize("sample_case", ("at_bound", "above_bound", "huge_integer"))
def test_noise_loader_bounds_rehashed_integer_microseconds(tmp_path, sample_case) -> None:
    queries = tuple(
        f"MATCH (n) WHERE n.id = {index} RETURN n"
        for index in range(1, 21)
    )
    statements = ("CREATE (n:Node {id: 1})",)
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=20,
        max_generated_seed_budget=20,
    )
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=statements,
            query_texts=queries,
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)
    worker(entry)
    noise_path = next(tmp_path.glob("entries/*/noise_calibration.json"))
    payload = json.loads(noise_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    upper = int(entry.query_timeout_seconds * 1_000_000)
    replacement = (
        upper
        if sample_case == "at_bound"
        else upper + 1
        if sample_case == "above_bound"
        else 10**1000
    )
    for probe in body["probe_samples_us"]:
        for sample in probe["samples_us"]:
            sample[1] = replacement
            sample[2] = replacement
    if sample_case != "huge_integer":
        body["calibration"] = worker_module.to_jsonable(
            asdict(
                worker_module.summarize_aa_noise_samples(
                    [value["samples_us"] for value in body["probe_samples_us"]]
                )
            )
        )
    noise_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    load = lambda: worker_module._load_entry_noise(
        noise_path,
        entry=entry,
        graph_snapshot_hash=_graph_snapshot_hash(statements),
        dbms_fingerprint="a" * 64,
        dbms_health_epoch="e" * 64,
        expected_probe_hashes=tuple(body["probe_pair_hashes"]),
    )

    if sample_case == "at_bound":
        assert load() is not None
    else:
        with pytest.raises(WorkerFailure) as captured:
            load()
        assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_invalid_stage1_measurement_never_becomes_an_accepted_record(tmp_path) -> None:
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")

    class InvalidStage1Adapter(_Adapter):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_calls = 0

        def execute(self, query, parameters, query_kind, **kwargs):
            result = super().execute(query, parameters, query_kind, **kwargs)
            if query_kind != "runtime":
                return result
            self.runtime_calls += 1
            if self.runtime_calls <= 400:  # fixed 20 probes x 10 A/A pairs x 2 executions
                return result
            return ExecutionResult(
                system=result.system,
                query_kind=result.query_kind,
                rows=result.rows,
                elapsed_ms=float("nan"),
                summary=result.summary,
            )

    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: InvalidStage1Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "infrastructure_failed"
    assert result["accepted_pairs"] == 0
    assert not tuple(tmp_path.glob("entries/*/pair_runtime.jsonl"))


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
    assert first_result["accepted_pairs"] == 0
    assert first_result["acceptance_generated_seeds"] == 1
    first_progress = json.loads(next(tmp_path.glob("entries/*/acceptance_progress.json")).read_text())
    assert len(first_progress["pending_acceptances"]) == 1

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
    assert result["accepted_pairs"] == 0
    assert result["acceptance_generated_seeds"] == 1
    assert result["reject_reasons"] == {}
    assert result["rule_stats"]["accepted"] == {}
    retry_progress = json.loads(next(tmp_path.glob("entries/*/acceptance_progress.json")).read_text())
    assert retry_progress["pending_acceptances"] == first_progress["pending_acceptances"]


@pytest.mark.parametrize("adapter_type", (_FinalFingerprintDriftAdapter, _FinalGraphDriftAdapter))
def test_final_identity_drift_invalidates_completed_rule_entry_acceptance(
    tmp_path,
    adapter_type,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    findings = [
        json.loads(line)
        for line in (tmp_path / "reliability" / "findings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    finding = findings[-1]
    assert finding["schema_version"] == 2
    assert finding["scope"] == "entry"
    assert finding["entry_id"] == entry.entry_id
    assert finding["query_hash"] is None
    assert finding["graph_hash"]
    assert finding["observed_identity"]
    assert finding["attempt_evidence"]
    assert finding["kind"] == (
        ErrorKind.DBMS_CRASHED.value
        if adapter_type is _FinalFingerprintDriftAdapter
        else ErrorKind.INFRASTRUCTURE_ERROR.value
    )
    assert not tuple(tmp_path.glob("entries/*/timeout_queue.jsonl"))

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    retry_result = retry(entry)

    assert retry_result["terminal_state"] == "infrastructure_failed"
    assert retry_result["acceptance_status"] == "incomplete"


def test_identity_drift_finding_failure_leaves_no_tombstone_and_retry_writes_both(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    real_record = worker_module.ReliabilityFindingStore.record_entry
    failed = False

    def fail_finding_once(self, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("finding append failed")
        return real_record(self, **kwargs)

    monkeypatch.setattr(
        worker_module.ReliabilityFindingStore,
        "record_entry",
        fail_finding_once,
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _FinalFingerprintDriftAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "infrastructure_failed"
    entry_dir = next(tmp_path.glob("entries/*"))
    assert not (entry_dir / "runtime_invalidation.json").exists()

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _FinalFingerprintDriftAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    assert retry(entry)["terminal_state"] == "infrastructure_failed"
    assert (entry_dir / "runtime_invalidation.json").exists()
    findings = (tmp_path / "reliability" / "findings.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"scope":"entry"' in findings


def test_identity_drift_tombstone_failure_keeps_finding_and_retry_writes_both(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    real_tombstone = worker_module._write_entry_runtime_invalidation
    failed = False

    def fail_tombstone_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("tombstone write failed")
        return real_tombstone(*args, **kwargs)

    monkeypatch.setattr(
        worker_module,
        "_write_entry_runtime_invalidation",
        fail_tombstone_once,
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _FinalFingerprintDriftAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "infrastructure_failed"
    entry_dir = next(tmp_path.glob("entries/*"))
    assert not (entry_dir / "runtime_invalidation.json").exists()
    finding_path = tmp_path / "reliability" / "findings.jsonl"
    assert '"scope":"entry"' in finding_path.read_text(encoding="utf-8")

    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _FinalFingerprintDriftAdapter(),
    )
    retry.bind_run_dir(tmp_path)
    assert retry(entry)["terminal_state"] == "infrastructure_failed"
    assert (entry_dir / "runtime_invalidation.json").exists()
    findings = [
        json.loads(line) for line in finding_path.read_text(encoding="utf-8").splitlines()
    ]
    assert findings[-1]["reproductions"] >= 2


def test_worker_rejects_typed_screening_policy_object_drift(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="constant_true_disjunction", family="bool")
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=("MATCH (n) WHERE n.id = 1 RETURN n",),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Stage2WideGapAdapter(),
    )
    first.bind_run_dir(tmp_path)
    assert first(entry)["terminal_state"] == "stage2_complete"
    policy = replace(
        worker_module.DEFAULT_SCREENING_POLICY,
        stage2_relative_ratio=1.6,
    )
    monkeypatch.setattr(
        worker_module,
        "DEFAULT_SCREENING_POLICY",
        policy,
        raising=False,
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("resume must use sealed corpus"),
        adapter_factory=lambda _entry: _NoRuntimeAdapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_rule_entry_retry_resumes_durable_accepted_pairs_without_double_counting(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
        assert result["terminal_state"] == "stage2_complete"

    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["rule_id"] == entry.rule_id
    assert records[0]["source_query_ordinal"] == 1
    assert "query_a" not in records[0] and "query_b" not in records[0]
    assert not tuple(tmp_path.glob("entries/*/accepted_pairs.jsonl"))


def test_task5_legacy_evidence_migrates_crash_safely_to_pair_runtime(tmp_path, monkeypatch) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(rule_id="null_partition_tautology", family="bool")
    query = "MATCH (n) WHERE n.id = 1 RETURN n"
    statements = ("CREATE (n:Node {id: 1})",)
    generated = GeneratedShard(
        graph_statements=statements,
        query_texts=(query,),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    built = worker_module.RulePairFactory(worker_module.RuleRegistry.default()).build(
        query,
        entry,
        entry.target,
    )
    assert built.pair is not None
    pair = built.pair
    entry_dir = tmp_path / "entries" / worker_module._entry_directory_name(entry.entry_id)
    entry_dir.mkdir(parents=True)
    legacy_record = {
        "certificate": worker_module.to_jsonable(asdict(pair.certificate)),
        "dbms_fingerprint": "a" * 64,
        "entry_id": entry.entry_id,
        "equivalence": "complete_typed_equivalent",
        "family": entry.family,
        "graph_snapshot_hash": _graph_snapshot_hash(statements),
        "pair_hash": pair.pair_hash,
        "parameters": worker_module.to_jsonable(pair.parameters),
        "proof_version": pair.certificate.proof_version,
        "query_a": pair.query_a,
        "query_b": pair.query_b,
        "query_hash": worker_module.portable_ast_hash(pair.query_a),
        "rule_id": entry.rule_id,
        "semantic_proof_sha256": pair.transform_metadata["semantic_proof_sha256"],
        "site": pair.certificate.site_class,
        "source_query_ordinal": 1,
        "stream": entry.stream.value,
        "tier": entry.tier,
        "transform_metadata": worker_module.to_jsonable(pair.transform_metadata),
    }
    (entry_dir / "accepted_pairs.jsonl").write_text(
        json.dumps(legacy_record, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    progress_body = {
        "schema_version": 1,
        "entry_id": entry.entry_id,
        "elapsed_seconds": 0.0,
        "last_query_ordinal": 1,
        "attempted_query_records": 1,
        "reject_counts": {},
        "accepted_pair_hashes": [pair.pair_hash],
    }
    (entry_dir / "acceptance_progress.json").write_text(
        json.dumps(
            {**progress_body, "content_hash": canonical_hash(progress_body)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    class SimulatedMigrationCrash(BaseException):
        pass

    real_unlink = worker_module._unlink_fsync
    monkeypatch.setattr(
        worker_module,
        "_unlink_fsync",
        lambda _path: (_ for _ in ()).throw(SimulatedMigrationCrash()),
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    with pytest.raises(SimulatedMigrationCrash):
        first(entry)

    migration_progress = json.loads((entry_dir / "acceptance_progress.json").read_text())
    assert migration_progress["schema_version"] == 5
    assert migration_progress["evidence_identity"] == {
        "dbms_fingerprint": "a" * 64,
        "dbms_health_epoch": "e" * 64,
        "graph_snapshot_hash": _graph_snapshot_hash(statements),
    }
    assert migration_progress["stage1_intent"] is None
    assert migration_progress["legacy_migration_sha256"]
    assert len(migration_progress["pending_acceptances"]) == 1
    assert (entry_dir / "accepted_pairs.jsonl").exists()

    monkeypatch.setattr(worker_module, "_unlink_fsync", real_unlink)
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    result = retry(entry)

    assert result["terminal_state"] == "stage2_complete"
    assert result["accepted_pairs"] == 1
    assert not (entry_dir / "accepted_pairs.jsonl").exists()
    compact = json.loads((entry_dir / "pair_runtime.jsonl").read_text())
    assert compact["pair_hash"] == pair.pair_hash
    assert "query_a" not in compact and "query_b" not in compact


def test_rule_entry_retry_keeps_reject_progress_and_does_not_repeat_completed_seed(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    _install_valid_noise_stub(monkeypatch)
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

    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
    record = json.loads(records_path.read_text(encoding="utf-8"))
    assert record["source_query_ordinal"] == 1
    crashed_progress = json.loads(
        next(tmp_path.glob("entries/*/acceptance_progress.json")).read_text(encoding="utf-8")
    )
    assert crashed_progress["accepted_pair_hashes"] == []
    assert crashed_progress["pending_acceptances"][0]["pair_hash"] == record["pair_hash"]

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
    assert progress["pending_acceptances"] == []


def test_progress_records_an_ordered_digest_ledger_for_every_runtime_append(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(entry)

    assert result["terminal_state"] == "stage2_complete"
    entry_dir = next(tmp_path.glob("entries/*"))
    records = [
        json.loads(line)
        for line in (entry_dir / "pair_runtime.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    progress = json.loads((entry_dir / "acceptance_progress.json").read_text(encoding="utf-8"))
    ledger = progress["accepted_record_ledger"]
    assert [item["pair_hash"] for item in ledger] == [record["pair_hash"] for record in records]
    assert [item["source_query_ordinal"] for item in ledger] == [1, 2]
    assert [item["record_digest"] for item in ledger] == [
        worker_module.canonical_record_digest(record) for record in records
    ]
    assert progress["accepted_pair_hashes"] == sorted(item["pair_hash"] for item in ledger)


def test_retry_rejects_reordered_runtime_jsonl_even_when_every_record_is_valid(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
    lines = records_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
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
    "corruption",
    ("missing_ledger", "duplicate_ledger", "digest_drift", "accepted_hash_mirror"),
)
def test_retry_rejects_runtime_ledger_corruption(
    tmp_path,
    monkeypatch,
    corruption,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    if corruption == "missing_ledger":
        body.pop("accepted_record_ledger", None)
    elif corruption == "duplicate_ledger":
        body["accepted_record_ledger"][1] = dict(body["accepted_record_ledger"][0])
    elif corruption == "digest_drift":
        body["accepted_record_ledger"][0]["record_digest"] = "0" * 64
    else:
        body["accepted_pair_hashes"] = body["accepted_pair_hashes"][:1]
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


def test_schema3_accepted_runtime_evidence_without_a_ledger_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    body["schema_version"] = 3
    for key in (
        "accepted_record_ledger",
        "noise_floor_id",
        "noise_intent",
    ):
        body.pop(key)
    progress_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


def test_retry_rejects_a_self_consistent_runtime_record_without_pending_intent(
    tmp_path,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    body["accepted_pair_hashes"] = []
    body["pending_acceptances"] = []
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


@pytest.mark.parametrize("corruption", ("progress_ahead", "non_contiguous", "multi_step"))
def test_retry_rejects_non_single_step_accepted_progress_divergence(
    tmp_path,
    corruption,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    quota = 2 if corruption == "multi_step" else 1
    queries = (
        "MATCH (n) WHERE n.id = 1 RETURN n",
        "MATCH (n) WHERE n.id = 2 RETURN n",
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
    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
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


@pytest.mark.parametrize("probe", ("source_query_ordinal_ahead", "rehashed_progress_behind"))
def test_retry_rejects_durable_ordinal_ahead_of_matching_progress_hash_set(
    tmp_path,
    probe,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=1,
        max_generated_seed_budget=2,
    )
    first_query = "MATCH (n) WHERE n.id = 1 RETURN n"
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(first_query, "MATCH (n) WHERE n.id = 2 RETURN n"),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    first(entry)
    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
    progress_path = next(tmp_path.glob("entries/*/acceptance_progress.json"))
    if probe == "source_query_ordinal_ahead":
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


def test_rule_entry_rejects_duplicate_ast_corpus_before_initial_seal(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=2,
        max_generated_seed_budget=2,
    )
    query = "MATCH (n) WHERE n.id = 1 RETURN n"
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: GeneratedShard(
            graph_statements=("CREATE (n:Node {id: 1})",),
            query_texts=(query, query),
            generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
        ),
        adapter_factory=lambda _entry: _Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        worker(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR
    assert not tuple(tmp_path.glob("entries/*/generation_corpus.json"))


def test_quota_two_retry_rejects_duplicate_sealed_corpus_ordinal_rebinding(tmp_path) -> None:
    entry = _rule_plan_entry(
        rule_id="null_partition_tautology",
        family="bool",
        accepted_pair_quota=2,
        max_generated_seed_budget=2,
    )
    accepted_query = "MATCH (n) WHERE n.id = 1 RETURN n"
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(accepted_query, "MATCH (n) RETURN n"),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": entry.graph_scale},
    )
    first = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda _entry, _raw_dir: generated,
        adapter_factory=lambda _entry: _Adapter(),
    )
    first.bind_run_dir(tmp_path)
    result = first(entry)
    assert result["accepted_pairs"] == 0
    assert result["acceptance_generated_seeds"] == 2
    progress = json.loads(next(tmp_path.glob("entries/*/acceptance_progress.json")).read_text())
    assert len(progress["pending_acceptances"]) == 1

    generation_path = next(tmp_path.glob("entries/*/generation_corpus.json"))
    sealed = json.loads(generation_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in sealed.items() if key != "content_hash"}
    body["query_texts"][1] = body["query_texts"][0]
    generation_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must load the sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    with pytest.raises(WorkerFailure) as captured:
        retry(entry)

    assert captured.value.kind is ErrorKind.EVIDENCE_INTEGRITY_ERROR


@pytest.mark.parametrize("corruption", ("truncated", "non_object"))
def test_retry_treats_malformed_accepted_jsonl_as_evidence_integrity_failure(
    tmp_path,
    corruption,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
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
        "pair_hash",
        "query_a_hash",
        "safety_certificate_hash",
        "family",
        "stream",
        "site_class",
        "graph_fingerprint",
        "samples_us",
        "negative_sample",
        "schedule_order",
        "median_ratio",
        "direction_consistency",
        "delta_over_noise",
        "noise_floor_id",
        "extra_field",
        "source_query_ordinal",
        "self_consistent_samples",
    ),
)
def test_rule_entry_retry_recomputes_every_compact_runtime_binding(
    tmp_path,
    tamper,
    monkeypatch,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    records_path = next(tmp_path.glob("entries/*/pair_runtime.jsonl"))
    record = json.loads(records_path.read_text(encoding="utf-8"))
    if tamper in {"pair_hash", "query_a_hash", "safety_certificate_hash", "graph_fingerprint"}:
        record[tamper] = "0" * 64
    elif tamper == "samples_us":
        record["samples_us"] = []
    elif tamper == "negative_sample":
        record["samples_us"][0][1] = -1
    elif tamper == "schedule_order":
        record["samples_us"][0][0] = "BA" if record["samples_us"][0][0] == "AB" else "AB"
    elif tamper == "median_ratio":
        record["median_ratio"] = 999.0
    elif tamper == "direction_consistency":
        record["direction_consistency"] = 2.0
    elif tamper == "delta_over_noise":
        record["delta_over_noise"] = float("nan")
    elif tamper == "noise_floor_id":
        record["noise_floor_id"] = "0" * 64
    elif tamper == "self_consistent_samples":
        record["samples_us"] = [
            [order, a_us + 100_000, b_us + 200_000]
            for order, a_us, b_us in record["samples_us"]
        ]
        record.update(
            worker_module.summarize_integer_samples(
                [tuple(sample) for sample in record["samples_us"]],
                noise_p99_ms=0.2,
            )
        )
    elif tamper == "extra_field":
        record["forged"] = "metadata"
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


@pytest.mark.parametrize("sample_case", ("at_bound", "above_bound", "huge_integer"))
def test_runtime_integer_microseconds_obey_entry_timeout_bound(
    tmp_path,
    monkeypatch,
    sample_case,
) -> None:
    _install_valid_noise_stub(monkeypatch)
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
    entry_dir = next(tmp_path.glob("entries/*"))
    records_path = entry_dir / "pair_runtime.jsonl"
    record = json.loads(records_path.read_text(encoding="utf-8"))
    upper = int(entry.query_timeout_seconds * 1_000_000)
    if sample_case == "at_bound":
        replacement = upper
        for sample in record["samples_us"]:
            sample[1] = replacement
            sample[2] = replacement
        record.update(
            worker_module.summarize_integer_samples(
                [tuple(sample) for sample in record["samples_us"]],
                noise_p99_ms=0.2,
            )
        )
    elif sample_case == "above_bound":
        replacement = upper + 1
        for sample in record["samples_us"]:
            sample[1] = replacement
        record.update(
            worker_module.summarize_integer_samples(
                [tuple(sample) for sample in record["samples_us"]],
                noise_p99_ms=0.2,
            )
        )
    else:
        replacement = 10**1000
        for sample in record["samples_us"][:2]:
            sample[1] = replacement
    records_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    progress_path = entry_dir / "acceptance_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in progress.items() if key != "content_hash"}
    body["accepted_record_ledger"][0]["record_digest"] = (
        worker_module.canonical_record_digest(record)
    )
    progress_path.write_text(
        json.dumps({**body, "content_hash": canonical_hash(body)}, sort_keys=True),
        encoding="utf-8",
    )
    retry = RuntimeFirstShardWorker(
        load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
        generation_runner=lambda *_args: pytest.fail("retry must use sealed corpus"),
        adapter_factory=lambda _entry: _Adapter(),
    )
    retry.bind_run_dir(tmp_path)

    if sample_case == "at_bound":
        assert retry(entry)["terminal_state"] == "stage2_complete"
    else:
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
    assert records[0]["safety_certificate"]["proof_id"] == "kleene_or_true_v1"
    assert records[0]["safety_certificate"]["proof_version"] == "1"
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


def test_formal_worker_runs_every_candidate_through_terminal_validation_and_reduction(
    monkeypatch,
    tmp_path,
) -> None:
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
    # This focused worker-contract test uses a synthetic pipeline that does not
    # materialize validation CAS artifacts. Provenance replay is exercised by
    # the artifact-chain integration tests.
    monkeypatch.setattr(
        "graph_sym_perf.experiment.worker.verify_candidate_outcome_validation_chain",
        lambda *_args, **_kwargs: None,
    )

    resumed = resumed_worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60))

    assert resumed["candidate_hashes"] == result["candidate_hashes"]
    assert (tmp_path / "shards" / "neo4j-000" / "candidate-outcomes").is_dir()


def test_terminal_candidate_resume_verifies_complete_validation_provenance(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts", budget_bytes=10 * 1024 * 1024)
    candidate = ArtifactRecord(level="candidate", payload=_candidate_payload())
    candidate_ref = store.put_json("candidate", candidate.to_dict())
    validation_body = {
        **_validation_payload(),
        "candidate_hash": candidate.payload["candidate_hash"],
        "candidate_ref": asdict(candidate_ref),
    }
    original = ArtifactRecord.seal_validation(validation_body)
    original_ref = store.put_json("original-validation", original.to_dict())
    reduced = ArtifactRecord.seal_validation(
        {
            **validation_body,
            "candidate_hash": "d" * 64,
        }
    )
    reduced_ref = store.put_json("reduced-validation", reduced.to_dict())
    summary = {
        "candidate_hash": candidate.payload["candidate_hash"],
        "reduced_candidate_hash": "d" * 64,
        "status": "reduced_valid",
        "original_validation_hash": original.payload["report_hash"],
        "reduced_validation_hash": reduced.payload["report_hash"],
        "original_validation_ref": asdict(original_ref),
        "reduced_validation_ref": asdict(reduced_ref),
    }
    reduction_ref = store.put_json("reduction", summary)
    outcome = {
        "candidate_hash": candidate.payload["candidate_hash"],
        "candidate_ref": asdict(candidate_ref),
        "validation": {
            "stage": "validation_passed",
            "validation_ref": asdict(original_ref),
        },
        "reduction": {
            **summary,
            "reduction_ref": asdict(reduction_ref),
        },
    }
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(outcome), encoding="utf-8")

    resumed = _load_terminal_candidate_outcome(
        path,
        store=store,
        candidate_hash=str(candidate.payload["candidate_hash"]),
        candidate_ref=candidate_ref,
    )
    assert resumed["reduction"]["reduced_validation_hash"] == reduced.payload["report_hash"]

    missing_ref = {
        "content_hash": "f" * 64,
        "relative_path": "objects/ff/" + "f" * 64,
        "size_bytes": 1,
        "media_type": "application/json",
        "schema_version": 1,
    }
    missing_summary = {
        **summary,
        "reduced_validation_ref": missing_ref,
    }
    missing_reduction_ref = store.put_json("missing-reduction", missing_summary)
    path.write_text(
        json.dumps(
            {
                **outcome,
                "reduction": {
                    **missing_summary,
                    "reduction_ref": asdict(missing_reduction_ref),
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkerFailure, match="cannot resume terminal candidate outcome"):
        _load_terminal_candidate_outcome(
            path,
            store=store,
            candidate_hash=str(candidate.payload["candidate_hash"]),
            candidate_ref=candidate_ref,
        )

    missing_report_payload = dict(reduced.payload)
    missing_report_payload.pop("report_hash")
    missing_report_payload.pop("validation_hash")
    missing_report_payload["validation_hash"] = hashlib.sha256(
        stable_json_dumps(missing_report_payload).encode("utf-8")
    ).hexdigest()
    missing_report_ref = store.put_json(
        "missing-report-validation",
        {
            "schema_version": 2,
            "level": "validation",
            "measurement_protocol": "independent-two-session-v1",
            **missing_report_payload,
        },
    )
    missing_report_summary = {
        **summary,
        "reduced_validation_ref": asdict(missing_report_ref),
    }
    missing_report_reduction_ref = store.put_json(
        "missing-report-reduction",
        missing_report_summary,
    )
    path.write_text(
        json.dumps(
            {
                **outcome,
                "reduction": {
                    **missing_report_summary,
                    "reduction_ref": asdict(missing_report_reduction_ref),
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkerFailure, match="cannot resume terminal candidate outcome"):
        _load_terminal_candidate_outcome(
            path,
            store=store,
            candidate_hash=str(candidate.payload["candidate_hash"]),
            candidate_ref=candidate_ref,
        )


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
