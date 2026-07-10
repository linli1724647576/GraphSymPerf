"""Full typed result comparison for ordered sequences or unordered multisets."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from graph_sym_perf.equivalence.canonical import canonical_row


@dataclass(frozen=True)
class TypedEquivalence:
    equivalent: bool
    ordered: bool
    reason_codes: tuple[str, ...]


def compare_typed_results(left: Iterable[Any], right: Iterable[Any], *, ordered: bool) -> TypedEquivalence:
    left_rows = tuple(canonical_row(row) for row in left)
    right_rows = tuple(canonical_row(row) for row in right)
    equivalent = left_rows == right_rows if ordered else Counter(left_rows) == Counter(right_rows)
    return TypedEquivalence(equivalent, ordered, () if equivalent else ("typed_result_mismatch",))
