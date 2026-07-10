from graph_sym_perf.transforms.symmetry.rules import reorder_conjuncts


def test_predicate_reordering_preserves_conjunct_multiset() -> None:
    assert reorder_conjuncts(("a", "b", "c")) == ("c", "b", "a")
