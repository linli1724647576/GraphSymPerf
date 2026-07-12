from decimal import Decimal

from graph_sym_perf.core import ExecutionResult, TargetSystem, to_typed_value
from graph_sym_perf.equivalence.checker import compare_execution_results, compare_typed_results


def _result(rows, *, truncated: bool = False) -> ExecutionResult:
    return ExecutionResult(TargetSystem.NEO4J, "equivalence", tuple(rows), 1.0, {"truncated": truncated})


def test_execution_equivalence_rejects_driver_truncation() -> None:
    evidence = compare_execution_results(_result(({"x": 1},), truncated=True), _result(({"x": 1},)), ordered=False)

    assert not evidence.equivalent
    assert not evidence.complete_result
    assert evidence.reason_codes == ("result_truncated",)


def test_execution_equivalence_rejects_more_than_one_hundred_thousand_rows() -> None:
    rows = ({"x": index} for index in range(100_001))
    evidence = compare_execution_results(_result(rows), _result(({"x": 1},)), ordered=False)

    assert not evidence.equivalent
    assert not evidence.complete_result
    assert evidence.reason_codes == ("result_too_large",)


def test_non_finite_result_values_never_compare_as_complete_equivalence() -> None:
    evidence = compare_typed_results(({"x": float("inf")},), ({"x": float("inf")},), ordered=False)

    assert not evidence.equivalent
    assert not evidence.complete_result
    assert evidence.reason_codes == ("non_finite_result_value",)


def test_complete_typed_comparison_still_checks_all_duplicate_rows() -> None:
    left = _result(({"x": 1}, {"x": 1}, {"x": 2}))
    right = _result(({"x": 1}, {"x": 2}, {"x": 2}))

    evidence = compare_execution_results(left, right, ordered=False)

    assert not evidence.equivalent
    assert evidence.complete_result
    assert evidence.reason_codes == ("typed_result_mismatch",)


def test_unknown_result_types_fail_closed_instead_of_comparing_repr() -> None:
    class Unknown:
        def __repr__(self) -> str:
            return "same"

    evidence = compare_typed_results(((Unknown(),),), ((Unknown(),),), ordered=False)

    assert not evidence.equivalent
    assert not evidence.complete_result
    assert evidence.reason_codes == ("unsupported_typed_result_value",)


def test_decimal_values_compare_exactly_without_float_coercion() -> None:
    equivalent = compare_typed_results(
        ({"x": to_typed_value(Decimal("1.00"))},),
        ({"x": to_typed_value(Decimal("1.0"))},),
        ordered=False,
    )
    different = compare_typed_results(
        ({"x": to_typed_value(Decimal("1.0000000000000000001"))},),
        ({"x": to_typed_value(Decimal("1.0000000000000000002"))},),
        ordered=False,
    )

    assert equivalent.equivalent and equivalent.complete_result
    assert not different.equivalent and different.complete_result
