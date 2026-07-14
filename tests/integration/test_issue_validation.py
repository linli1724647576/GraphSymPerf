import os
import hashlib
from dataclasses import replace

import pytest

from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.sampling.yield_store import ValidatedYieldStore
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalentQueryPair,
    EquivalenceEvidence,
    SessionLifecycle,
    SubmissionPath,
    ValidationPolicy,
    ValidationSession,
    SafetyCertificate,
)
from graph_sym_perf.validation.validator import IssueValidator
from graph_sym_perf.validation.evidence import issue_validation_report_is_complete


def _certificate() -> SafetyCertificate:
    rule = RuleRegistry.default().get("pattern_order_symmetry")
    return SafetyCertificate(
        rule.rule_id,
        "clauses[0].pattern.components[0]",
        (),
        "graph_pattern",
        "pattern_defined",
        "match",
        rule.purity_requirements,
        rule.proof_id,
        "capability-hash",
    )


def _bundle(*, discovery_slow_query: str = "b", noise_floor_ms: float = 0.0) -> CandidateBundle:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    pair = EquivalentQueryPair(
        "MATCH (n) RETURN n ORDER BY n.id",
        "MATCH (n) WHERE true RETURN n ORDER BY n.id",
        {},
        snapshot.content_hash,
        "dbms",
    )
    return CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version="dbms-version",
        resolved_config={"runtime": {"validation_pairs": 20}},
        discovery_slow_query=discovery_slow_query,
        discovery_ratio=5.0,
        validation_policy=ValidationPolicy(noise_floor_ms=noise_floor_ms),
        certificate=_certificate(),
    )


def _lifecycle(session_number: int) -> SessionLifecycle:
    return SessionLifecycle(
        process_started=True,
        adapter_created=True,
        connection_opened=True,
        graph_reset=True,
        graph_loaded=True,
        graph_verified=True,
        connection_closed=True,
        process_exited=True,
        connection_id=f"connection-{session_number}",
    )


class FakeIndependentRunner:
    def run_session(self, candidate, session_number, *, expected_slow_query, noise_floor_ms, timer_resolution_ms):
        assert expected_slow_query == candidate.discovery_slow_query
        a_ms, b_ms = (40.0, 10.0) if expected_slow_query == "a" else (10.0, 40.0)
        samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", a_ms, b_ms) for index in range(20))
        evidence = RuntimeEvidence(
            RuntimeVerdict.PASS,
            hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest(),
            hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest(),
            samples,
            4.0,
            30.0,
            4.0,
            4.0,
            1.0,
            0.0,
            (),
            slow_query=expected_slow_query,
            graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
            dbms_fingerprint=candidate.pair.dbms_fingerprint,
        )
        control_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0) for index in range(20))
        query_a_hash = hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest()
        query_b_hash = hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest()
        controls = (
            RuntimeEvidence(
                RuntimeVerdict.PASS,
                query_a_hash,
                query_a_hash,
                control_samples,
                1.0,
                0.0,
                1.0,
                1.0,
                0.5,
                0.0,
                (),
                graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
                dbms_fingerprint=candidate.pair.dbms_fingerprint,
            ),
            RuntimeEvidence(
                RuntimeVerdict.PASS,
                query_b_hash,
                query_b_hash,
                control_samples,
                1.0,
                0.0,
                1.0,
                1.0,
                0.5,
                0.0,
                (),
                graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
                dbms_fingerprint=candidate.pair.dbms_fingerprint,
            ),
        )
        return EquivalenceEvidence(True, True, ()), ValidationSession(
            f"session-{session_number}",
            os.getpid() + session_number + 100,
            candidate.pair.graph_snapshot_hash,
            candidate.pair.dbms_fingerprint,
            evidence,
            controls,
            lifecycle=_lifecycle(session_number),
        )


class FixedSessionRunner(FakeIndependentRunner):
    def __init__(self, sessions: tuple[tuple[float, float], tuple[float, float]]) -> None:
        self.sessions = sessions

    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        a_ms, b_ms = self.sessions[session_number - 1]
        samples = tuple(
            PairedSample("AB" if index % 2 == 0 else "BA", a_ms, b_ms)
            for index in range(20)
        )
        ratio = max(a_ms, b_ms) / min(a_ms, b_ms)
        delta = abs(b_ms - a_ms)
        evidence = replace(
            session.evidence,
            paired_samples=samples,
            median_ratio=ratio,
            median_delta_ms=delta,
            ci_ratio_low=ratio,
            ci_ratio_high=ratio,
            ci_delta_low_ms=delta,
            ci_delta_high_ms=delta,
            slow_query="b",
            slower_side="b",
            measurement_source="ordinary_execute",
            measurement_protocol="ordinary-execute-20-pair-v1",
        )
        return equivalence, replace(session, evidence=evidence)


