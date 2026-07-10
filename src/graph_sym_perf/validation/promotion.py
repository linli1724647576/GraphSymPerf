from __future__ import annotations

from statistics import median

from graph_sym_perf.reducer.models import ReductionReport, ReductionStatus
from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, percentile
from graph_sym_perf.validation.models import (
    DuplicateDecision,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SubmitReadyIssue,
    _PROMOTION_TOKEN,
)


def promote(
    report: IssueValidationReport,
    *,
    reduction_report: ReductionReport,
    certificate: SafetyCertificate | None,
    duplicate_decision: DuplicateDecision,
) -> SubmitReadyIssue | None:
    if not _validation_gate_complete(report):
        return None
    if not _reduction_gate_complete(reduction_report) or not _direction_preserved(report, reduction_report):
        return None
    if not _certificate_complete(certificate) or not duplicate_decision.cleared:
        return None
    return SubmitReadyIssue._from_promotion(
        report,
        reduction_report,
        certificate,
        duplicate_decision,
        token=_PROMOTION_TOKEN,
    )


def submit_ready_issue_is_valid(issue: SubmitReadyIssue) -> bool:
    return (
        isinstance(issue, SubmitReadyIssue)
        and issue.stage is PipelineStage.SUBMIT_READY
        and _validation_gate_complete(issue.validation)
        and _reduction_gate_complete(issue.reduction)
        and _direction_preserved(issue.validation, issue.reduction)
        and _certificate_complete(issue.certificate)
        and issue.duplicate_decision.cleared
    )


def _validation_gate_complete(report: IssueValidationReport) -> bool:
    if (
        report.stage is not PipelineStage.VALIDATION_PASSED
        or not report.equivalence.equivalent
        or not report.equivalence.complete_result
        or report.reason_codes
        or report.validated_ratio is None
        or report.validated_ratio < 3.0
        or report.magnitude_stability not in {"stable", "shrunk_but_real"}
        or len(report.sessions) != 2
    ):
        return False
    first, second = report.sessions
    if first.session_id == second.session_id or first.process_id == second.process_id:
        return False
    if any(
        session.lifecycle is None
        or not session.lifecycle.complete
        or session.health_event is not None
        or not _eligible_for_combined_gate(session.evidence)
        or session.evidence.slow_query not in {"a", "b"}
        or session.graph_fingerprint != session.evidence.graph_snapshot_hash
        or session.dbms_fingerprint != session.evidence.dbms_fingerprint
        or not session.graph_fingerprint
        or not session.dbms_fingerprint
        or len(session.evidence.paired_samples) != 20
        or sum(sample.order == "AB" for sample in session.evidence.paired_samples) != 10
        or sum(sample.order == "BA" for sample in session.evidence.paired_samples) != 10
        or len(session.negative_controls) != 2
        or any(
            control.verdict is not RuntimeVerdict.PASS
            or not control.paired_samples
            or any(
                sample.a_ms is None
                or sample.b_ms is None
                or sample.a_timeout
                or sample.b_timeout
                for sample in control.paired_samples
            )
            for control in session.negative_controls
        )
        for session in report.sessions
    ):
        return False
    if not (
        first.evidence.slow_query == second.evidence.slow_query
        and first.graph_fingerprint == second.graph_fingerprint
        and first.dbms_fingerprint == second.dbms_fingerprint
        and first.lifecycle is not None
        and second.lifecycle is not None
        and first.lifecycle.connection_id != second.lifecycle.connection_id
    ):
        return False
    slow_query = first.evidence.slow_query
    combined = tuple(
        (sample.a_ms, sample.b_ms) if slow_query == "b" else (sample.b_ms, sample.a_ms)
        for session in report.sessions
        for sample in session.evidence.paired_samples
        if sample.a_ms is not None
        and sample.b_ms is not None
        and not sample.a_timeout
        and not sample.b_timeout
    )
    if len(combined) != 40 or any(fast <= 0 for fast, _ in combined):
        return False
    ratio = float(median(slow / fast for fast, slow in combined if fast > 0))
    delta = float(median(slow - fast for fast, slow in combined))
    ci = bootstrap_ratio_ci(combined)
    control_deltas = tuple(
        abs(sample.b_ms - sample.a_ms)
        for session in report.sessions
        for control in session.negative_controls
        for sample in control.paired_samples
        if sample.a_ms is not None
        and sample.b_ms is not None
        and not sample.a_timeout
        and not sample.b_timeout
    )
    control_p99 = percentile(control_deltas, 0.99) if control_deltas else 0.0
    return (
        ratio >= 3.0
        and ci.low >= 2.0
        and delta > max(control_p99, 0.01)
        and report.validated_ratio is not None
        and abs(report.validated_ratio - ratio) <= 1e-12
    )


def _reduction_gate_complete(reduction: ReductionReport) -> bool:
    evidence = reduction.final_evidence
    final_validation = reduction.final_validation
    return (
        reduction.status is ReductionStatus.REDUCED_VALID
        and reduction.locally_one_minimal
        and isinstance(final_validation, IssueValidationReport)
        and _validation_gate_complete(final_validation)
        and evidence.verdict is RuntimeVerdict.PASS
        and len(evidence.paired_samples) == 20
        and sum(sample.order == "AB" for sample in evidence.paired_samples) == 10
        and sum(sample.order == "BA" for sample in evidence.paired_samples) == 10
        and all(
            sample.a_ms is not None
            and sample.b_ms is not None
            and not sample.a_timeout
            and not sample.b_timeout
            for sample in evidence.paired_samples
        )
        and evidence.median_ratio is not None
        and evidence.median_ratio >= 3.0
        and evidence.ci_ratio_low is not None
        and evidence.ci_ratio_low >= 2.0
        and evidence.median_delta_ms is not None
        and evidence.median_delta_ms > 0
        and evidence.slow_query in {"a", "b"}
        and evidence.graph_snapshot_hash == reduction.reduced_pair.graph_snapshot_hash
        and evidence.dbms_fingerprint == reduction.reduced_pair.dbms_fingerprint
        and all(
            session.graph_fingerprint == reduction.reduced_pair.graph_snapshot_hash
            and session.dbms_fingerprint == reduction.reduced_pair.dbms_fingerprint
            for session in final_validation.sessions
        )
        and final_validation.sessions[0].evidence.slow_query == evidence.slow_query
    )


def _certificate_complete(certificate: SafetyCertificate | None) -> bool:
    return bool(
        certificate is not None
        and certificate.tier.lower() == "core"
        and certificate.rule_id.strip()
        and certificate.proof_version.strip()
        and certificate.site_class.strip()
    )


def _direction_preserved(report: IssueValidationReport, reduction: ReductionReport) -> bool:
    return bool(
        report.sessions
        and reduction.final_evidence.slow_query == report.sessions[0].evidence.slow_query
    )


def _eligible_for_combined_gate(evidence) -> bool:
    if evidence.verdict is RuntimeVerdict.PASS:
        return True
    return (
        evidence.verdict is RuntimeVerdict.FAIL
        and bool(evidence.reason_codes)
        and set(evidence.reason_codes)
        <= {"ratio_below_threshold", "ci_lower_below_threshold", "delta_within_noise"}
        and evidence.slow_query in {"a", "b"}
    )
