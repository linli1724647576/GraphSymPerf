import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.artifacts.store import ContentAddressedStore, verify_candidate_record_reference
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.evidence_validation import candidate_artifact_hashes
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


def test_worker_graph_identity_matches_validation_snapshot_contract() -> None:
    statements = (" CREATE (a) ", "", "CREATE (b)")

    assert _graph_snapshot_hash(statements) == GraphSnapshot.from_statements(statements).content_hash
    assert GraphCorpus((" CREATE (a) ", "CREATE (b)")).content_hash == _graph_snapshot_hash(statements)


def test_age_generation_provenance_never_claims_native_provider_coverage() -> None:
    assert _generation_backend_provenance(TargetSystem.APACHE_AGE, "agensgraph") == "agensgraph_proxy"
    with pytest.raises(ValueError, match="AgensGraph proxy"):
        _generation_backend_provenance(TargetSystem.APACHE_AGE, "neo4j")


def test_arcadedb_gdsmith_uses_the_postgres_wire_endpoint() -> None:
    connection = _gdsmith_connection(
        TargetSystem.ARCADEDB,
        {
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": "http://http.example:2480",
            "GRAPHSYMPERF_ARCADEDB_POSTGRES_URI": "postgresql://pg.example:5433/mydb",
            "GRAPHSYMPERF_ARCADEDB_USER": "root",
            "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret",
        },
    )

    assert connection == ("arcadedb", "pg.example", 5433, "root", "secret")


def test_arcadedb_gdsmith_rejects_a_database_the_upstream_provider_cannot_select() -> None:
    with pytest.raises(ValueError, match="must select /mydb"):
        _gdsmith_connection(
            TargetSystem.ARCADEDB,
            {
                "GRAPHSYMPERF_ARCADEDB_POSTGRES_URI": "postgresql://pg.example:5433/other",
            },
        )


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
                "GRAPHSYMPERF_ARCADEDB_POSTGRES_URI": "postgresql://127.0.0.1:5433/mydb",
                "GRAPHSYMPERF_ARCADEDB_USER": "root",
                "GRAPHSYMPERF_ARCADEDB_PASSWORD": "secret",
            },
            "arcadedb",
            "arcadedb",
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
    ids=["agensgraph", "apache-age-proxy", "arcadedb", "ladybugdb-proxy"],
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
    assert result["acceptance_generated_seeds"] == 2
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
    assert result["reject_reasons"] == {"TYPED_EQUIVALENCE_FAILED": 2}
    assert len(quarantine) == 2


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