def test_relative_submission_path_requires_both_sessions_and_pooled_ci() -> None:
    report = IssueValidator().validate(
        _bundle(noise_floor_ms=5.0),
        FixedSessionRunner(((25.0, 80.0), (25.0, 77.5))),
    )

    assert report.stage.value == "validation_passed"
    assert report.submission_path is SubmissionPath.RELATIVE
    assert report.pooled_ratio_ci_low is not None
    assert report.pooled_ratio_ci_low >= 2.0


def test_absolute_submission_path_accepts_stable_large_delta() -> None:
    report = IssueValidator().validate(
        _bundle(noise_floor_ms=10.0),
        FixedSessionRunner(((100.0, 180.0), (100.0, 170.0))),
    )

    assert report.stage.value == "validation_passed"
    assert report.submission_path is SubmissionPath.ABSOLUTE
    assert report.pooled_delta_ci_low_ms is not None
    assert report.pooled_delta_ci_low_ms > 10.0


def test_both_submission_gates_deterministically_choose_relative() -> None:
    report = IssueValidator().validate(
        _bundle(noise_floor_ms=5.0),
        FixedSessionRunner(((20.0, 80.0), (20.0, 80.0))),
    )

    assert report.submission_path is SubmissionPath.RELATIVE
    assert report.metadata["absolute_also_passed"] is True


def test_validator_rejects_same_process_sessions() -> None:
    class SameProcessRunner(FakeIndependentRunner):
        def run_session(self, *args, **kwargs):
            equivalence, session = super().run_session(*args, **kwargs)
            return equivalence, replace(session, process_id=17)

    report = IssueValidator().validate(_bundle(), SameProcessRunner())

    assert report.stage.value == "rejected"
    assert "sessions_not_independent" in report.reason_codes


class TimeoutSessionRunner(FakeIndependentRunner):
    def __init__(self, mode: str = "confirmed") -> None:
        self.mode = mode

    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        timeout_side = "a" if self.mode == "side_mismatch" and session_number == 2 else "b"
        samples = tuple(
            PairedSample(
                "AB" if index % 2 == 0 else "BA",
                None if timeout_side == "a" else 10.0,
                None if timeout_side == "b" else 10.0,
                a_timeout=timeout_side == "a",
                b_timeout=timeout_side == "b",
            )
            for index in range(20)
        )
        if self.mode == "both_timeout":
            samples = tuple(
                PairedSample(sample.order, None, None, True, True)
                for sample in samples
            )
        if self.mode == "partial_timeout":
            samples = (
                samples[0],
                *tuple(
                    PairedSample(sample.order, 10.0, 10.0)
                    for sample in samples[1:]
                ),
            )
        if self.mode == "other_incomplete":
            samples = tuple(
                PairedSample(sample.order, None, sample.b_ms, False, sample.b_timeout)
                for sample in samples
            )
        if self.mode == "target_schedule":
            samples = (
                replace(samples[0], order="BA"),
                *samples[1:],
            )
        evidence = replace(
            session.evidence,
            verdict=RuntimeVerdict.INCONCLUSIVE,
            paired_samples=samples,
            median_ratio=None,
            median_delta_ms=None,
            ci_ratio_low=None,
            ci_ratio_high=None,
            reason_codes=(
                "both_timeout" if self.mode == "both_timeout" else "one_sided_timeout",
            ),
            slow_query=timeout_side,
            slower_side=timeout_side,
        )
        if self.mode == "health" and session_number == 2:
            session = replace(session, health_event="dbms_restart")
        if self.mode == "graph_mismatch" and session_number == 2:
            session = replace(session, graph_fingerprint="different-graph")
        controls = session.negative_controls
        if self.mode == "control_invalid":
            controls = (replace(controls[0], verdict=RuntimeVerdict.INVALID), controls[1])
        elif self.mode == "control_empty":
            controls = (controls[0], replace(controls[1], paired_samples=()))
        elif self.mode == "control_short":
            controls = (
                controls[0],
                replace(controls[1], paired_samples=controls[1].paired_samples[:-1]),
            )
        elif self.mode == "control_schedule":
            controls = (
                controls[0],
                replace(
                    controls[1],
                    paired_samples=(
                        replace(controls[1].paired_samples[0], order="BA"),
                        *controls[1].paired_samples[1:],
                    ),
                ),
            )
        elif self.mode == "control_hash_swap":
            controls = (controls[1], controls[0])
        elif self.mode == "control_hash_arbitrary":
            controls = (
                replace(controls[0], query_a_hash="f" * 64, query_b_hash="f" * 64),
                controls[1],
            )
        elif self.mode == "control_duplicate":
            controls = (controls[0], controls[0])
        elif self.mode == "control_graph_mismatch":
            controls = (
                replace(controls[0], graph_snapshot_hash="different-graph"),
                controls[1],
            )
        elif self.mode == "control_dbms_mismatch":
            controls = (
                replace(controls[0], dbms_fingerprint="different-dbms"),
                controls[1],
            )
        return equivalence, replace(session, evidence=evidence, negative_controls=controls)


