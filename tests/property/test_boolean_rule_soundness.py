import itertools

import pytest

from graph_sym_perf.transforms.boolean_equivalence.proofs import (
    de_morgan_and,
    de_morgan_or,
    double_negation,
)
from graph_sym_perf.transforms.boolean_equivalence.rules import (
    constant_false_conjunction,
    constant_true_disjunction,
    null_complete_excluded_middle,
    null_contradiction,
    null_partition_tautology,
)


def test_null_complete_excluded_middle_is_true_for_three_valued_inputs() -> None:
    assert all(null_complete_excluded_middle(value) for value in (True, False, None))


@pytest.mark.parametrize("value", (True, False, None))
def test_all_core_boolean_identities_preserve_three_valued_truth(value: bool | None) -> None:
    assert constant_true_disjunction(value) is True
    assert constant_false_conjunction(value) is False
    assert null_partition_tautology(value) is True
    assert null_contradiction(value) is False
    assert null_complete_excluded_middle(value) is True


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
