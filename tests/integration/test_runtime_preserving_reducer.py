from __future__ import annotations

import hashlib
import os
import copy
from dataclasses import replace

import pytest

from graph_sym_perf.reducer.models import ReductionBundle, ReductionStatus, RuleWitness
from graph_sym_perf.reducer.reducer import CandidateReducer, evaluate_reduction_candidate
from graph_sym_perf.runtime.models import EquivalentQueryPair, PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    SafetyCertificate,
    SessionLifecycle,
    ValidationPolicy,
    ValidationSession,
)
from graph_sym_perf.validation.validator import IssueValidator


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pair() -> EquivalentQueryPair:
    snapshot = GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",))
    return EquivalentQueryPair(
        "MATCH (n:Person), (m:Unused) WHERE n.id = 1 AND true RETURN n.id ORDER BY n.id",
        "MATCH (n:Person), (m:Unused) WHERE true AND n.id = 1 RETURN n.id ORDER BY n.id",
        {},
        snapshot.content_hash,
        "dbms",
    )


def _certificate() -> SafetyCertificate:
    return SafetyCertificate("predicate_order_symmetry", "core", "v1", "where_boolean")


def _candidate(pair: EquivalentQueryPair | None = None) -> CandidateBundle:
    pair = pair or _pair()
    return CandidateBundle.create(
        pair,
        graph_snapshot=GraphSnapshot.from_statements(("CREATE (n:Person {id: 1})",)),
        dbms_version="dbms-version",
        resolved_config={"runtime": {"reducer_pairs": 5}, "volatile_functions": ()},
        discovery_slow_query="b",
        discovery_ratio=4.0,
        validation_policy=ValidationPolicy(),
        certificate=_certificate(),
    )


def _control() -> RuntimeEvidence:
    samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 10.0) for index in range(20))
    return RuntimeEvidence(RuntimeVerdict.PASS, "control", "control", samples, 1.0, 0.0, 1.0, 1.0, 0.5, 0.0, ())


class IndependentRunner:
    def run_session(self, candidate, session_number, *, expected_slow_query, noise_floor_ms, timer_resolution_ms):
        samples = tuple(PairedSample("AB" if index % 2 == 0 else "BA", 10.0, 40.0) for index in range(20))
        evidence = RuntimeEvidence(
            RuntimeVerdict.PASS,
            _hash(candidate.pair.query_a),
            _hash(candidate.pair.query_b),
            samples,
            4.0,
            30.0,
            4.0,
            4.0,
            1.0,
            0.0,
            (),
            slow_query="b",
            graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
            dbms_fingerprint=candidate.pair.dbms_fingerprint,
            measurement_source="ordinary_execute",
            measurement_protocol="ordinary-execute-20-pair-v1",
        )
        lifecycle = SessionLifecycle(True, True, True, True, True, True, True, True, f"connection-{session_number}")
        return EquivalenceEvidence(True, True, ()), ValidationSession(
            f"session-{session_number}",
            os.getpid() + session_number + 100,
            candidate.pair.graph_snapshot_hash,
            candidate.pair.dbms_fingerprint,
            evidence,
            (_control(), _control()),
            lifecycle=lifecycle,
        )


class CompleteEquivalenceGate:
    protocol_id = "typed-complete-test-v1"

    def evaluate(self, pair: EquivalentQueryPair) -> EquivalenceEvidence:
        equivalent = "RETURN n.id" in pair.query_a and "RETURN n.id" in pair.query_b
        return EquivalenceEvidence(equivalent, equivalent, () if equivalent else ("typed_result_mismatch",))


class OrdinaryFivePairScreen:
    protocol_id = "ordinary-execute-5-pair-test-v1"

    def __init__(self) -> None:
        self.evaluated: list[EquivalentQueryPair] = []

    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence:
        self.evaluated.append(pair)
        samples = tuple(PairedSample(order, 10.0, 40.0) for order in ("AB", "BA", "AB", "BA", "AB"))
        return RuntimeEvidence(
            RuntimeVerdict.PASS,
            _hash(pair.query_a),
            _hash(pair.query_b),
            samples,
            4.0,
            30.0,
            4.0,
            4.0,
            1.0,
            0.0,
            (),
            slow_query="b",
            graph_snapshot_hash=pair.graph_snapshot_hash,
            dbms_fingerprint=pair.dbms_fingerprint,
            measurement_source="ordinary_execute",
            measurement_protocol="ordinary-execute-5-pair-v1",
        )

    def profile(self, *_args, **_kwargs):
        raise AssertionError("reducer must never invoke PROFILE")


