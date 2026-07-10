import time

from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


class _ShortDeadlineTask:
    accepted_query_pairs = 1
    max_runtime_seconds = 1


def _blocks_forever(_task):
    time.sleep(10)
    return {"accepted_pairs": 1}


def test_isolated_worker_timeout_does_not_block_the_supervisor(tmp_path) -> None:
    plan = RunPlan("timeout", 2, (_ShortDeadlineTask(),), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.05,
    ).run(plan, _blocks_forever)

    assert summary.failed
    assert not summary.completed
    assert "WORKER_HUNG" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
