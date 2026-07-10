from __future__ import annotations

from graph_sym_perf.validation.models import (
    DeveloperStatus,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SubmitReadyIssue,
)


def promote(
    report: IssueValidationReport,
    *,
    reduction_status: str,
    certificate: SafetyCertificate | None,
    possible_duplicate: bool = False,
) -> SubmitReadyIssue | None:
    if report.stage is not PipelineStage.VALIDATION_PASSED:
        return None
    if not report.equivalence.equivalent or not report.equivalence.complete_result:
        return None
    if reduction_status != "reduced_valid" or certificate is None or certificate.tier != "core" or possible_duplicate:
        return None
    return SubmitReadyIssue(PipelineStage.SUBMIT_READY, DeveloperStatus.WAITING_REPLY, report, certificate)
