"""Independent two-session validation orchestration.

The session runner is intentionally small: production adapters own process and
connection lifecycle, while this gate verifies the required evidence before a
candidate can advance.
"""

from __future__ import annotations

from statistics import median
from typing import Protocol

from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, percentile
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    ValidationSession,
)
from graph_sym_perf.validation.session_runner import SubprocessSessionRunner


class IndependentSessionRunner(Protocol):
    def run_session(
        self,
        candidate: CandidateBundle,
        session_number: int,
        *,
        expected_slow_query: str,
        noise_floor_ms: float,
        timer_resolution_ms: float,
    ) -> tuple[EquivalenceEvidence, ValidationSession]: ...


class IssueValidator:
    def validate(
        self,
        candidate: CandidateBundle,
        runner: IndependentSessionRunner,
    ) -> IssueValidationReport:
        if not isinstance(candidate, CandidateBundle):
            raise TypeError("IssueValidator requires a CandidateBundle")
        discovery_ratio = candidate.discovery_ratio
        integrity_reasons = candidate.verify_integrity()
        if integrity_reasons:
            return IssueValidationReport(
                PipelineStage.INVALID,
                EquivalenceEvidence(False, False, integrity_reasons),
                (),
                discovery_ratio,
                None,
                "unvalidated",
                integrity_reasons,
            )
        pair = candidate.pair
        policy = candidate.validation_policy
        volatile_functions = tuple(candidate.resolved_config.get("volatile_functions", ()))
        for query in (pair.query_a, pair.query_b):
            purity = assess_query_purity(query, volatile_functions=volatile_functions)
            if not purity.safe:
                return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(False, False, purity.reason_codes), (), discovery_ratio, None, "unvalidated", purity.reason_codes)
        run_options = {
            "expected_slow_query": candidate.discovery_slow_query,
            "noise_floor_ms": policy.noise_floor_ms,
            "timer_resolution_ms": policy.timer_resolution_ms,
        }
        first_equivalence, first = runner.run_session(candidate, 1, **run_options)
        second_equivalence, second = runner.run_session(candidate, 2, **run_options)
        sessions = (first, second)
        if any(session.lifecycle is None or not session.lifecycle.complete for session in sessions):
            return IssueValidationReport(PipelineStage.INVALID, EquivalenceEvidence(False, False, ("session_lifecycle_incomplete",)), sessions, discovery_ratio, None, "unstable", ("session_lifecycle_incomplete",))
        connection_ids = tuple(session.lifecycle.connection_id for session in sessions if session.lifecycle is not None)
        if (
            first.session_id == second.session_id
            or first.process_id == second.process_id
            or len(set(connection_ids)) != 2
        ):
            return IssueValidationReport(PipelineStage.INVALID, EquivalenceEvidence(False, False, ("sessions_not_independent",)), sessions, discovery_ratio, None, "unstable", ("sessions_not_independent",))
        if any(not equivalence.equivalent or not equivalence.complete_result for equivalence in (first_equivalence, second_equivalence)):
            equivalence_reasons = tuple(
                dict.fromkeys(
                    reason
                    for equivalence in (first_equivalence, second_equivalence)
                    for reason in equivalence.reason_codes
                )
            ) or ("equivalence_failed",)
            return IssueValidationReport(
                PipelineStage.REJECTED,
                EquivalenceEvidence(False, False, equivalence_reasons),
                sessions,
                discovery_ratio,
                None,
                "unvalidated",
                equivalence_reasons,
            )
        if any(
            session.graph_fingerprint != pair.graph_snapshot_hash
            or session.dbms_fingerprint != pair.dbms_fingerprint
            or session.evidence.graph_snapshot_hash != pair.graph_snapshot_hash
            or session.evidence.dbms_fingerprint != pair.dbms_fingerprint
            or session.health_event
            for session in sessions
        ):
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("session_fingerprint_or_health_failure",)), sessions, discovery_ratio, None, "unvalidated", ("session_fingerprint_or_health_failure",))
        evidences = tuple(session.evidence for session in sessions)
        if any(not _eligible_for_combined_gate(evidence) for evidence in evidences):
            return IssueValidationReport(PipelineStage.INCONCLUSIVE, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, None, "unstable", ("runtime_inconclusive",))
        slow_queries = tuple(evidence.slow_query for evidence in evidences)
        reasons: list[str] = []
        if (
            slow_queries[0] not in {"a", "b"}
            or slow_queries[0] != slow_queries[1]
            or slow_queries[0] != candidate.discovery_slow_query
        ):
            reasons.append("direction_mismatch")
        if any(not _valid_session_protocol(session) for session in sessions):
            reasons.append("invalid_measurement_protocol")
        combined = _combined_samples(sessions, slow_queries[0]) if not reasons else ()
        if not combined:
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, None, "unstable", tuple(reasons or ("missing_paired_samples",)))
        ratios = tuple(slow / fast for fast, slow in combined if fast > 0)
        ratio = float(median(ratios)) if ratios else None
        delta = float(median(slow - fast for fast, slow in combined))
        ci = bootstrap_ratio_ci(combined)
        negative_control_floor = _negative_control_p99(sessions)
        absolute_floor = max(policy.noise_floor_ms, negative_control_floor, 10 * policy.timer_resolution_ms)
        if ratio is None or ratio < policy.min_ratio:
            reasons.append("ratio_below_threshold")
        if ci.low < policy.min_ci_low:
            reasons.append("ci_lower_below_threshold")
        if delta <= absolute_floor:
            reasons.append("absolute_delta_within_noise")
        if negative_control_floor > 0 and delta <= negative_control_floor:
            reasons.append("negative_control_noise")
        stable = "stable" if discovery_ratio and ratio is not None and ratio >= discovery_ratio * 0.5 else "shrunk_but_real"
        if reasons:
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, ratio, "unstable", tuple(reasons))
        return IssueValidationReport(PipelineStage.VALIDATION_PASSED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, ratio, stable, ())


