import os
from dataclasses import replace

import pytest

from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalentQueryPair,
    EquivalenceEvidence,
    SessionLifecycle,
    ValidationPolicy,
    ValidationSession,
)
from graph_sym_perf.validation.validator import IssueValidator


def _bundle(*, discovery_slow_query: str = "b", noise_floor_ms: float = 0.0) -> CandidateBundle:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    pair = EquivalentQueryPair(
        "MATCH (n) RETURN n ORDER BY n.id",
        "MATCH (n) RETURN n ORDER BY n.id",
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
            "a",
            "b",
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
        controls = (
            RuntimeEvidence(RuntimeVerdict.PASS, "a", "a", control_samples, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0, ()),
            RuntimeEvidence(RuntimeVerdict.PASS, "b", "b", control_samples, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0, ()),
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


def test_two_fresh_sessions_are_required_for_validation_passed() -> None:
    report = IssueValidator().validate(_bundle(), FakeIndependentRunner())
    assert report.stage.value == "validation_passed"
    assert report.validated_ratio == 4.0


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

    assert report.stage.value == "invalid"
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