def test_same_side_timeout_confirmation_stays_diagnostic_inconclusive() -> None:
    report = IssueValidator().validate(_bundle(), TimeoutSessionRunner())

    assert report.stage.value == "inconclusive"
    assert report.submission_path is None
    assert report.metadata["timeout_validation_confirmed"] is True
    assert "timeout_validation_confirmed" in report.reason_codes
    assert not issue_validation_report_is_complete(report)


@pytest.mark.parametrize(
    "mode",
    (
        "side_mismatch",
        "both_timeout",
        "partial_timeout",
        "other_incomplete",
        "target_schedule",
        "control_invalid",
        "control_empty",
        "control_short",
        "control_schedule",
        "control_hash_swap",
        "control_hash_arbitrary",
        "control_duplicate",
        "control_graph_mismatch",
        "control_dbms_mismatch",
        "health",
        "graph_mismatch",
    ),
)
def test_timeout_confirmation_fails_closed_on_direction_or_identity_health_mismatch(
    mode: str,
) -> None:
    report = IssueValidator().validate(_bundle(), TimeoutSessionRunner(mode))

    assert report.submission_path is None
    assert report.metadata.get("timeout_validation_confirmed", False) is False
    assert not issue_validation_report_is_complete(report)


def test_timeout_confirmation_requires_the_frozen_discovery_direction() -> None:
    report = IssueValidator().validate(
        _bundle(discovery_slow_query="a"),
        TimeoutSessionRunner(),
    )

    assert report.stage.value == "inconclusive"
    assert report.submission_path is None
    assert report.metadata["timeout_validation_confirmed"] is False


def test_two_fresh_sessions_are_required_for_validation_passed() -> None:
    bundle = _bundle()
    report = IssueValidator().validate(bundle, FakeIndependentRunner())
    assert report.stage.value == "validation_passed"
    assert report.validated_ratio == 4.0
    assert report.candidate_hash == bundle.candidate_hash
    assert report.query_a_hash == bundle.content_hashes["query_a"]
    assert report.query_b_hash == bundle.content_hashes["query_b"]
    assert report.policy_hash == bundle.content_hashes["validation_policy"]
    assert report.certificate_hash == bundle.content_hashes["certificate"]
    assert report.rule_id == bundle.certificate.rule_id


def test_completed_validation_is_the_only_source_of_sampler_yield(tmp_path) -> None:
    store = ValidatedYieldStore(tmp_path / "validated_yield.jsonl")
    store.record_accepted_pair(rule_id="pattern_order_symmetry", accepted_pair_hash="a" * 64)

    report = IssueValidator(yield_store=store).validate(_bundle(), FakeIndependentRunner())

    assert report.stage.value == "validation_passed"
    assert store.snapshot(("pattern_order_symmetry",)).counts == {
        "pattern_order_symmetry": (1, 1)
    }


def test_validation_rejects_a_ratio_that_is_within_aa_noise_floor() -> None:
    report = IssueValidator().validate(_bundle(noise_floor_ms=40.0), FakeIndependentRunner())

    assert report.stage.value == "rejected"
    assert "absolute_delta_within_noise" in report.reason_codes


class DirectionChangingRunner(FakeIndependentRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        evidence = RuntimeEvidence(
            **{
                **session.evidence.__dict__,
                "slow_query": "a" if session_number == 1 else "b",
            }
        )
        return equivalence, ValidationSession(**{**session.__dict__, "evidence": evidence})


def test_validation_compares_slow_query_identity_not_oriented_delta_sign() -> None:
    report = IssueValidator().validate(_bundle(), DirectionChangingRunner())

    assert report.stage.value == "rejected"
    assert "direction_mismatch" in report.reason_codes


class MisleadingSessionSummaryRunner(FakeIndependentRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 20.0) for index in range(20))
        evidence = RuntimeEvidence(
            **{
                **session.evidence.__dict__,
                "paired_samples": samples,
                "median_ratio": 4.0,
                "median_delta_ms": 30.0,
                "ci_ratio_low": 3.0,
                "ci_ratio_high": 5.0,
            }
        )
        return equivalence, ValidationSession(**{**session.__dict__, "evidence": evidence})


