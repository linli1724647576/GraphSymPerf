import json

from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker


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
    assert result["generated_query_records"] == 2
    assert adapter.loaded == shard.graph_statements
    assert adapter.closed
    assert all(kind != "profile" for _, kind in adapter.executed)
    records = [json.loads(line) for line in (tmp_path / "shards" / "neo4j-000" / "accepted_pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["equivalence"] == "complete_typed_equivalent" for record in records)


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
