import os
from dataclasses import replace

import pytest

from graph_sym_perf.reducer.models import ReductionReport, ReductionStatus
from graph_sym_perf.runtime.models import EquivalentQueryPair, PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    DuplicateDecision,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
    SessionLifecycle,
    SubmitReadyIssue,
    ValidationSession,
)
from graph_sym_perf.validation.promotion import promote


def _evidence(*, verdict: RuntimeVerdict = RuntimeVerdict.PASS, slow_query: str = "b") -> RuntimeEvidence:
    samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 40.0) for index in range(20))
    return RuntimeEvidence(
        verdict,
        "a",
        "b",
        samples,
        4.0,
        30.0,
        4.0,
        4.0,
        1.0,
        0.01,
        (),
        slow_query=slow_query,
        graph_snapshot_hash="graph",
        dbms_fingerprint="dbms",
    )


def _lifecycle(index: int) -> SessionLifecycle:
    return SessionLifecycle(True, True, True, True, True, True, True, True, f"connection-{index}")


def _control() -> RuntimeEvidence:
    samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0) for index in range(20))
    return RuntimeEvidence(RuntimeVerdict.PASS, "a", "a", samples, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0, ())


def _report(*, stage: PipelineStage = PipelineStage.VALIDATION_PASSED, metadata=None) -> IssueValidationReport:
    sessions = tuple(
        ValidationSession(
            f"session-{index}",
            os.getpid() + index + 100,
            "graph",
            "dbms",
            _evidence(),
            (_control(), _control()),
            lifecycle=_lifecycle(index),
        )
        for index in (1, 2)
    )
    return IssueValidationReport(
        stage=stage,
        equivalence=EquivalenceEvidence(True, True, ()),
        sessions=sessions,
        discovery_ratio=8.0,
        validated_ratio=4.0,
        magnitude_stability="stable",
        reason_codes=(),
        metadata={} if metadata is None else metadata,
    )


def _reduction(*, status: ReductionStatus = ReductionStatus.REDUCED_VALID, verdict: RuntimeVerdict = RuntimeVerdict.PASS) -> ReductionReport:
    pair = EquivalentQueryPair("MATCH (n) RETURN n ORDER BY n", "MATCH (n) RETURN n ORDER BY n", {}, "graph", "dbms")
    return ReductionReport(
        status,
        pair,
        pair,
        (),
        (),
        _evidence(verdict=verdict),
        locally_one_minimal=True,
        final_validation=_report(),
    )


def _certificate(**changes) -> SafetyCertificate:
    values = {"rule_id": "pattern_order_symmetry", "tier": "core", "proof_version": "v1", "site_class": "match"}
    values.update(changes)
    return SafetyCertificate(**values)


def _duplicate_decision(*, possible: bool = False, resolved: bool = False) -> DuplicateDecision:
    return DuplicateDecision(possible, resolved, "https://example.test/dedup" if resolved else None)


def test_profile_or_plan_like_metadata_cannot_promote_without_validation() -> None:
    report = _report(
        stage=PipelineStage.SCREENED,
        metadata={"profile_supported": True, "pmc_novelty": 1000, "plan_changed": True},
    )

    assert promote(report, reduction_report=_reduction(), certificate=_certificate(), duplicate_decision=_duplicate_decision()) is None


def test_only_validation_passed_reduced_core_certificate_becomes_submit_ready() -> None:
    issue = promote(_report(), reduction_report=_reduction(), certificate=_certificate(), duplicate_decision=_duplicate_decision())

    assert issue is not None
    assert issue.stage is PipelineStage.SUBMIT_READY
    assert issue.developer_status is DeveloperStatus.WAITING_REPLY
    assert issue.reduction.status is ReductionStatus.REDUCED_VALID


def test_manually_resolved_possible_duplicate_can_promote_with_evidence() -> None:
    issue = promote(
        _report(),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(possible=True, resolved=True),
    )

    assert issue is not None


@pytest.mark.parametrize(
    ("reduction", "certificate", "possible_duplicate"),
    (
        (_reduction(status=ReductionStatus.REDUCTION_PARTIAL), _certificate(), False),
        (_reduction(verdict=RuntimeVerdict.FAIL), _certificate(), False),
        (_reduction(), _certificate(tier="experimental"), False),
        (_reduction(), _certificate(proof_version=""), False),
        (_reduction(), _certificate(), True),
    ),
)
def test_promotion_rejects_every_reduction_certificate_and_dedup_bypass(
    reduction: ReductionReport,
    certificate: SafetyCertificate,
    possible_duplicate: bool,
) -> None:
    assert promote(
        _report(),
        reduction_report=reduction,
        certificate=certificate,
        duplicate_decision=_duplicate_decision(possible=possible_duplicate),
    ) is None


def test_submit_ready_issue_cannot_be_constructed_outside_promotion() -> None:
    with pytest.raises(TypeError, match="promote"):
        SubmitReadyIssue(
            PipelineStage.SUBMIT_READY,
            DeveloperStatus.WAITING_REPLY,
            _report(),
            _reduction(),
            _certificate(),
        )


def test_promotion_recomputes_combined_samples_instead_of_trusting_report_summary() -> None:
    report = _report()
    weak_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 20.0) for index in range(20))
    weak_sessions = tuple(
        replace(session, evidence=replace(session.evidence, paired_samples=weak_samples))
        for session in report.sessions
    )

    assert promote(
        replace(report, sessions=weak_sessions),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None


def test_promotion_rechecks_negative_control_noise() -> None:
    report = _report()
    noisy_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 60.0) for index in range(20))
    noisy_control = replace(_control(), paired_samples=noisy_samples, median_delta_ms=50.0)
    noisy_sessions = tuple(
        replace(session, negative_controls=(noisy_control, session.negative_controls[1]))
        for session in report.sessions
    )

    assert promote(
        replace(report, sessions=noisy_sessions),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None


def test_promotion_rechecks_session_fingerprints_and_protocol_shape() -> None:
    report = _report()
    forged_session = replace(report.sessions[0], graph_fingerprint="other-graph")

    assert promote(
        replace(report, sessions=(forged_session, report.sessions[1])),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None


def test_reduced_pair_must_preserve_the_validated_slow_query_direction() -> None:
    reduction = _reduction()
    reversed_reduction = replace(reduction, final_evidence=replace(reduction.final_evidence, slow_query="a"))

    assert promote(
        _report(),
        reduction_report=reversed_reduction,
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None


def test_reduction_requires_complete_samples_independent_revalidation_and_minimality() -> None:
    reduction = _reduction()

    assert promote(
        _report(),
        reduction_report=replace(reduction, final_evidence=replace(reduction.final_evidence, paired_samples=())),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None
    assert promote(
        _report(),
        reduction_report=replace(reduction, final_validation=None),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None
    assert promote(
        _report(),
        reduction_report=replace(reduction, locally_one_minimal=False),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is None


def test_promotion_uses_combined_samples_not_single_session_ci_or_oriented_delta_sign() -> None:
    report = _report()
    aggregate_only = replace(
        report.sessions[0],
        evidence=replace(
            report.sessions[0].evidence,
            verdict=RuntimeVerdict.FAIL,
            ci_ratio_low=1.5,
            median_delta_ms=-999.0,
            reason_codes=("ci_lower_below_threshold", "delta_within_noise"),
        ),
    )
    report = replace(report, sessions=(aggregate_only, report.sessions[1]))

    assert promote(
        report,
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_decision=_duplicate_decision(),
    ) is not None