def test_validation_recomputes_gates_from_combined_paired_samples() -> None:
    report = IssueValidator().validate(_bundle(), MisleadingSessionSummaryRunner())

    assert report.stage.value == "rejected"
    assert report.validated_ratio == 2.0
    assert "ratio_below_threshold" in report.reason_codes


class NoisyNegativeControlRunner(FakeIndependentRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        control_samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 60.0) for index in range(20))
        control = RuntimeEvidence(RuntimeVerdict.PASS, "a", "a", control_samples, 1.0, 50.0, 1.0, 1.0, 0.5, 0.0, ())
        return equivalence, ValidationSession(**{**session.__dict__, "negative_controls": (control, session.negative_controls[1])})


def test_validation_rejects_target_delta_within_negative_control_p99() -> None:
    report = IssueValidator().validate(_bundle(), NoisyNegativeControlRunner())

    assert report.stage.value == "rejected"
    assert "negative_control_noise" in report.reason_codes


def test_validator_rejects_raw_pair_that_bypasses_candidate_hash_binding() -> None:
    with pytest.raises(TypeError, match="CandidateBundle"):
        IssueValidator().validate(_bundle().pair, FakeIndependentRunner())  # type: ignore[arg-type]


class MustNotRun:
    def run_session(self, *args, **kwargs):
        raise AssertionError("invalid candidate must be rejected before session launch")


def test_validator_rejects_tampered_candidate_before_session_launch() -> None:
    candidate = _bundle()
    tampered = replace(candidate, pair=replace(candidate.pair, parameters={"changed": True}))

    report = IssueValidator().validate(tampered, MustNotRun())

    assert report.stage.value == "invalid"
    assert "parameters_hash_mismatch" in report.reason_codes


@pytest.mark.parametrize(
    "missing_step",
    (
        "process_started",
        "adapter_created",
        "connection_opened",
        "graph_reset",
        "graph_loaded",
        "graph_verified",
        "connection_closed",
        "process_exited",
    ),
)
def test_validation_requires_every_session_lifecycle_step(missing_step: str) -> None:
    class IncompleteLifecycleRunner(FakeIndependentRunner):
        def run_session(self, *args, **kwargs):
            equivalence, session = super().run_session(*args, **kwargs)
            lifecycle = replace(session.lifecycle, **{missing_step: False})
            return equivalence, replace(session, lifecycle=lifecycle)

    report = IssueValidator().validate(_bundle(), IncompleteLifecycleRunner())

    assert report.stage.value == "invalid"
    assert "session_lifecycle_incomplete" in report.reason_codes


def test_validation_requires_distinct_os_processes_and_driver_connections() -> None:
    class ReusedConnectionRunner(FakeIndependentRunner):
        def run_session(self, *args, **kwargs):
            equivalence, session = super().run_session(*args, **kwargs)
            return equivalence, replace(session, lifecycle=replace(session.lifecycle, connection_id="reused"))

    report = IssueValidator().validate(_bundle(), ReusedConnectionRunner())

    assert report.stage.value == "rejected"
    assert "sessions_not_independent" in report.reason_codes


def test_validation_sessions_must_match_discovery_slow_query_identity() -> None:
    report = IssueValidator().validate(_bundle(discovery_slow_query="a"), FakeIndependentRunner())

    assert report.stage.value == "validation_passed"
    assert all(session.evidence.slow_query == "a" for session in report.sessions)


def test_combined_gate_does_not_require_each_session_ci_to_pass_independently() -> None:
    class OneSessionBelowCiRunner(FakeIndependentRunner):
        def run_session(self, *args, **kwargs):
            equivalence, session = super().run_session(*args, **kwargs)
            if args[1] == 1:
                evidence = replace(
                    session.evidence,
                    verdict=RuntimeVerdict.FAIL,
                    ci_ratio_low=1.5,
                    reason_codes=("ci_lower_below_threshold",),
                )
                session = replace(session, evidence=evidence)
            return equivalence, session

    report = IssueValidator().validate(_bundle(), OneSessionBelowCiRunner())

    assert report.stage.value == "validation_passed"
