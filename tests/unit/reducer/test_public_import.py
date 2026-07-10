from graph_sym_perf.reducer import CandidateReducer
from graph_sym_perf.reducer.reducer import CandidateReducer as ConcreteCandidateReducer


def test_public_reducer_import_resolves_to_runtime_preserving_reducer() -> None:
    assert CandidateReducer is ConcreteCandidateReducer
