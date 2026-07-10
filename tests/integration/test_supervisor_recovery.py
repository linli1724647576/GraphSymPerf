from graph_sym_perf.experiment.plan_models import RunPlan, ShardTask, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


def test_retryable_shard_failure_does_not_stop_other_tasks(tmp_path) -> None:
    tasks = (
        ShardTask.__new__(ShardTask),
    )
    # Use small stand-in tasks: supervisor only needs a stable task identity.
    plan = RunPlan("test", 2, ("bad", "good"), 2, SourceRevision(None, None, None))  # type: ignore[arg-type]
    calls = []
    def worker(task):
        calls.append(task)
        if task == "bad" and calls.count("bad") == 1:
            raise RuntimeError("retryable")
        return {"accepted_pairs": 1000}
    summary = ShardSupervisor(tmp_path, retry_limit=1).run(plan, worker)
    assert summary.completed == ("bad", "good")
