from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import EquivalentQueryPair, EquivalenceEvidence, ValidationSession
from graph_sym_perf.validation.validator import IssueValidator


class FakeIndependentRunner:
    def run_session(self, pair, session_number):
        evidence = RuntimeEvidence(RuntimeVerdict.PASS, "a", "b", (), 4.0, 40.0, 3.0, 5.0, 1.0, 0.0, ())
        return EquivalenceEvidence(True, True, ()), ValidationSession(
            f"session-{session_number}", session_number, pair.graph_snapshot_hash, pair.dbms_fingerprint, evidence, ()
        )


def test_two_fresh_sessions_are_required_for_validation_passed() -> None:
    pair = EquivalentQueryPair("MATCH (n) RETURN n ORDER BY n", "MATCH (n) RETURN n ORDER BY n", {}, "graph", "dbms")
    report = IssueValidator().validate(pair, FakeIndependentRunner(), discovery_ratio=5.0)
    assert report.stage.value == "validation_passed"
    assert report.validated_ratio == 4.0


def test_validation_rejects_a_ratio_that_is_within_aa_noise_floor() -> None:
    pair = EquivalentQueryPair("MATCH (n) RETURN n ORDER BY n", "MATCH (n) RETURN n ORDER BY n", {}, "graph", "dbms")

    report = IssueValidator().validate(pair, FakeIndependentRunner(), discovery_ratio=5.0, noise_floor_ms=40.0)

    assert report.stage.value == "rejected"
    assert "absolute_delta_within_noise" in report.reason_codes