class ProfileOnlyScreen(OrdinaryFivePairScreen):
    protocol_id = "profile-only-invalid-v1"

    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence:
        self.evaluated.append(pair)
        samples = tuple(PairedSample(order, 10.0, 40.0) for order in ("AB", "BA", "AB", "BA", "AB"))
        return RuntimeEvidence(
            RuntimeVerdict.PASS,
            _hash(pair.query_a),
            _hash(pair.query_b),
            samples,
            100.0,
            1000.0,
            99.0,
            101.0,
            1.0,
            0.0,
            (),
            slow_query="b",
            graph_snapshot_hash=pair.graph_snapshot_hash,
            dbms_fingerprint=pair.dbms_fingerprint,
            measurement_source="profile",
            measurement_protocol="profile-5-pair-v1",
        )


class FinalIssueValidator:
    protocol_id = "independent-two-session-v1"

    def __init__(self, *, fail: bool = False) -> None:
        self.candidates: list[CandidateBundle] = []
        self.fail = fail

    def validate(self, candidate: CandidateBundle):
        self.candidates.append(candidate)
        report = IssueValidator().validate(candidate, IndependentRunner())
        if self.fail:
            object.__setattr__(report, "stage", type(report.stage).REJECTED)
            object.__setattr__(report, "reason_codes", ("ratio_below_threshold",))
        return report


def _bundle(*, screen=None, final=None) -> ReductionBundle:
    candidate = _candidate()
    original = IssueValidator().validate(candidate, IndependentRunner())
    witness = _witness()
    return ReductionBundle.create(
        original_candidate=candidate,
        original_validation=original,
        rule_witness=witness,
        equivalence_gate=CompleteEquivalenceGate(),
        runtime_screen=screen or OrdinaryFivePairScreen(),
        final_validator=final or FinalIssueValidator(),
    )


def _witness() -> RuleWitness:
    return RuleWitness.create(
        rule_id="predicate_order_symmetry",
        proof_version="v1",
        relation="conjunct_order",
        required_fragments_a=("n.id = 1 AND true",),
        required_fragments_b=("true AND n.id = 1",),
        required_variables=("n",),
        output_schema=("n.id",),
    )


def test_reduction_bundle_rejects_validation_whose_runtime_evidence_was_tampered() -> None:
    candidate = _candidate()
    original = IssueValidator().validate(candidate, IndependentRunner())
    tampered = copy.copy(original)
    first = original.sessions[0]
    object.__setattr__(
        tampered,
        "sessions",
        (replace(first, evidence=replace(first.evidence, query_a_hash="wrong")), original.sessions[1]),
    )

    with pytest.raises(ValueError, match="VALIDATION_PASSED"):
        ReductionBundle.create(
            original_candidate=candidate,
            original_validation=tampered,
            rule_witness=_witness(),
            equivalence_gate=CompleteEquivalenceGate(),
            runtime_screen=OrdinaryFivePairScreen(),
            final_validator=FinalIssueValidator(),
        )


def test_reduction_bundle_rejects_profile_sourced_original_validation() -> None:
    class ProfileSourcedRunner(IndependentRunner):
        def run_session(self, *args, **kwargs):
            equivalence, session = super().run_session(*args, **kwargs)
            return equivalence, replace(
                session,
                evidence=replace(
                    session.evidence,
                    measurement_source="profile",
                    measurement_protocol="profile-20-pair-v1",
                ),
            )

    candidate = _candidate()
    original = IssueValidator().validate(candidate, ProfileSourcedRunner())
    assert original.stage.value == "validation_passed"

    with pytest.raises(ValueError, match="ordinary execute"):
        ReductionBundle.create(
            original_candidate=candidate,
            original_validation=original,
            rule_witness=_witness(),
            equivalence_gate=CompleteEquivalenceGate(),
            runtime_screen=OrdinaryFivePairScreen(),
            final_validator=FinalIssueValidator(),
        )


def test_reducer_reaches_paired_one_minimal_then_runs_independent_validation(tmp_path) -> None:
    final = FinalIssueValidator()
    bundle = _bundle(final=final)
    checkpoint = tmp_path / "best-so-far.json"

    report = CandidateReducer(max_attempts=20, checkpoint_path=checkpoint).reduce(bundle)

    assert report.status is ReductionStatus.REDUCED_VALID
    assert len(report.reduced_pair.query_a) < len(report.original_pair.query_a)
    assert len(report.reduced_pair.query_b) < len(report.original_pair.query_b)
    assert any(step.layer == "pattern_atom" for step in report.accepted_steps)
    assert report.minimality_proof is not None and report.minimality_proof.verify(report.reduced_pair)
    assert report.final_validation is not None and report.final_validation.candidate_hash == report.reduced_candidate_hash
    assert final.candidates == [final.candidates[0]]
    assert final.candidates[0].pair == report.reduced_pair
    assert report.original_char_count > report.reduced_char_count
    assert report.original_ast_node_count > report.reduced_ast_node_count
    assert len(report.original_ast_hash) == len(report.reduced_ast_hash) == 64
    assert checkpoint.exists()


