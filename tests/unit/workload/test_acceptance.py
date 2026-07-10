from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import AcceptedPairRequest


def test_accepted_builder_tops_up_until_exact_pair_budget() -> None:
    builder = AcceptedPairBuilder(lambda seed: ("Q", "T") if seed % 2 else None)
    corpus = builder.build(AcceptedPairRequest(accepted_pairs=3, max_generated_seeds=10))
    assert corpus.accepted_count == 3
    assert corpus.generated_seeds == 5
