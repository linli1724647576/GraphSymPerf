import time

from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


class _ShortDeadlineTask:
    accepted_query_pairs = 1
    max_runtime_seconds = 1


def _blocks_forever(_task):
    time.sleep(10)
    return {"accepted_pairs": 1}


def _raises_deterministic_evidence_error(_task):
    raise ValueError("reduction minimality proof does not verify")


def test_isolated_worker_budget_exhaustion_is_incomplete_without_blocking_supervisor(tmp_path) -> None:
    plan = RunPlan("timeout", 2, (_ShortDeadlineTask(),), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(
        tmp_path,
        retry_limit=0,
        process_isolation=True,
        hard_timeout_seconds=0.05,
    ).run(plan, _blocks_forever)

    assert summary.incomplete
    assert not summary.completed
    assert "WORKER_HUNG" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_isolated_deterministic_evidence_error_is_not_retried(tmp_path) -> None:
    plan = RunPlan("evidence", 2, (_ShortDeadlineTask(),), 1, SourceRevision(None, None, None))  # type: ignore[arg-type]

    summary = ShardSupervisor(
        tmp_path,
        retry_limit=3,
        process_isolation=True,
    ).run(plan, _raises_deterministic_evidence_error)

    assert summary.failed
    assert len(tuple((tmp_path / "shards").glob("*/attempt-*"))) == 1
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "EVIDENCE_INTEGRITY_ERROR" in events
