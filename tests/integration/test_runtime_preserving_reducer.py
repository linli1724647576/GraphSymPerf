from graph_sym_perf.reducer.reducer import CandidateReducer
from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import EquivalentQueryPair


class Predicate:
    def evaluate(self, pair):
        valid = "slow_marker" in pair.query_b and "RETURN" in pair.query_a and "RETURN" in pair.query_b
        return RuntimeEvidence(RuntimeVerdict.PASS if valid else RuntimeVerdict.FAIL, "a", "b", (), 4.0 if valid else 1.0, 20.0, 3.0, 5.0, 1.0, 0.0, ())


def test_reducer_returns_reduced_valid_pair_only_after_runtime_predicate() -> None:
    pair = EquivalentQueryPair("MATCH (n) WHERE true RETURN n", "MATCH (n) WHERE slow_marker = true AND true RETURN n", {}, "g", "d")
    report = CandidateReducer(Predicate(), max_attempts=20).reduce(pair)
    assert report.status.value == "reduced_valid"
    assert len(report.reduced_pair.query_a) + len(report.reduced_pair.query_b) < len(pair.query_a) + len(pair.query_b)
