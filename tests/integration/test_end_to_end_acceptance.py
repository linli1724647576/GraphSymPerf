import hashlib
import json
import time

import graph_sym_perf.experiment.worker as worker_module

from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    RulePlanEntry,
    RuleStream,
    RunPlan,
    ShardTask,
    SourceRevision,
)
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.supervisor import ShardSupervisor
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.generation.corpus import GraphCorpus
from graph_sym_perf.runtime.models import NoiseCalibration


class _AcceptanceWorker:
    def __init__(self, counts: dict[TargetSystem, int]) -> None:
        self.context = None
        self.counts = counts

    def bind_attempt_context(self, context) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        count = self.counts[task.target]
        hashes = [hashlib.sha256(f"{task}:{index}".encode()).hexdigest() for index in range(count)]
        (self.context.shard_dir / "accepted_pairs.jsonl").write_text(
            "".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes), encoding="utf-8"
        )
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(stage, encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {
            "accepted_pairs": count,
            "accepted_pair_hashes": hashes,
            "stage_evidence": stages,
        }


class _OneHungShardWorker(_AcceptanceWorker):
    def __init__(self, hung_target: TargetSystem) -> None:
        super().__init__({target: 1 for target in TargetSystem})
        self.hung_target = hung_target

    def __call__(self, task):
        if task.target is self.hung_target:
            time.sleep(10)
        return super().__call__(task)


def _task(target: TargetSystem, *, accepted_pairs: int) -> ShardTask:
    return ShardTask(
        target,
        0,
        20260710 + list(TargetSystem).index(target),
        accepted_pairs,
        accepted_pairs,
        1,
        target.value,
    )


def test_fake_seven_system_matrix_counts_only_exactly_complete_shards(tmp_path) -> None:
    tasks = tuple(_task(target, accepted_pairs=1000) for target in TargetSystem)
    short_target = TargetSystem.APACHE_AGE
    counts = {target: 1000 for target in TargetSystem}
    counts[short_target] = 999
    plan = RunPlan(
        "seven-system-acceptance",
        2,
        tasks,
        7_000,
        SourceRevision(None, None, None),
    )

    summary = ShardSupervisor(tmp_path, retry_limit=0).run(plan, _AcceptanceWorker(counts))

    assert len(summary.completed) == 6
    assert tuple(task.target for task in summary.incomplete) == (short_target,)
    assert summary.failed == ()
    for task in tasks:
        state = json.loads(
            (
                tmp_path
                / "shards"
                / f"{task.target.value}-{task.shard_index:03d}"
                / "checkpoint.json"
            ).read_text(encoding="utf-8")
        )["state"]
        assert state == ("incomplete" if task.target is short_target else "completed")


def test_one_hung_shard_does_not_prevent_another_system_from_completing(tmp_path) -> None:
    hung = _task(TargetSystem.NEO4J, accepted_pairs=1)
    healthy = _task(TargetSystem.MEMGRAPH, accepted_pairs=1)
    plan = RunPlan(
        "hung-shard-isolation",
        2,
        (hung, healthy),
        2,
        SourceRevision(None, None, None),
    )
    started = time.monotonic()

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.1,
        heartbeat_seconds=0.01,
        stale_after_seconds=0.5,
        max_parallel_generation_systems=2,
    ).run(plan, _OneHungShardWorker(TargetSystem.NEO4J))

    assert time.monotonic() - started < 5.0
    assert summary.completed == (healthy,)
    assert summary.incomplete == (hung,)
    assert summary.failed == ()
    assert "WORKER_HUNG" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


class _EntryAdapter:
    target = TargetSystem.NEO4J

    def reset_graph(self, *, timeout_seconds=None) -> bool:
        return True

    def reset_schema(self) -> None:
        return None

    def ensure_schema(self) -> None:
        return None

    def load_graph(self, _corpus: GraphCorpus) -> None:
        return None

    def execute(self, _query, _parameters, query_kind, **_kwargs) -> ExecutionResult:
        manifest = {
            "graph_manifest_nodes": ("nodes", 1),
            "graph_manifest_relationships": ("relationships", 0),
            "graph_manifest_labels": ("labels", 1),
            "graph_manifest_relationship_types": ("types", 0),
            "graph_manifest_indexes": ("indexes", 0),
        }
        key, value = manifest.get(query_kind, ("value", 1))
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=({key: value},),
            elapsed_ms=1.0,
            summary={"truncated": False},
        )

    def dbms_fingerprint(self) -> DBMSFingerprint:
        return DBMSFingerprint("a" * 64, server_version="neo4j/test")

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        return DBMSHealthEpoch("e" * 64, "test")

    def close(self) -> None:
        return None


def _entry(rule_id: str, family: str) -> RulePlanEntry:
    return RulePlanEntry(
        entry_id=f"p1:neo4j:discovery:{rule_id}:128:0",
        phase=CampaignPhase.P1,
        target=TargetSystem.NEO4J,
        rule_id=rule_id,
        family=family,
        tier="core",
        graph_scale=128,
        shard_index=0,
        shard_seed=7,
        accepted_pair_quota=1,
        max_generated_seed_budget=1,
        max_variants_per_seed=1,
        proof_version="1",
        query_timeout_seconds=30,
        max_entry_runtime_seconds=600,
        measurement_protocol_id="paired-runtime-v1",
        stream=RuleStream.DISCOVERY,
    )


def test_bool_and_sym_rule_entries_keep_independent_quota_and_statistics(
    tmp_path,
    monkeypatch,
) -> None:
    noise = NoiseCalibration(True, 200, 0.1, 0.2, 0.0, 1.0, ())
    noise_result = (noise, "f" * 64)
    probe_hashes = tuple(f"{index:064x}" for index in range(1, 21))

    def fixed_noise(*_args, **kwargs):
        if kwargs["anchored_noise_floor_id"] is None:
            kwargs["persist_intent"]("f" * 64, probe_hashes)
        return noise_result

    monkeypatch.setattr(
        worker_module,
        "_load_or_calibrate_entry_noise",
        fixed_noise,
    )
    query = "MATCH (a:Node), (b:Node) WHERE a.id = 1 RETURN a.id, b.id"
    generated = GeneratedShard(
        graph_statements=("CREATE (n:Node {id: 1})",),
        query_texts=(query,),
        generator_metadata={"backend": "gdsmith", "requested_max_nodes": 128},
    )
    results = []
    for entry in (
        _entry("null_partition_tautology", "bool"),
        _entry("pattern_order_symmetry", "sym"),
    ):
        worker = RuntimeFirstShardWorker(
            load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"),
            generation_runner=lambda _entry, _raw_dir: generated,
            adapter_factory=lambda _entry: _EntryAdapter(),
        )
        worker.bind_run_dir(tmp_path)
        results.append(worker(entry))

    assert [result["accepted_pairs"] for result in results] == [1, 1]
    assert [result["terminal_state"] for result in results] == [
        "stage2_complete",
        "stage2_complete",
    ]
    assert [result["rule_stats"]["accepted"] for result in results] == [
        {"null_partition_tautology": 1},
        {"pattern_order_symmetry": 1},
    ]
    assert len(tuple((tmp_path / "entries").glob("*/pair_runtime.jsonl"))) == 2
    assert not tuple((tmp_path / "entries").glob("*/accepted_pairs.jsonl"))
