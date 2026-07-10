"""Independent two-session validation orchestration.

The session runner is intentionally small: production adapters own process and
connection lifecycle, while this gate verifies the required evidence before a
candidate can advance.
"""

from __future__ import annotations

from typing import Protocol

from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.runtime.models import RuntimeVerdict
from graph_sym_perf.validation.models import (
    EquivalentQueryPair,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    ValidationSession,
)


class IndependentSessionRunner(Protocol):
    def run_session(self, pair: EquivalentQueryPair, session_number: int) -> tuple[EquivalenceEvidence, ValidationSession]: ...


class IssueValidator:
    def validate(
        self,
        pair: EquivalentQueryPair,
        runner: IndependentSessionRunner,
        *,
        discovery_ratio: float | None,
        noise_floor_ms: float = 0.0,
        min_ratio: float = 3.0,
        min_ci_low: float = 2.0,
        timer_resolution_ms: float = 0.001,
    ) -> IssueValidationReport:
        for query in (pair.query_a, pair.query_b):
            purity = assess_query_purity(query)
            if not purity.safe:
                return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(False, False, purity.reason_codes), (), discovery_ratio, None, "unvalidated", purity.reason_codes)
        first_equivalence, first = runner.run_session(pair, 1)
        second_equivalence, second = runner.run_session(pair, 2)
        sessions = (first, second)
        if first.session_id == second.session_id or first.process_id == second.process_id:
            return IssueValidationReport(PipelineStage.INVALID if hasattr(PipelineStage, "INVALID") else PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("sessions_not_independent",)), sessions, discovery_ratio, None, "unstable", ("sessions_not_independent",))
        if any(not equivalence.equivalent or not equivalence.complete_result for equivalence in (first_equivalence, second_equivalence)):
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("equivalence_failed",)), sessions, discovery_ratio, None, "unvalidated", ("equivalence_failed",))
        if any(session.graph_fingerprint != pair.graph_snapshot_hash or session.dbms_fingerprint != pair.dbms_fingerprint or session.health_event for session in sessions):
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(False, False, ("session_fingerprint_or_health_failure",)), sessions, discovery_ratio, None, "unvalidated", ("session_fingerprint_or_health_failure",))
        evidences = tuple(session.evidence for session in sessions)
        if any(evidence.verdict is not RuntimeVerdict.PASS for evidence in evidences):
            return IssueValidationReport(PipelineStage.INCONCLUSIVE, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, None, "unstable", ("runtime_inconclusive",))
        directions = tuple(1 if (evidence.median_delta_ms or 0) > 0 else -1 for evidence in evidences)
        ratio = min(evidence.median_ratio or 0 for evidence in evidences)
        stable = "stable" if discovery_ratio and ratio >= discovery_ratio * 0.5 else "shrunk_but_real"
        absolute_floor = max(noise_floor_ms, 10 * timer_resolution_ms)
        reasons: list[str] = []
        if directions[0] != directions[1] or ratio < min_ratio or any((evidence.ci_ratio_low or 0) < min_ci_low for evidence in evidences):
            reasons.append("runtime_gate_failed")
        if any((evidence.median_delta_ms or 0) <= absolute_floor for evidence in evidences):
            reasons.append("absolute_delta_within_noise")
        if reasons:
            return IssueValidationReport(PipelineStage.REJECTED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, ratio, "unstable", tuple(reasons))
        return IssueValidationReport(PipelineStage.VALIDATION_PASSED, EquivalenceEvidence(True, True, ()), sessions, discovery_ratio, ratio, stable, ())
