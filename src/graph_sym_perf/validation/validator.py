"""Independent two-session validation orchestration.

The session runner is intentionally small: production adapters own process and
connection lifecycle, while this gate verifies the required evidence before a
candidate can advance.
"""

from __future__ import annotations

import hashlib
from statistics import median
from typing import Protocol

from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.sampling.yield_store import ValidatedYieldStore
from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.runtime.statistics import bootstrap_delta_ci, bootstrap_ratio_ci, percentile
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    SubmissionPath,
    ValidationSession,
    _VALIDATION_REPORT_TOKEN,
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
    def __init__(self, *, yield_store: ValidatedYieldStore | None = None) -> None:
        self.yield_store = yield_store

    def validate(
        self,
        candidate: CandidateBundle,
        runner: IndependentSessionRunner,
    ) -> IssueValidationReport:
        report = self._validate(candidate, runner)
        if self.yield_store is not None:
            report_hash = hashlib.sha256(stable_json_dumps(report).encode("utf-8")).hexdigest()
            self.yield_store.record_completed_validation(
                rule_id=candidate.certificate.rule_id,
                passed=report.stage is PipelineStage.VALIDATION_PASSED,
                validation_report_hash=report_hash,
            )
        return report

    def _validate(
        self,
        candidate: CandidateBundle,
        runner: IndependentSessionRunner,
    ) -> IssueValidationReport:
        if not isinstance(candidate, CandidateBundle):
            raise TypeError("IssueValidator requires a CandidateBundle")
        discovery_ratio = candidate.discovery_ratio
        integrity_reasons = candidate.verify_integrity()
        if integrity_reasons:
            return _bound_report(candidate,
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
                return _bound_report(candidate, PipelineStage.REJECTED, EquivalenceEvidence(False, False, purity.reason_codes), (), discovery_ratio, None, "unvalidated", purity.reason_codes)
        run_options = {
            "expected_slow_query": candidate.discovery_slow_query,
            "noise_floor_ms": policy.noise_floor_ms,
            "timer_resolution_ms": policy.timer_resolution_ms,
        }
        first_equivalence, first = runner.run_session(candidate, 1, **run_options)
        second_equivalence, second = runner.run_session(candidate, 2, **run_options)
        sessions = (first, second)
        if any(session.lifecycle is None or not session.lifecycle.complete for session in sessions):
            return _bound_report(candidate, PipelineStage.INVALID, EquivalenceEvidence(False, False, ("session_lifecycle_incomplete",)), sessions, discovery_ratio, None, "unstable", ("session_lifecycle_incomplete",))
        connection_ids = tuple(session.lifecycle.connection_id for session in sessions if session.lifecycle is not None)
        if (
            first.session_id == second.session_id
            or first.process_id == second.process_id
            or len(set(connection_ids)) != 2
        ):
            return _bound_report(candidate, PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("sessions_not_independent",)), sessions, discovery_ratio, None, "unstable", ("sessions_not_independent",))
        if any(not equivalence.equivalent or not equivalence.complete_result for equivalence in (first_equivalence, second_equivalence)):
            equivalence_reasons = tuple(
                dict.fromkeys(
                    reason
                    for equivalence in (first_equivalence, second_equivalence)
                    for reason in equivalence.reason_codes
                )
            ) or ("equivalence_failed",)
            return _bound_report(candidate,
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
            or session.evidence.query_a_hash != candidate.content_hashes["query_a"]
            or session.evidence.query_b_hash != candidate.content_hashes["query_b"]
            or session.health_event
            for session in sessions
        ):
            return _bound_report(candidate, PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("session_fingerprint_or_health_failure",)), sessions, discovery_ratio, None, "unvalidated", ("session_fingerprint_or_health_failure",))
        evidences = tuple(session.evidence for session in sessions)
        if any(not _eligible_for_combined_gate(evidence) for evidence in evidences):
            timeout_confirmed = _timeout_validation_confirmed(candidate, sessions)
            return _bound_report(
                candidate,
                PipelineStage.INCONCLUSIVE,
                EquivalenceEvidence(True, True, ()),
                sessions,
                discovery_ratio,
                None,
                "unstable",
                (("timeout_validation_confirmed",) if timeout_confirmed else ("runtime_inconclusive",)),
                metadata={"timeout_validation_confirmed": timeout_confirmed},
            )
        slow_queries = tuple(evidence.slower_side or evidence.slow_query for evidence in evidences)
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
            return _bound_report(candidate, PipelineStage.REJECTED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, None, "unstable", tuple(reasons or ("missing_paired_samples",)))
        ratios = tuple(slow / fast for fast, slow in combined if fast > 0)
        ratio = float(median(ratios)) if ratios else None
        delta = float(median(slow - fast for fast, slow in combined))
        bootstrap_seed = int.from_bytes(
            hashlib.sha256(
                f"{candidate.candidate_hash}:pooled-validation-bootstrap-v1".encode("utf-8")
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
        negative_control_floor = _negative_control_p99(sessions)
        effective_noise = max(
            policy.noise_floor_ms,
            negative_control_floor,
            10 * policy.timer_resolution_ms,
        )
        session_summaries = tuple(_session_summary(session, slow_queries[0]) for session in sessions)
        relative_pass = bool(
            ratio is not None
            and all(session_ratio >= 2.0 for session_ratio, _session_delta in session_summaries)
            and ratio >= 3.0
            and ci.low >= 2.0
            and delta > effective_noise
        )
        absolute_pass = bool(
            ratio is not None
            and all(session_delta > 0.0 for _session_ratio, session_delta in session_summaries)
            and delta >= max(5.0 * effective_noise, 50.0)
            and delta_ci.low > effective_noise
            and ratio >= 1.5
            and ci.low >= 1.2
        )
        if ratio is None or ratio < 3.0:
            reasons.append("ratio_below_threshold")
        if ci.low < 2.0:
            reasons.append("ci_lower_below_threshold")
        if delta <= effective_noise:
            reasons.append("absolute_delta_within_noise")
        if negative_control_floor > 0 and delta <= negative_control_floor:
            reasons.append("negative_control_noise")
        stable = "stable" if discovery_ratio and ratio is not None and ratio >= discovery_ratio * 0.5 else "shrunk_but_real"
        if not (relative_pass or absolute_pass):
            if not reasons:
                reasons.append("submission_thresholds_not_met")
            return _bound_report(
                candidate,
                PipelineStage.REJECTED,
                EquivalenceEvidence(True, True, ()),
                sessions,
                discovery_ratio,
                ratio,
                "unstable",
                tuple(dict.fromkeys(reasons)),
                pooled_delta_ms=delta,
                pooled_ratio_ci_low=ci.low,
                pooled_delta_ci_low_ms=delta_ci.low,
            )
        submission_path = (
            SubmissionPath.RELATIVE if relative_pass else SubmissionPath.ABSOLUTE
        )
        return _bound_report(
            candidate,
            PipelineStage.VALIDATION_PASSED,
            EquivalenceEvidence(True, True, ()),
            sessions,
            discovery_ratio,
            ratio,
            stable,
            (),
            pooled_delta_ms=delta,
            pooled_ratio_ci_low=ci.low,
            pooled_delta_ci_low_ms=delta_ci.low,
            submission_path=submission_path,
            metadata={"absolute_also_passed": relative_pass and absolute_pass},
        )


def _bound_report(
    candidate: CandidateBundle,
    stage: PipelineStage,
    equivalence: EquivalenceEvidence,
    sessions: tuple[ValidationSession, ...],
    discovery_ratio: float | None,
    validated_ratio: float | None,
    magnitude_stability: str,
    reason_codes: tuple[str, ...],
    *,
    pooled_delta_ms: float | None = None,
    pooled_ratio_ci_low: float | None = None,
    pooled_delta_ci_low_ms: float | None = None,
    submission_path: SubmissionPath | None = None,
    metadata: dict[str, object] | None = None,
) -> IssueValidationReport:
    return IssueValidationReport._from_validator(
        stage=stage,
        equivalence=equivalence,
        sessions=sessions,
        discovery_ratio=discovery_ratio,
        validated_ratio=validated_ratio,
        pooled_delta_ms=pooled_delta_ms,
        pooled_ratio_ci_low=pooled_ratio_ci_low,
        pooled_delta_ci_low_ms=pooled_delta_ci_low_ms,
        submission_path=submission_path,
        magnitude_stability=magnitude_stability,
        reason_codes=reason_codes,
        candidate=candidate,
        metadata=metadata,
        token=_VALIDATION_REPORT_TOKEN,
    )


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


def _session_summary(session: ValidationSession, slow_query: str) -> tuple[float, float]:
    oriented = _combined_samples((session,), slow_query)
    if len(oriented) != 20 or any(fast <= 0 for fast, _slow in oriented):
        return (0.0, 0.0)
    return (
        float(median(slow / fast for fast, slow in oriented)),
        float(median(slow - fast for fast, slow in oriented)),
    )


def _timeout_validation_confirmed(
    candidate: CandidateBundle,
    sessions: tuple[ValidationSession, ...],
) -> bool:
    if len(sessions) != 2:
        return False
    timeout_sides: list[str] = []
    for session in sessions:
        evidence = session.evidence
        if evidence.verdict is not RuntimeVerdict.INCONCLUSIVE:
            return False
        if "one_sided_timeout" not in evidence.reason_codes:
            return False
        samples = evidence.paired_samples
        if (
            len(samples) != 20
            or sum(sample.order == "AB" for sample in samples) != 10
            or sum(sample.order == "BA" for sample in samples) != 10
            or not _timeout_negative_controls_complete(candidate, session)
        ):
            return False
        a_only = all(
            sample.a_timeout
            and not sample.b_timeout
            and sample.a_ms is None
            and sample.b_ms is not None
            for sample in samples
        )
        b_only = all(
            sample.b_timeout
            and not sample.a_timeout
            and sample.b_ms is None
            and sample.a_ms is not None
            for sample in samples
        )
        if a_only == b_only:
            return False
        timeout_sides.append("a" if a_only else "b")
    return (
        timeout_sides[0]
        == timeout_sides[1]
        == candidate.discovery_slow_query
    )


def _timeout_negative_controls_complete(
    candidate: CandidateBundle,
    session: ValidationSession,
) -> bool:
    if len(session.negative_controls) != 2:
        return False
    expected_query_hashes = (
        candidate.content_hashes["query_a"],
        candidate.content_hashes["query_b"],
    )
    for control, expected_query_hash in zip(
        session.negative_controls,
        expected_query_hashes,
        strict=True,
    ):
        if (
            control.query_a_hash != expected_query_hash
            or control.query_b_hash != expected_query_hash
            or control.graph_snapshot_hash != candidate.pair.graph_snapshot_hash
            or control.graph_snapshot_hash != session.graph_fingerprint
            or control.dbms_fingerprint != candidate.pair.dbms_fingerprint
            or control.dbms_fingerprint != session.dbms_fingerprint
            or control.verdict is not RuntimeVerdict.PASS
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
        ):
            return False
    return True


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
