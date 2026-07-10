import pytest

from graph_sym_perf.explanation.collector import collect_explanation
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SubmitReadyIssue,
)


def _issue() -> SubmitReadyIssue:
    report = IssueValidationReport(PipelineStage.VALIDATION_PASSED, EquivalenceEvidence(True, True, ()), (), None, 4.0, "stable", ())
    return SubmitReadyIssue(PipelineStage.SUBMIT_READY, DeveloperStatus.WAITING_REPLY, report, SafetyCertificate("r", "core", "p", "where"))


def test_explanation_requires_submit_ready_issue_and_cannot_change_stage() -> None:
    with pytest.raises(TypeError):
        collect_explanation({})  # type: ignore[arg-type]
    issue = _issue()
    evidence = collect_explanation(issue, plan={"operator": "Scan"}, profile={"db_hits": 3})
    assert evidence.issue_stage is PipelineStage.SUBMIT_READY
    assert issue.stage is PipelineStage.SUBMIT_READY