def test_profile_only_signal_is_rejected_when_ordinary_runtime_protocol_is_absent() -> None:
    report = CandidateReducer(max_attempts=20).reduce(_bundle(screen=ProfileOnlyScreen()))

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.accepted_steps == ()
    assert any("ordinary_runtime_protocol_invalid" in step.reason for step in report.rejected_steps)
    assert report.final_validation is None
    assert report.stop_reason == "final_runtime_inconclusive"


def test_dependency_output_and_witness_fail_before_equivalence_or_runtime_execution() -> None:
    screen = OrdinaryFivePairScreen()
    bundle = _bundle(screen=screen)
    unbound = replace(bundle.pair, query_a="MATCH (m) RETURN n.id", query_b="MATCH (m) RETURN n.id")
    witness_deleted = replace(
        bundle.pair,
        query_a="MATCH (n) WHERE n.id = 1 RETURN n.id",
        query_b="MATCH (n) WHERE n.id = 1 RETURN n.id",
    )
    output_changed = replace(
        bundle.pair,
        query_a=bundle.pair.query_a.replace("RETURN n.id", "RETURN count(*) AS rows"),
        query_b=bundle.pair.query_b.replace("RETURN n.id", "RETURN count(*) AS rows"),
    )

    decisions = tuple(evaluate_reduction_candidate(bundle, pair) for pair in (unbound, witness_deleted, output_changed))

    assert tuple(decision.reason for decision in decisions) == (
        "parse_type_dependency_failed",
        "rule_witness_not_preserved",
        "parse_type_dependency_failed",
    )
    assert screen.evaluated == []


def test_equivalence_gate_must_return_typed_complete_evidence_before_runtime() -> None:
    class LookalikeEquivalenceGate:
        protocol_id = "untyped-lookalike-v1"

        def evaluate(self, _pair):
            return type("Lookalike", (), {"equivalent": True, "complete_result": True})()

    screen = OrdinaryFivePairScreen()
    original = _bundle(screen=screen)
    bundle = replace(original, equivalence_gate=LookalikeEquivalenceGate())

    decision = evaluate_reduction_candidate(bundle, bundle.pair)

    assert decision.reason == "full_typed_equivalence_failed"
    assert screen.evaluated == []


def test_attempt_limit_after_progress_is_partial_and_cannot_run_final_validation(tmp_path) -> None:
    final = FinalIssueValidator()
    report = CandidateReducer(max_attempts=2, checkpoint_path=tmp_path / "partial.json").reduce(_bundle(final=final))

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.accepted_steps
    assert report.minimality_proof is None
    assert report.final_validation is None
    assert final.candidates == []
    assert report.stop_reason == "attempt_limit"


def test_infrastructure_error_cannot_be_relabelled_as_minimality_symptom_failure() -> None:
    class ProposalReloadFailure(OrdinaryFivePairScreen):
        protocol_id = "reload-failure-test-v1"

        def evaluate(self, pair):
            if "(m:Unused)" not in pair.query_a:
                raise RuntimeError("graph reload failed")
            return super().evaluate(pair)

    final = FinalIssueValidator()
    report = CandidateReducer(max_attempts=20).reduce(_bundle(screen=ProposalReloadFailure(), final=final))

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.minimality_proof is None
    assert report.final_validation is None
    assert final.candidates == []
    assert report.stop_reason == "minimality_inconclusive"


def test_unchanged_pair_cannot_be_labelled_reduced_valid_or_reach_final_validator() -> None:
    original_pair = _pair()
    pair = replace(
        original_pair,
        query_a=original_pair.query_a.replace(", (m:Unused)", ""),
        query_b=original_pair.query_b.replace(", (m:Unused)", ""),
    )
    candidate = _candidate(pair)
    original = IssueValidator().validate(candidate, IndependentRunner())
    final = FinalIssueValidator()
    bundle = ReductionBundle.create(
        original_candidate=candidate,
        original_validation=original,
        rule_witness=_witness(),
        equivalence_gate=CompleteEquivalenceGate(),
        runtime_screen=OrdinaryFivePairScreen(),
        final_validator=final,
    )

    report = CandidateReducer(max_attempts=20).reduce(bundle)

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.accepted_steps == ()
    assert report.final_validation is None
    assert final.candidates == []
    assert report.stop_reason == "no_reduction_accepted"


