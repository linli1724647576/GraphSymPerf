from __future__ import annotations

from typing import Mapping

from graph_sym_perf.explanation.models import ExplanationEvidence
from graph_sym_perf.validation.models import PipelineStage, SubmitReadyIssue


def collect_explanation(
    issue: SubmitReadyIssue,
    *,
    plan: Mapping[str, object] | None = None,
    profile: Mapping[str, object] | None = None,
) -> ExplanationEvidence:
    if not isinstance(issue, SubmitReadyIssue):
        raise TypeError("explanation collection accepts SubmitReadyIssue only")
    if issue.stage is not PipelineStage.SUBMIT_READY:
        raise ValueError("explanation is available only after submit readiness")
    return ExplanationEvidence(issue.stage, plan, profile)
