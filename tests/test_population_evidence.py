"""Proof observations retain relationship identity without leaking interpreter objects."""

import json

import pytest

import ir
from population import _Interpreter, synthesize_relational_state
from state import GraphSpec
from symbolic import _population_contract


def named_relationship_query(operator):
    edge = ir.Edge(ir.Node("a", ("A",)), ir.Relationship(variable="r", type="T"), ir.Node("b", ("B",)))
    body = ir.Query(None, None, (ir.Alias(ir.Literal(1), "one"),))
    return ir.Query(edge, None, (ir.Alias(operator(body=body), "value"),))


@pytest.mark.parametrize("operator,expected", [(ir.Count, 1), (ir.Exists, True)])
def test_population_contract_with_named_relationship_and_subquery_is_json_safe(operator, expected):
    query = named_relationship_query(operator)
    populated = synthesize_relational_state(query, alternatives=(query,), scale=3, data_seed=0,
                                           min_result_rows=1)
    contract = _population_contract(populated)
    restored = json.loads(json.dumps(contract, allow_nan=False))
    assert restored["expected_rows"] == [{"value": expected}]
    outcomes = restored["evidence"]["subquery_outcomes"]
    assert outcomes and outcomes[0]["bindings"]["r"] == {"kind": "relationship", "index": 0}


@pytest.mark.parametrize("operator,expected", [(ir.Count, 1), (ir.Exists, True)])
def test_parallel_relationships_keep_distinct_identities_in_subquery_evidence(operator, expected):
    spec = GraphSpec("AST", 1, 0, version=2, include_reference=False,
                     node_groups=({"count": 1, "labels": ["A"], "properties": {"uid": 1}},
                                  {"count": 1, "labels": ["B"], "properties": {"uid": 2}}),
                     edge_groups=({"count": 2, "source": 1, "target": 2, "type": "T",
                                   "source_label": "A", "target_label": "B", "properties": {}},))
    interpreter = _Interpreter(spec)
    assert interpreter.run(named_relationship_query(operator)) == [{"value": expected}, {"value": expected}]
    observations = json.loads(json.dumps(interpreter.subquery_outcomes, allow_nan=False))
    assert [item["bindings"]["r"] for item in observations] == [
        {"kind": "relationship", "index": 0}, {"kind": "relationship", "index": 1}]
    assert all(item["bindings"]["a"] == 1 and item["bindings"]["b"] == 2 for item in observations)