def test_failed_final_independent_validation_invalidates_reduction() -> None:
    final = FinalIssueValidator(fail=True)
    report = CandidateReducer(max_attempts=20).reduce(_bundle(final=final))

    assert report.status is ReductionStatus.REDUCTION_INVALIDATED
    assert report.minimality_proof is not None
    assert report.final_validation is not None
    assert report.reduced_candidate_hash == report.final_validation.candidate_hash


def test_explicit_final_typed_equivalence_mismatch_invalidates_reduction() -> None:
    class MismatchRunner(IndependentRunner):
        def run_session(self, candidate, session_number, **kwargs):
            _equivalence, session = super().run_session(candidate, session_number, **kwargs)
            return EquivalenceEvidence(False, True, ("typed_result_mismatch",)), session

    class MismatchFinal(FinalIssueValidator):
        def validate(self, candidate):
            self.candidates.append(candidate)
            return IssueValidator().validate(candidate, MismatchRunner())

    report = CandidateReducer(max_attempts=20).reduce(_bundle(final=MismatchFinal()))

    assert report.status is ReductionStatus.REDUCTION_INVALIDATED
    assert report.stop_reason == "final_validation_failed"


def test_final_validator_exception_is_partial_with_attempt_and_reason_evidence() -> None:
    class RaisingFinal(FinalIssueValidator):
        def validate(self, candidate):
            self.candidates.append(candidate)
            raise RuntimeError("validator worker crashed")

    report = CandidateReducer(max_attempts=20).reduce(_bundle(final=RaisingFinal()))

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.attempts
    assert tuple(attempt.index for attempt in report.attempts) == tuple(range(1, len(report.attempts) + 1))
    assert report.stop_reason == "final_validation_error:RuntimeError"
    assert report.final_validation is None


def test_final_evidence_is_recomputed_from_all_40_validation_pairs() -> None:
    class OneSessionThresholdFailRunner(IndependentRunner):
        def run_session(self, candidate, session_number, **kwargs):
            equivalence, session = super().run_session(candidate, session_number, **kwargs)
            if session_number == 1:
                session = replace(
                    session,
                    evidence=replace(
                        session.evidence,
                        verdict=RuntimeVerdict.FAIL,
                        ci_ratio_low=1.5,
                        reason_codes=("ci_lower_below_threshold",),
                    ),
                )
            return equivalence, session

    class CombinedFinal(FinalIssueValidator):
        def validate(self, candidate):
            self.candidates.append(candidate)
            return IssueValidator().validate(candidate, OneSessionThresholdFailRunner())

    report = CandidateReducer(max_attempts=20).reduce(_bundle(final=CombinedFinal()))

    assert report.status is ReductionStatus.REDUCED_VALID
    assert len(report.final_evidence.paired_samples) == 40
    assert report.final_evidence.verdict is RuntimeVerdict.PASS
    assert report.final_evidence.query_a_hash == _hash(report.reduced_pair.query_a)
    assert report.final_evidence.query_b_hash == _hash(report.reduced_pair.query_b)
    assert report.final_evidence.slow_query == "b"
    assert report.final_evidence.measurement_protocol == "ordinary-execute-40-pair-combined-v1"


def test_checkpoint_oserror_returns_partial_with_monotonic_best_and_attempt(tmp_path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")

    report = CandidateReducer(
        max_attempts=20,
        checkpoint_path=blocker / "best.json",
    ).reduce(_bundle())

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.reduced_pair != report.original_pair
    assert tuple(attempt.index for attempt in report.attempts) == tuple(range(1, len(report.attempts) + 1))
    assert report.attempts[-1].outcome == "symptom_preserved"
    assert report.stop_reason == "checkpoint_error:FileExistsError"


def test_final_validation_with_wrong_session_query_hash_is_partial() -> None:
    class WrongEvidenceFinal(FinalIssueValidator):
        def validate(self, candidate):
            report = super().validate(candidate)
            first = report.sessions[0]
            object.__setattr__(
                report,
                "sessions",
                (replace(first, evidence=replace(first.evidence, query_b_hash="wrong")), report.sessions[1]),
            )
            return report

    report = CandidateReducer(max_attempts=20).reduce(_bundle(final=WrongEvidenceFinal()))

    assert report.status is ReductionStatus.REDUCTION_PARTIAL
    assert report.stop_reason == "final_validation_inconclusive"
