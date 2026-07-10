"""Full typed result comparison for ordered sequences or unordered multisets."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from graph_sym_perf.core import ExecutionResult
from graph_sym_perf.equivalence.canonical import NonFiniteResultValue, UnsupportedTypedResultValue, canonical_row


@dataclass(frozen=True)
class TypedEquivalence:
    equivalent: bool
    ordered: bool
    reason_codes: tuple[str, ...]
    complete_result: bool = True


def compare_typed_results(left: Iterable[Any], right: Iterable[Any], *, ordered: bool) -> TypedEquivalence:
    try:
        left_rows = tuple(canonical_row(row) for row in left)
        right_rows = tuple(canonical_row(row) for row in right)
    except NonFiniteResultValue:
        return TypedEquivalence(False, ordered, ("non_finite_result_value",), False)
    except UnsupportedTypedResultValue:
        return TypedEquivalence(False, ordered, ("unsupported_typed_result_value",), False)
    equivalent = left_rows == right_rows if ordered else Counter(left_rows) == Counter(right_rows)
    return TypedEquivalence(equivalent, ordered, () if equivalent else ("typed_result_mismatch",))


def compare_execution_results(
    left: ExecutionResult,
    right: ExecutionResult,
    *,
    ordered: bool,
    max_result_rows: int = 100_000,
) -> TypedEquivalence:
    if bool(left.summary.get("truncated")) or bool(right.summary.get("truncated")):
        return TypedEquivalence(False, ordered, ("result_truncated",), False)
    if len(left.rows) > max_result_rows or len(right.rows) > max_result_rows:
        return TypedEquivalence(False, ordered, ("result_too_large",), False)
    return compare_typed_results(left.rows, right.rows, ordered=ordered)
