"""Actual scan domains keep Boolean and numeric property populations apart."""

import pytest

import ir
from population import MatchDemand, PopulationError, evaluate_query, synthesize_relational_state
from state import GraphSpec, render_state_checks


@pytest.mark.parametrize("boolean,number", [(False, 0), (True, 1)])
def test_mixed_boolean_and_numeric_scan_domains_have_distinct_materialization_checks(boolean, number):
    spec = GraphSpec("AST", 7, 0, version=2, include_reference=False, node_groups=(
        {"count": 2, "labels": ["Same"], "properties": {"uid": {"affine": {"base": 1, "step": 1}}, "flag": boolean}},
        {"count": 3, "labels": ["Same"], "properties": {"uid": {"affine": {"base": 3, "step": 1}}, "flag": number}},
    ))
    checks = [check for check in spec.state_checks()
              if check["kind"] == "node_count" and "flag" in check.get("properties", {})]
    assert len(checks) == 2
    assert next(check["expected"] for check in checks if type(check["properties"]["flag"]) is bool) == 2
    assert next(check["expected"] for check in checks if type(check["properties"]["flag"]) is int) == 3
    for check in checks:
        query = ir.Query(ir.Node("n", tuple(check["labels"]), tuple(
            (key, ir.Literal(value)) for key, value in check["properties"].items())), None, (ir.Literal(1),))
        assert len(evaluate_query(query, spec)) == check["expected"]
    assert len({check["query"] for check in render_state_checks(checks)}) == 2


@pytest.mark.parametrize("left,right,expected", [(False, 0, False), (True, 1, False),
                                                 (0, 0.0, True), (1, 1.0, True)])
def test_predicate_equality_uses_database_numeric_coercion_without_boolean_coercion(left, right, expected):
    spec = GraphSpec("AST", 1, 0, version=2, include_reference=False,
                     node_groups=({"count": 1, "labels": ["Root"], "properties": {"uid": 1}},))
    query = ir.Query(None, None, (ir.Alias(ir.Comparison(ir.Literal(left), "=", ir.Literal(right)), "equal"),))
    assert evaluate_query(query, spec) == [{"equal": expected}]


def test_numeric_equal_scan_predicates_cannot_claim_disjoint_exact_match_populations():
    first, second = ir.Node("a", ("Same",)), ir.Node("b", ("Same",))
    query = ir.Query(first, ir.Comparison(ir.Property("a", "flag"), "=", ir.Literal(0)),
                     (ir.Property("a", "uid"), ir.Property("b", "uid")), matches=(
        ir.Match(second, ir.Comparison(ir.Property("b", "flag"), "=", ir.Literal(0.0))),))
    with pytest.raises(PopulationError, match="quantity|matching|satisf"):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(
            MatchDemand(("query", "pattern"), "first", exact_matches=2),
            MatchDemand(("query", "matches", 0, "pattern"), "second", exact_matches=3)))
