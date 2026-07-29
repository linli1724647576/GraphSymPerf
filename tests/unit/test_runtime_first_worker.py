import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.generation.symmetric_state import SymmetryQueryPair


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


def test_worker_consumes_state_certified_pairs_without_boolean_transformation(tmp_path) -> None:
    adapter = _Adapter()
    pair = SymmetryQueryPair(
        query_a="MATCH (a:L0 {id: 1})-[r:R0]-(b:L1) RETURN count(*) AS gsp_count",
        query_b="MATCH (b:L1)-[r:R0]-(a:L0 {id: 3}) RETURN count(*) AS gsp_count",
        rule_id="undirected_endpoint_orbit_symmetry",
        certificate={
            "state_relation": "involutive_isomorphism_modulo_replay_id",
            "syntactic_symmetry": "undirected_endpoint_reversal",
        },
    )
    shard = GeneratedShard(
        graph_statements=("CREATE (n:L0 {id: 1})",),
        query_pairs=(pair,),
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

    assert result["accepted_pairs"] == 1
    record = json.loads(
        (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert record["rule_id"] == pair.rule_id
    assert record["symmetry_certificate"]["state_relation"] == "involutive_isomorphism_modulo_replay_id"
    assert all(" OR NOT " not in query.upper() for query, _kind in adapter.executed)


def _symmetry_pair(index: int = 0) -> SymmetryQueryPair:
    return SymmetryQueryPair(
        query_a=(
            f"MATCH (a:L0 {{id: {index + 1}}})-[r:R0]-(b:L1) "
            "RETURN count(*) AS gsp_count"
        ),
        query_b=(
            f"MATCH (b:L1)-[r:R0]-(a:L0 {{id: {index + 101}}}) "
            "RETURN count(*) AS gsp_count"
        ),
        rule_id="undirected_endpoint_orbit_symmetry",
        certificate={
            "state_relation": "involutive_isomorphism_modulo_replay_id",
            "syntactic_symmetry": "undirected_endpoint_reversal",
            "anchor_orbit": (index + 1, index + 101),
        },
    )


def test_worker_accepts_only_complete_equivalent_gdsmith_pairs_and_writes_minimal_records(tmp_path) -> None:
    adapter = _Adapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_pairs=(_symmetry_pair(0), _symmetry_pair(1)),
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
    assert result["generated_query_records"] == 2
    assert adapter.loaded == shard.graph_statements
    assert adapter.closed
    assert all(kind != "profile" for _, kind in adapter.executed)
    records = [json.loads(line) for line in (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["equivalence"] == "complete_typed_equivalent" for record in records)


def test_worker_preserves_failed_attempt_and_allocates_a_fresh_retry_directory(tmp_path) -> None:
    adapter = _Adapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_pairs=(_symmetry_pair(),),
        generator_metadata={"backend": "gdsmith"},
    )
    calls = 0

    def generation_runner(_task, _raw_dir):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic generation failure")
        return shard

    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=generation_runner,
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        worker(task)
    result = worker(task)

    assert result["accepted_pairs"] == 1
    assert (tmp_path / "shards" / "neo4j-000").is_dir()
    assert (tmp_path / "shards" / "neo4j-000-retry-001" / "summary.json").is_file()


def test_worker_fails_instead_of_reporting_an_underfilled_accepted_pair_budget(tmp_path) -> None:
    adapter = _Adapter()
    shard = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1, value: 1})",),
        query_pairs=(_symmetry_pair(),),
        generator_metadata={"backend": "gdsmith"},
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 2, 2, 60)
    worker = RuntimeFirstShardWorker(
        load_experiment_spec("config/experiment.yaml"),
        generation_runner=lambda _task, _raw_dir: shard,
        adapter_factory=lambda _task: adapter,
    )
    worker.bind_run_dir(tmp_path)

    with pytest.raises(RuntimeError, match="accepted only 1 of 2 required symmetry pairs"):
        worker(task)

    assert adapter.closed


def test_worker_rejects_an_unsafe_pair_even_if_it_carries_a_certificate(tmp_path) -> None:
    unsafe = SymmetryQueryPair(
        query_a="MATCH (n) SET n.value = 1 RETURN n",
        query_b="MATCH (n) SET n.value = 1 RETURN n",
        rule_id="undirected_endpoint_orbit_symmetry",
        certificate={"state_relation": "involutive_isomorphism_modulo_replay_id"},
    )
    worker = RuntimeFirstShardWorker(load_experiment_spec("config/experiment.yaml"))
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)

    accepted, rejects = worker._build_accepted_pairs(object(), task, (unsafe,), tmp_path)

    assert accepted == []
    assert rejects == {"PURITY_REJECT": 1}


def test_worker_passes_the_manifest_gdsmith_algorithm_to_the_runner(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run_gdsmith(**kwargs):
        captured["algorithm"] = kwargs["config"].algorithm
        captured["extra_args"] = kwargs["config"].backend_extra_args[kwargs["backend"]]
        captured["num_queries"] = kwargs["num_queries"]
        return SimpleNamespace(
            returncode=0,
            parsed=SimpleNamespace(
                graph_statements=(
                    "CREATE (n0:L0 {id: 1})",
                    "CREATE (n1:L1 {id: 2})",
                    "MATCH (n0 {id: 1}), (n1 {id: 2}) "
                    "MERGE (n0)-[r:R0 {id: 11}]->(n1)",
                ),
                accepted_queries=(),
            ),
        )

    monkeypatch.setattr("graph_sym_perf.experiment.worker.run_gdsmith", fake_run_gdsmith)
    spec = load_experiment_spec("config/experiment.yaml")
    spec = replace(spec, generation=replace(spec.generation, algorithm="NON_EMPTY"))
    worker = RuntimeFirstShardWorker(
        spec,
        environment={
            "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687",
            "GRAPHSYMPERF_NEO4J_USER": "neo4j",
            "GRAPHSYMPERF_NEO4J_PASSWORD": "secret",
        },
    )
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)

    worker._generate_with_gdsmith(task, tmp_path)

    assert captured["algorithm"] == "NON_EMPTY"
    assert captured["extra_args"] == ("--max-node-num", "1024", "--label-num", "4")
    assert captured["num_queries"] == 1


def test_discovery_screen_skips_unused_bootstrap_work(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeHarness:
        def measure_pair(self, _adapter, _base, _transformed, *, policy):
            captured["bootstrap_resamples"] = policy.bootstrap_resamples
            return SimpleNamespace(verdict="fail", median_ratio=None)

    monkeypatch.setattr("graph_sym_perf.experiment.worker.RuntimeHarness", FakeHarness)
    spec = load_experiment_spec("config/experiment.yaml")
    worker = RuntimeFirstShardWorker(spec)
    task = ShardTask(TargetSystem.NEO4J, 0, 7, 1, 1, 60)

    candidates = worker._screen_pairs(object(), task, [_symmetry_pair()], tmp_path)

    assert candidates == 0
    assert captured["bootstrap_resamples"] == 1
