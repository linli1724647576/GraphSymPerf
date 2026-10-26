"""EXISTS growth is masked only by an independently preserved row witness."""

from dataclasses import replace

import pytest

import ir
from population import MatchDemand, PopulationError, evaluate_query, synthesize_relational_state
from query import parse_query
from state import GraphSpec


def case(*, witness_quantity=1, witness_label="Witness", witness_limit=None, partial_witness=False):
    root = ir.Node("root", ("Root",))

    def branch(kind, label):
        pattern = ir.Edge(ir.Node("root"), ir.Relationship(type=kind), ir.Node("item", (label,)))
        return ir.Query(pattern, None, (ir.Alias(ir.Literal(1), "row"),))

    witness = replace(branch("WITNESS", witness_label), limit=witness_limit)
    if partial_witness:
        witness = replace(witness, where=ir.Comparison(ir.Property("root", "uid"), "=", ir.Literal(1)))
    low, high = branch("LOW", "Payload"), branch("HIGH", "Payload")

    def outer(body):
        return ir.Query(root, None, (ir.Alias(ir.Property("root", "uid"), "id"),
                                    ir.Alias(ir.Exists(body=body), "exists")))

    source, target = outer(ir.UnionAll((witness, low))), outer(ir.UnionAll((witness, high)))
    path = ("returns", 1, "expression", "body", "branches")
    demands = [MatchDemand(("query", "pattern"), "roots", exact_matches=2),
               MatchDemand(("alternatives", 0, "pattern"), "roots", exact_matches=2),
               MatchDemand(("query", *path, 1, "pattern"), "low", exact_matches=1),
               MatchDemand(("alternatives", 0, *path, 1, "pattern"), "high", scale_matches=True)]
    if witness_quantity is not None:
        demands.extend(MatchDemand((*prefix, *path, 0, "pattern"), "stable-witness", exact_matches=witness_quantity)
                       for prefix in (("query",), ("alternatives", 0)))
    return source, target, tuple(demands)


@pytest.mark.parametrize("scale", [7, 10000])
def test_positive_witness_for_each_input_certifies_both_query_versions(scale):
    source, target, demands = case()
    populated = synthesize_relational_state(source, alternatives=(target,), demands=demands,
                                            scale=scale, data_seed=3, min_result_rows=1)
    assert populated.expected_rows == [{"id": 1, "exists": True}, {"id": 2, "exists": True}]
    proof = GraphSpec.from_dict(populated.evidence["validation_graph_spec"])
    for tree in (source, target):
        assert evaluate_query(tree, proof) == populated.expected_rows
    masks = [observation["mask"] for certificate in populated.evidence["growth_certificates"]
             for observation in certificate["observations"] if observation.get("mask")]
    assert masks
    assert all(mask["kind"] == "exists_preserved_union_witness" for mask in masks)
    assert {tuple(mask["address"][:2]) for mask in masks} >= {("query", "returns"), ("alternatives", 0)}
    assert all(mask["minimum_rows"] == 1 and mask["physical_early_termination_guaranteed"] is False for mask in masks)
    # These checks close the actual root input domain and must remain in the
    # independently executed, loaded-state validation set.
    required = populated.evidence["required_matching_checks"]
    assert required and all(check in populated.checks for check in required)
    assert any("WITNESS" in check["query"] for check in required)
    for check in required:
        if "HIGH" not in check["query"]:
            assert evaluate_query(parse_query(check["query"]), proof) == check["expected_rows"]
        if scale <= 5000:
            assert evaluate_query(parse_query(check["query"]), populated.spec) == check["expected_rows"]
    if scale <= 5000:
        for tree in (source, target):
            assert evaluate_query(tree, populated.spec) == populated.expected_rows
    else:
        assert populated.spec.counts()["nodes"] > 5000
        assert any(item["proof"] == "actual_prefix_rows_and_growth_stable_filters"
                   for item in populated.evidence["row_observation_certificates"])


@pytest.mark.parametrize("options", [{"witness_quantity": None}, {"witness_quantity": 0},
                                      {"witness_limit": 0}, {"witness_label": "Payload"},
                                      {"partial_witness": True}])
def test_unproved_zero_limited_shared_or_partial_witness_cannot_mask_growth(options):
    source, target, demands = case(**options)
    with pytest.raises(PopulationError):
        synthesize_relational_state(source, alternatives=(target,), demands=demands,
                                    scale=10000, data_seed=3, min_result_rows=1)


def test_a_plain_exists_consumer_does_not_automatically_mask_its_growing_input():
    rows = ir.Query(ir.Edge(ir.Node("root", ("Root",)), ir.Relationship(type="HIGH"),
                           ir.Node("item", ("Payload",))), None, (ir.Literal(1),))
    source = ir.Query(None, None, (ir.Alias(ir.Exists(body=rows), "exists"),))
    demand = MatchDemand(("query", "returns", 0, "expression", "body", "pattern"),
                         "growth", scale_matches=True)
    with pytest.raises(PopulationError, match="masking certificate"):
        synthesize_relational_state(source, demands=(demand,), scale=10000, data_seed=3)
