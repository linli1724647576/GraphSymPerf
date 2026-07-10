from graph_sym_perf.transforms.boolean_equivalence.proofs import null_complete_excluded_middle


def test_null_complete_excluded_middle_is_true_for_three_valued_inputs() -> None:
    assert all(null_complete_excluded_middle(value) for value in (True, False, None))
