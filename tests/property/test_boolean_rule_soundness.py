import itertools

from graph_sym_perf.transforms.boolean_equivalence.proofs import (
    constant_false_conjunction,
    constant_true_disjunction,
    de_morgan_and,
    de_morgan_or,
    double_negation,
    null_complete_excluded_middle,
    null_contradiction,
    null_partition_tautology,
)


def test_null_complete_excluded_middle_is_true_for_three_valued_inputs() -> None:
    assert all(null_complete_excluded_middle(value) for value in (True, False, None))


def test_all_boolean_rules_have_exhaustive_three_valued_truth_tables() -> None:
    values = (True, False, None)
    assert all(constant_true_disjunction(value) is True for value in values)
    assert all(constant_false_conjunction(value) is False for value in values)
    assert all(null_partition_tautology(value) is True for value in values)
    assert all(null_contradiction(value) is False for value in values)
    assert all(double_negation(value) is value for value in values)
    for left, right in itertools.product(values, repeat=2):
        assert de_morgan_or(left, right)
        assert de_morgan_and(left, right)
