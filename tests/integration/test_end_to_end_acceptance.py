import hashlib
import json
import time

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.plan_models import RunPlan, ShardTask, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


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
    assert summary.failed == (hung,)
    assert summary.incomplete == ()
    assert "WORKER_HUNG" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
