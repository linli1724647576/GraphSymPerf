"""Public, side-effect-free verification of sealed issue validation evidence."""

from __future__ import annotations

from statistics import median

from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, percentile
from graph_sym_perf.validation.models import IssueValidationReport, PipelineStage


def issue_validation_report_is_complete(report: object) -> bool:
    if not isinstance(report, IssueValidationReport):
        return False
    if (
        report.stage is not PipelineStage.VALIDATION_PASSED
        or report.verify_integrity()
        or not report.equivalence.equivalent
        or not report.equivalence.complete_result
        or report.reason_codes
        or report.validated_ratio is None
        or report.validated_ratio < 3.0
        or report.magnitude_stability not in {"stable", "shrunk_but_real"}
        or len(report.sessions) != 2
        or not report.candidate_hash
        or not report.query_a_hash
        or not report.query_b_hash
        or not report.policy_hash
        or not report.certificate_hash
        or not report.rule_id
        or not report.proof_version
        or report.validation_protocol != "independent-two-session-v1"
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
        or session.evidence.query_a_hash != report.query_a_hash
        or session.evidence.query_b_hash != report.query_b_hash
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
    return bool(
        ratio >= 3.0
        and ci.low >= 2.0
        and delta > max(control_p99, 0.01)
        and abs(report.validated_ratio - ratio) <= 1e-12
    )


def _eligible_for_combined_gate(evidence: object) -> bool:
    verdict = getattr(evidence, "verdict", None)
    if verdict is RuntimeVerdict.PASS:
        return True
    return bool(
        verdict is RuntimeVerdict.FAIL
        and getattr(evidence, "reason_codes", ())
        and set(evidence.reason_codes)
        <= {"ratio_below_threshold", "ci_lower_below_threshold", "delta_within_noise"}
        and getattr(evidence, "slow_query", None) in {"a", "b"}
    )
