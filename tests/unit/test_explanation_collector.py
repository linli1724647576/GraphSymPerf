import pytest

from graph_sym_perf.explanation.collector import collect_explanation
from graph_sym_perf.validation.models import PipelineStage, SubmitReadyIssue
from graph_sym_perf.validation.promotion import promote
from tests.unit.validation.test_promotion import _certificate, _duplicate_decision, _reduction, _report


def _issue() -> SubmitReadyIssue:
    issue = promote(_report(), reduction_report=_reduction(), certificate=_certificate(), duplicate_decision=_duplicate_decision())
    assert issue is not None
    return issue


def test_explanation_requires_submit_ready_issue_and_cannot_change_stage() -> None:
    with pytest.raises(TypeError):
        collect_explanation({})  # type: ignore[arg-type]
    issue = _issue()
    evidence = collect_explanation(issue, plan={"operator": "Scan"}, profile={"db_hits": 3})
    assert evidence.issue_stage is PipelineStage.SUBMIT_READY
    assert issue.stage is PipelineStage.SUBMIT_READY