def _combined_samples(sessions: tuple[ValidationSession, ...], slow_query: str | None) -> tuple[tuple[float, float], ...]:
    if slow_query not in {"a", "b"}:
        return ()
    combined: list[tuple[float, float]] = []
    for session in sessions:
        for sample in session.evidence.paired_samples:
            if sample.a_ms is None or sample.b_ms is None or sample.a_timeout or sample.b_timeout:
                continue
            combined.append((sample.a_ms, sample.b_ms) if slow_query == "b" else (sample.b_ms, sample.a_ms))
    return tuple(combined)


def _valid_session_protocol(session: ValidationSession) -> bool:
    samples = session.evidence.paired_samples
    if len(samples) != 20 or sum(sample.order == "AB" for sample in samples) != 10 or sum(sample.order == "BA" for sample in samples) != 10:
        return False
    if len(session.negative_controls) != 2:
        return False
    return all(
        control.verdict is RuntimeVerdict.PASS
        and bool(control.paired_samples)
        and all(
            sample.a_ms is not None
            and sample.b_ms is not None
            and not sample.a_timeout
            and not sample.b_timeout
            for sample in control.paired_samples
        )
        for control in session.negative_controls
    )


def _eligible_for_combined_gate(evidence) -> bool:
    if evidence.verdict is RuntimeVerdict.PASS:
        return True
    aggregate_only_reasons = {
        "ratio_below_threshold",
        "ci_lower_below_threshold",
        "delta_within_noise",
    }
    return (
        evidence.verdict is RuntimeVerdict.FAIL
        and bool(evidence.reason_codes)
        and set(evidence.reason_codes) <= aggregate_only_reasons
        and evidence.slow_query in {"a", "b"}
    )


def _negative_control_p99(sessions: tuple[ValidationSession, ...]) -> float:
    deltas = tuple(
        abs(sample.b_ms - sample.a_ms)
        for session in sessions
        for control in session.negative_controls
        for sample in control.paired_samples
        if sample.a_ms is not None and sample.b_ms is not None and not sample.a_timeout and not sample.b_timeout
    )
    return percentile(deltas, 0.99) if deltas else 0.0
