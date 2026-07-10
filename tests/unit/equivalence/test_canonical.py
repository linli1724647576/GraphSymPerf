from graph_sym_perf.equivalence.checker import compare_typed_results


def test_unordered_results_use_a_typed_multiset_with_duplicate_counts() -> None:
    equal = compare_typed_results(((1,), (1,), (None,)), ((None,), (1,), (1,)), ordered=False)
    unequal_type = compare_typed_results(((1,),), ((1.0,),), ordered=False)
    unequal_count = compare_typed_results(((1,), (1,)), ((1,),), ordered=False)

    assert equal.equivalent
    assert not unequal_type.equivalent
    assert not unequal_count.equivalent


def test_ordered_results_preserve_sequence_and_nested_values() -> None:
    equal = compare_typed_results((({"x": [None, ""]},),), (({"x": [None, ""]},),), ordered=True)
    reordered = compare_typed_results(((1,), (2,)), ((2,), (1,)), ordered=True)

    assert equal.equivalent
    assert not reordered.equivalent
