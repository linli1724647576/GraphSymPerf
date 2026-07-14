"""Public, side-effect-free verification of sealed issue validation evidence."""

from __future__ import annotations

import hashlib
from statistics import median

from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.runtime.statistics import (
    bootstrap_delta_ci,
    bootstrap_ratio_ci,
    percentile,
    relative_mad,
)
from graph_sym_perf.validation.models import (
    IssueValidationReport,
    PipelineStage,
    SubmissionPath,
)


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
        or report.pooled_delta_ms is None
        or report.pooled_ratio_ci_low is None
        or report.pooled_delta_ci_low_ms is None
        or report.submission_path not in {SubmissionPath.RELATIVE, SubmissionPath.ABSOLUTE}
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
            or len(control.paired_samples) != 20
            or sum(sample.order == "AB" for sample in control.paired_samples) != 10
            or sum(sample.order == "BA" for sample in control.paired_samples) != 10
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
    bootstrap_seed = int.from_bytes(
        hashlib.sha256(
            f"{report.candidate_hash}:pooled-validation-bootstrap-v1".encode("utf-8")
        ).digest()[:8],
        "big",
    )
    ci = bootstrap_ratio_ci(combined, resamples=10_000, seed=bootstrap_seed)
    delta_ci = bootstrap_delta_ci(
        combined,
        slower_side="b",
        resamples=10_000,
        seed=bootstrap_seed,
    )
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
    effective_noise = max(
        report.validation_policy.noise_floor_ms,
        control_p99,
        10 * report.validation_policy.timer_resolution_ms,
    )
    session_summaries = tuple(
        (
            float(
                median(
                    (sample.b_ms / sample.a_ms)
                    if slow_query == "b"
                    else (sample.a_ms / sample.b_ms)
                    for sample in session.evidence.paired_samples
                    if sample.a_ms is not None
                    and sample.b_ms is not None
                    and sample.a_ms > 0
                    and sample.b_ms > 0
                )
            ),
            float(
                median(
                    (sample.b_ms - sample.a_ms)
                    if slow_query == "b"
                    else (sample.a_ms - sample.b_ms)
                    for sample in session.evidence.paired_samples
                    if sample.a_ms is not None and sample.b_ms is not None
                )
            ),
        )
        for session in report.sessions
    )
    relative_pass = bool(
        all(session_ratio >= 2.0 for session_ratio, _ in session_summaries)
        and ratio >= 3.0
        and ci.low >= 2.0
        and delta > effective_noise
    )
    absolute_pass = bool(
        all(session_delta > 0.0 for _, session_delta in session_summaries)
        and delta >= max(5.0 * effective_noise, 50.0)
        and delta_ci.low > effective_noise
        and ratio >= 1.5
        and ci.low >= 1.2
    )
    path_valid = (
        report.submission_path is SubmissionPath.RELATIVE
        and relative_pass
        or report.submission_path is SubmissionPath.ABSOLUTE
        and absolute_pass
        and not relative_pass
    )
    return bool(
        path_valid
        and bool(report.metadata.get("absolute_also_passed", False))
        == (relative_pass and absolute_pass)
        and abs(report.validated_ratio - ratio) <= 1e-12
        and abs(report.pooled_delta_ms - delta) <= 1e-12
        and abs(report.pooled_ratio_ci_low - ci.low) <= 1e-12
        and abs(report.pooled_delta_ci_low_ms - delta_ci.low) <= 1e-12
    )


def combined_runtime_evidence(report: object) -> RuntimeEvidence | None:
    """Recompute one issue-grade runtime record from both independent sessions."""

    if not issue_validation_report_is_complete(report):
        return None
    assert isinstance(report, IssueValidationReport)
    slow_query = report.sessions[0].evidence.slow_query
    if slow_query not in {"a", "b"}:
        return None
    samples = tuple(
        sample
        for session in report.sessions
        for sample in session.evidence.paired_samples
    )
    oriented = tuple(
        (sample.a_ms, sample.b_ms) if slow_query == "b" else (sample.b_ms, sample.a_ms)
        for sample in samples
        if sample.a_ms is not None
        and sample.b_ms is not None
        and not sample.a_timeout
        and not sample.b_timeout
    )
    if len(oriented) != 40 or any(fast <= 0 for fast, _slow in oriented):
        return None
    ratios = tuple(slow / fast for fast, slow in oriented)
    bootstrap_seed = int.from_bytes(
        hashlib.sha256(
            f"{report.candidate_hash}:pooled-validation-bootstrap-v1".encode("utf-8")
        ).digest()[:8],
        "big",
    )
    ci = bootstrap_ratio_ci(oriented, resamples=10_000, seed=bootstrap_seed)
    delta_ci = bootstrap_delta_ci(
        oriented,
        slower_side="b",
        resamples=10_000,
        seed=bootstrap_seed,
    )
    sources = {session.evidence.measurement_source for session in report.sessions}
    protocols = {session.evidence.measurement_protocol for session in report.sessions}
    source = "ordinary_execute" if sources == {"ordinary_execute"} else None
    protocol = (
        "ordinary-execute-40-pair-combined-v1"
        if protocols == {"ordinary-execute-20-pair-v1"}
        else None
    )
    first = report.sessions[0]
    return RuntimeEvidence(
        verdict=RuntimeVerdict.PASS,
        query_a_hash=report.query_a_hash,
        query_b_hash=report.query_b_hash,
        paired_samples=samples,
        median_ratio=float(median(ratios)),
        median_delta_ms=float(median(slow - fast for fast, slow in oriented)),
        ci_ratio_low=ci.low,
        ci_ratio_high=ci.high,
        direction_consistency=sum(ratio > 1.0 for ratio in ratios) / len(ratios),
        relative_mad=relative_mad(ratios),
        reason_codes=(),
        slow_query=slow_query,
        slower_side=slow_query,
        ci_delta_low_ms=delta_ci.low,
        ci_delta_high_ms=delta_ci.high,
        graph_snapshot_hash=first.graph_fingerprint,
        dbms_fingerprint=first.dbms_fingerprint,
        measurement_source=source,
        measurement_protocol=protocol,
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
