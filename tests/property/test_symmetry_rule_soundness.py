import pytest

from graph_sym_perf.core import QueryCase, TransformConfig
from graph_sym_perf.transforms.symmetry import generate_symmetry_family
from graph_sym_perf.transforms.symmetry.rules import reorder_conjuncts


def _case(query: str) -> QueryCase:
    return QueryCase("case_1", "gdsmith", query, {}, "", "", source="gdsmith")


def test_predicate_reordering_preserves_conjunct_multiset() -> None:
    assert reorder_conjuncts(("a", "b", "c")) == ("c", "b", "a")


@pytest.mark.parametrize(
    "query",
    (
        "OPTIONAL MATCH (a:L0), (b:L1) RETURN a, b",
        "MATCH (a:L0) WITH a MATCH (b:L1) RETURN a, b",
        "MATCH (a:L0) MATCH (b:L1) RETURN a, b",
    ),
)
def test_core_pattern_order_does_not_cross_scope_boundaries(query: str) -> None:
    family = generate_symmetry_family(
        _case(query), TransformConfig(enabled_rules=("pattern_order_symmetry",))
    )
    assert not family.variants
