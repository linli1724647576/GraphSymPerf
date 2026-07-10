from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


def test_supervisor_counts_only_exactly_complete_shards(tmp_path) -> None:
    plan = RunPlan("acceptance", 2, ("complete", "short"), 2_000, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path).run(plan, lambda task: {"accepted_pairs": 1000 if task == "complete" else 999})
    assert summary.completed == ("complete",)
    assert summary.incomplete == ("short",)
