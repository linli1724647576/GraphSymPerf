"""Semantic population obligations, including nested counts and read closure."""
from dataclasses import replace
import json

import pytest

from ir import (Alias, All, Boolean, Call, Case, Comparison, Count, Edge, Exists, Literal,
                Match, Negation, Node, Property, Query, Relationship, Variable, With)
from population import (PopulationError, _build_model, evaluate_query,
                        render_setup, synthesize_state)
from state import GraphSpec


def kernel(*, predicate=None, bounds=(1, 3)):
    path = Edge(Node("u", ("Root",), (("id", Literal(1)),)),
                Relationship(type="LINK", min_hops=bounds[0], max_hops=bounds[1]),
                Node("v", ("Leaf",)), "undirected", "p")
    predicate = predicate or Comparison(Property("x", "keep"), "=", Literal(True))
    where = All("x", Call("nodes", (Variable("p"),)), predicate)
    return Query(path, where, (Property("u", "uid"), Property("v", "uid")))


def outer(expression):
    return Query(Node("u", ("Root",), (("id", Literal(1)),)), expression,
                 (Property("u", "uid"),))


@pytest.mark.parametrize("threshold", [0, 2, 5])
def test_count_threshold_reaches_population_and_full_query(threshold):
    query = outer(Comparison(Count(body=kernel()), ">", Literal(threshold)))
    result = synthesize_state(query, scale=7, data_seed=4)
    assert result.constraints["valid_paths"] == threshold + 1
    assert evaluate_query(query, result.spec) == result.expected_rows == [{"u.uid": 1}]
    assert result.spec.indexes[0]["name"] == "gsp_cost_lookup"


def test_exhausting_search_budget_does_not_claim_global_unsatisfiability():
    query = outer(Comparison(Count(body=kernel()), ">", Literal(5)))
    with pytest.raises(PopulationError, match="budget exhausted.*unknown"):
        synthesize_state(query, scale=4, data_seed=0, max_models=1)


def test_integer_property_candidates_stay_in_the_ir_integer_domain():
    predicate = Comparison(Property("x", "value"), "=", Literal(2**63 - 1))
    result = synthesize_state(outer(Exists(body=kernel(predicate=predicate))), scale=4, data_seed=0)
    assert result.constraints["true_properties"]["value"] == 2**63 - 1
    assert result.constraints["false_properties"]["value"] == 2**63 - 2
    assert all(-(2**63) <= value < 2**63
               for node in result.spec.iter_nodes() for value in node["properties"].values()
               if type(value) is int)


def test_integer_node_predicate_and_large_anchor_id_are_not_count_thresholds():
    predicate = Comparison(Property("x", "flag"), "=", Literal(7))
    body = kernel(predicate=predicate, bounds=(3, 4))
    body = replace(body, pattern=replace(body.pattern, left=replace(body.pattern.left, properties=())))
    query = Query(Node("u", ("Root",)), Boolean("AND", (
        Comparison(Property("u", "id"), "=", Literal(895440)),
        Comparison(Count(body=body), ">", Literal(2)),
    )), (Property("u", "uid"),))
    result = synthesize_state(query, scale=8, data_seed=0)
    assert result.constraints["valid_paths"] == 3
    assert result.constraints["valid_length"] == 3
    assert result.constraints["root_properties"]["id"] == 895440
    assert result.constraints["true_properties"]["flag"] == 7
    assert result.constraints["false_properties"]["flag"] != 7
    assert evaluate_query(query, result.spec) == result.expected_rows


def test_with_alias_filter_is_a_real_cardinality_requirement():
    query = Query(None, None, (Property("u", "uid"),), pipeline=(
        Match(Node("u", ("Root",), (("id", Literal(1)),))),
        With((Variable("u"), Alias(Count(body=kernel()), "n")),
             Comparison(Variable("n"), ">", Literal(2))),
    ))
    result = synthesize_state(query, scale=4, data_seed=0)
    assert result.constraints["valid_paths"] == 3
    assert evaluate_query(query, result.spec) == [{"u.uid": 1}]


def test_shadowed_alias_does_not_reuse_a_previous_count_dependency():
    query = Query(None, None, (Property("u", "uid"),), pipeline=(
        Match(Node("u", ("Root",), (("id", Literal(1)),))),
        With((Variable("u"), Alias(Count(body=kernel()), "n"))),
        With((Variable("u"), Alias(Literal(895440), "n")),
             Comparison(Variable("n"), "=", Literal(895440))),
    ))
    result = synthesize_state(query, scale=4, data_seed=0)
    assert result.constraints["count_search_domain"][1] < 64
    assert evaluate_query(query, result.spec) == [{"u.uid": 1}]


def test_nested_count_counts_rows_not_the_inner_scalar_value():
    scalar = Query(None, None, (Alias(Count(body=kernel()), "n"),))
    impossible = outer(Comparison(Count(body=scalar), ">", Literal(2)))
    with pytest.raises(PopulationError, match="No compatible"):
        synthesize_state(impossible, scale=3, data_seed=0)
    possible = outer(Comparison(Count(body=scalar), "=", Literal(1)))
    result = synthesize_state(possible, scale=4, data_seed=0)
    assert result.constraints["valid_paths"] == 0
    assert evaluate_query(possible, result.spec) == [{"u.uid": 1}]


def test_renamed_negated_boolean_is_solved_not_assigned_false_by_recipe():
    predicate = Negation(Comparison(Property("x", "disabled"), "=", Literal(True)))
    query = outer(Exists(body=kernel(predicate=predicate)))
    result = synthesize_state(query, scale=8, data_seed=0)
    assert result.constraints["true_properties"] == {"disabled": False}
    assert result.constraints["false_properties"] == {"disabled": True}
    assert evaluate_query(query, result.spec) == result.expected_rows


def test_fixed_anchor_truth_conflict_is_not_hidden_by_creating_another_anchor():
    query = outer(Boolean("AND", (
        Comparison(Property("u", "keep"), "=", Literal(False)),
        Comparison(Count(body=kernel()), ">", Literal(0)),
    )))
    with pytest.raises(PopulationError, match="No compatible"):
        synthesize_state(query, scale=4, data_seed=0)


def test_empty_child_with_false_anchor_selects_failure_at_depth_zero():
    query = outer(Boolean("AND", (
        Comparison(Property("u", "keep"), "=", Literal(False)),
        Comparison(Count(body=kernel()), "=", Literal(0)),
    )))
    result = synthesize_state(query, scale=7, data_seed=0)
    assert result.constraints["failure_depth"] == 0
    assert result.constraints["valid_paths"] == 0
    assert evaluate_query(query, result.spec) == [{"u.uid": 1}]


def test_equivalent_endpoint_reversal_preserves_full_concrete_bag():
    query = kernel()
    result = synthesize_state(query, scale=9, data_seed=2)
    reversed_query = replace(query, pattern=replace(query.pattern, left=query.pattern.right,
                                                   right=query.pattern.left))
    assert evaluate_query(reversed_query, result.spec) == result.expected_rows


def test_large_state_is_bounded_and_roundtrips_without_a_pattern_recipe():
    query = outer(Exists(body=kernel()))
    result = synthesize_state(query, scale=1_000_000, data_seed=0)
    encoded = json.dumps(result.spec.to_dict())
    assert len(encoded) < 6000
    restored = GraphSpec.from_dict(json.loads(encoded))
    assert restored.to_dict() == result.spec.to_dict()
    assert restored.counts()["nodes"] > 1_000_000
    assert len(next(restored.iter_batches("nodes", 5))) == 5
    assert "range(0, 999999)" in "\n".join(render_setup(restored))
    assert "GspBad" not in encoded


def test_v1_recipe_dictionary_remains_unchanged():
    original = {"version": 1, "pattern": "P3", "scale": 4, "data_seed": 0,
                "state_variant": "pruning_path", "include_reference": True}
    assert GraphSpec.from_dict(original).to_dict() == original


def test_identical_second_lexical_path_cannot_read_the_expanding_region():
    good = kernel()
    bad = replace(good, where=None)
    query = Query(Node("u", ("Root",), (("id", Literal(1)),)), None,
                  (Alias(Count(body=good), "good"),
                   Alias(Comparison(Count(body=bad), ">", Literal(4)), "many")))
    with pytest.raises(PopulationError, match="one lexical"):
        synthesize_state(query, scale=10, data_seed=0)


def test_unlabelled_scan_cannot_observe_new_nodes_at_a_later_threshold():
    query = outer(Boolean("AND", (
        Exists(body=kernel()),
        Comparison(Count(Node("z")), ">", Literal(4)),
    )))
    with pytest.raises(PopulationError, match="Unlabelled node scans"):
        synthesize_state(query, scale=20, data_seed=0)


def test_labeled_endpoint_scan_is_outside_the_closed_anchor_domain():
    query = outer(Boolean("AND", (
        Exists(body=kernel()),
        Comparison(Count(Node("z", ("Leaf",))), ">", Literal(4)),
    )))
    with pytest.raises(PopulationError, match="disjoint"):
        synthesize_state(query, scale=20, data_seed=0)


def test_identical_endpoint_node_in_another_scope_is_not_exempt_from_read_closure():
    body = kernel()
    query = outer(Boolean("AND", (
        Exists(body=body),
        Comparison(Count(body.pattern.right), ">", Literal(4)),
    )))
    with pytest.raises(PopulationError, match="disjoint"):
        synthesize_state(query, scale=20, data_seed=0)


def test_bare_uncorrelated_path_start_cannot_bypass_false_entry():
    body = kernel()
    body = replace(body, pattern=replace(body.pattern, left=Node("u")))
    query = Query(Node("anchor", ("Root",), (("id", Literal(1)),)), None,
                  (Alias(Comparison(Count(body=body), ">", Literal(4)), "many"),))
    with pytest.raises(PopulationError, match="bare path start"):
        synthesize_state(query, scale=10, data_seed=0)


def test_bare_correlated_path_start_uses_the_lexical_anchor_definition():
    body = kernel()
    body = replace(body, pattern=replace(body.pattern, left=Node("u")))
    query = outer(Comparison(Count(body=body), ">", Literal(2)))
    result = synthesize_state(query, scale=10, data_seed=0)
    assert result.constraints["valid_paths"] == 3
    assert "root/pattern/variable" == result.evidence["anchor_definition"]
    assert evaluate_query(query, result.spec) == [{"u.uid": 1}]


def test_distinct_preserves_boolean_and_integer_types():
    body = kernel()
    value = Case(Comparison(Property("v", "uid"), "=", Literal(11)), Literal(True), Literal(1))
    query = replace(body, returns=(Alias(value, "value"),), distinct=True)
    result = synthesize_state(query, scale=3, data_seed=0, result_rows=2)
    assert result.constraints["valid_paths"] == 2
    assert {type(row["value"]) for row in evaluate_query(query, result.spec)} == {bool, int}
    with_query = Query(None, None, (Variable("value"),), pipeline=(
        Match(body.pattern, body.where), With((Alias(value, "value"),), distinct=True),
    ))
    assert len(evaluate_query(with_query, result.spec)) == 2


def test_all_must_guard_the_path_as_a_mandatory_positive_condition():
    query = kernel()
    query = replace(query, where=Boolean("OR", (query.where, Literal(True))))
    with pytest.raises(PopulationError, match="mandatory conjunct"):
        synthesize_state(query, scale=10, data_seed=0)


def test_shared_and_independent_topologies_are_real_distinct_models():
    query = kernel(bounds=(2, 3))
    args = (query.pattern, query.where.predicate, [query.pattern.left],
            {"uid": 1, "id": 1, "keep": True}, {"keep": True}, {"keep": False},
            1, 3, 2, 3, 0)
    shared = _build_model(*args, failure_depth=1, sharing="shared")
    independent = _build_model(*args, failure_depth=1, sharing="independent")
    assert shared.counts()["nodes"] < independent.counts()["nodes"]
    assert evaluate_query(query, shared) == evaluate_query(query, independent)
    result = synthesize_state(outer(Exists(body=query)), scale=10, data_seed=0, prefix_budget=1)
    assert result.evidence["selected_sharing"] == "shared"
    assert any(item["sharing"] == "independent" for item in result.evidence["rejected_structures"])


def test_unsatisfiable_node_local_predicate_is_rejected():
    query = kernel(predicate=Literal(True))
    with pytest.raises(PopulationError, match="explicit false valuation"):
        synthesize_state(query, scale=10, data_seed=0)


def fixed_boolean_query(*, where=False, value=7):
    high = Exists(Edge(Node("u"), Relationship(type="VISIT"), Node("v", ("Candidate",)), "out"),
                  Comparison(Property("v", "score"), "=", Literal(value)))
    condition = Boolean("AND", (Comparison(Property("u", "enabled"), "=", Literal(True)), high))
    return Query(Node("u", ("Root",), (("id", Literal(1)),)), condition if where else None,
                 (Property("u", "uid"),) if where else
                 (Property("u", "uid"), Alias(condition, "predicate")))


def fixed_join_query():
    return Query(Node("u", ("Root",), (("id", Literal(1)),)), None,
                 (Property("u", "uid"), Property("a", "uid"), Property("b", "uid")), matches=(
                     Match(Edge(Node("u"), Relationship(type="LEFT"), Node("a", ("Left",)), "out"),
                           Comparison(Property("a", "keep"), "=", Literal(True))),
                     Match(Edge(Node("u"), Relationship(type="RIGHT"), Node("b", ("Right",)), "out"),
                           Comparison(Property("b", "keep"), "=", Literal(True))),
                 ))


@pytest.mark.parametrize("truth", [False, True])
def test_fixed_component_predicate_projection_respects_explicit_truth(truth):
    query = fixed_boolean_query()
    result = synthesize_state(query, scale=12, data_seed=0,
                              expected_values={"predicate": truth})
    assert result.expected_rows[0]["predicate"] is truth
    assert evaluate_query(query, result.spec) == result.expected_rows
    component = result.constraints["components"][0]
    assert component["failed_candidates"] == 12
    assert component["candidate_matches"] == component["valid_matches"] + 12


def test_fixed_component_local_integer_predicate_changes_population_values():
    for score in (7, 23):
        result = synthesize_state(fixed_boolean_query(where=True, value=score), scale=5, data_seed=0)
        component = result.constraints["components"][0]
        assert component["true_properties"]["score"] == score
        assert component["false_properties"]["score"] != score
        assert component["valid_matches"] >= 1


def test_decisive_scalar_sibling_priority_comes_from_the_actual_boolean_ast():
    query = fixed_boolean_query()
    result = synthesize_state(query, scale=12, data_seed=0)
    roots = [node for node in result.spec.iter_nodes() if "Root" in node["labels"]]
    assert roots[0]["properties"]["enabled"] is False
    assert result.expected_rows[0]["predicate"] is False
    assert result.constraints["components"][0]["valid_matches"] == 0
    assert result.evidence["skippable"] is True
    # Requiring a true output makes the same preferred assignment invalid;
    # it must be rejected by whole-query semantics, not forced into the graph.
    true_result = synthesize_state(query, scale=12, data_seed=0, expected_values={"predicate": True})
    assert true_result.constraints["root_properties"]["enabled"] is True
    assert true_result.evidence["skippable"] is False


def test_decisive_scalar_with_alias_resolves_its_lexical_producer():
    expensive = Exists(Edge(Node("r"), Relationship(type="R"), Node("v", ("Candidate",)), "out"),
                       Comparison(Property("v", "keep"), "=", Literal(True)))
    query = Query(None, None, (Property("r", "uid"),
                              Alias(Boolean("AND", (Variable("gate"), expensive)), "answer")),
                  pipeline=(
                      Match(Node("r", ("Origin",), (("token", Literal(17)),))),
                      With((Variable("r"), Alias(Comparison(Property("r", "mode"), "=", Literal(7)), "gate"))),
                  ))
    result = synthesize_state(query, scale=11, data_seed=0)
    assert result.constraints["root_properties"]["mode"] != 7
    assert result.expected_rows[0]["answer"] is False
    assert result.evidence["skippable"] is True
    assert evaluate_query(query, result.spec) == result.expected_rows
    # A later constant alias shadows the producer; following a printed name
    # instead of its symbol would falsely report a decisive false guard.
    shadowed = replace(query, pipeline=(*query.pipeline,
                                       With((Variable("r"), Alias(Literal(True), "gate")))))
    other = synthesize_state(shadowed, scale=11, data_seed=0)
    assert other.evidence["skippable"] is False


def test_shared_anchor_components_jointly_solve_product_count_threshold():
    query = outer(Comparison(Count(body=fixed_join_query()), ">", Literal(2)))
    result = synthesize_state(query, scale=9, data_seed=0)
    counts = [component["valid_matches"] for component in result.constraints["components"]]
    assert len(counts) == 2 and counts[0] * counts[1] > 2
    assert max(counts) > 1
    assert evaluate_query(query, result.spec) == result.expected_rows == [{"u.uid": 1}]


def test_quantity_evidence_does_not_multiply_across_with_distinct():
    body = fixed_join_query()
    query = Query(None, None, body.returns[:1] + body.returns[2:], pipeline=(
        Match(body.pattern), body.matches[0], With((Variable("u"),), distinct=True), body.matches[1],
    ))
    result = synthesize_state(query, scale=9, data_seed=0, result_rows=3)
    chains = result.evidence["quantity_relation"]["mandatory_match_chains"]
    assert len(chains) == 2
    assert all(len(chain["factors"]) == 1 for chain in chains)
    assert len(evaluate_query(query, result.spec)) == 3


def test_shared_anchor_component_scope_rejects_overlapping_edge_read_domains():
    query = fixed_join_query()
    both = replace(query.matches[1], pattern=replace(query.matches[1].pattern,
                                                    relationship=query.matches[0].pattern.relationship,
                                                    right=Node("b", ("Left",))))
    query = replace(query, matches=(query.matches[0], both))
    with pytest.raises(PopulationError, match="overlap"):
        synthesize_state(query, scale=10, data_seed=0)


def row_flow_pair(kind="limit", n=3, *, constant=1):
    from ir import RowLimit, UnionAll
    from population import MatchDemand, _located
    def branch(kind, label):
        edge = Edge(Node("u", ("Anchor",), (("key", Literal(19)),)),
                    Relationship(type=kind), Node("v", (label,)), "out")
        return Query(edge, None, (Alias(Literal(constant), "value"),))
    guard, low, high = branch("WITNESS", "ProofRow"), branch("SMALL", "SmallRow"), branch("LARGE", "LargeRow")
    def observation(other):
        union = UnionAll((guard, other))
        if kind == "limit":
            return RowLimit(union, n)
        if kind == "count":
            return Query(Node("u", ("Anchor",), (("key", Literal(19)),)), None,
                         (Alias(Count(body=RowLimit(union, n)), "value"),))
        return Query(Node("u", ("Anchor",), (("key", Literal(19)),)), None,
                     (Alias(Exists(body=union), "value"),))
    source, target = observation(low), observation(high)
    demands = []
    for prefix, tree in ((('query',), source), (('alternatives', 0), target)):
        for address, value in _located(tree):
            if isinstance(value, Edge):
                kind = value.relationship.type
                demands.append(MatchDemand((*prefix, *address), kind,
                                           exact_matches=1 if kind == "SMALL" else None,
                                           scale_matches=kind == "LARGE"))
    return source, target, tuple(demands)


@pytest.mark.parametrize("kind", ["limit", "count", "exists"])
def test_symbolic_joint_state_satisfies_row_flow_witness_and_real_high_matches(kind):
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair(kind, n=3)
    result = synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=13, data_seed=0)
    assert _bag_for_test(evaluate_query(source, result.spec)) == _bag_for_test(evaluate_query(target, result.spec))
    assert result.expected_rows == evaluate_query(source, result.spec)
    facts = {component["role_atoms"][0]: component for component in result.constraints["components"]}
    assert facts["WITNESS"]["valid_matches"] >= (1 if kind == "exists" else 3)
    assert facts["SMALL"]["valid_matches"] == 1
    assert facts["LARGE"]["valid_matches"] == 13
    assert all(component["failed_candidates"] == 0 for component in facts.values())
    assert len([node for node in result.spec.iter_nodes() if "ProofRow" in node["labels"]]) == facts["WITNESS"]["valid_matches"]


def _bag_for_test(rows):
    from population import _bag
    return _bag(rows)


def test_symbolic_high_expansion_is_bounded_and_protected_at_every_read():
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair("count", n=4)
    result = synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=1_000_000, data_seed=0)
    assert len(json.dumps(result.spec.to_dict())) < 8000
    assert result.expected_rows == [{"value": 4}]
    assert result.evidence["saturation_certificates"]
    assert result.evidence["validation_graph_spec"]


def test_symbolic_limit_without_constant_projection_cannot_claim_invariant_bag():
    from ir import RowLimit, UnionAll
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair("limit", n=3)
    def changed(tree):
        branches = tuple(replace(branch, returns=(Alias(Property("v", "uid"), "value"),)) for branch in tree.body.branches)
        return RowLimit(UnionAll(branches), tree.limit)
    with pytest.raises(PopulationError, match="constant"):
        synthesize_symbolic_state(changed(source), demands=demands, alternatives=(changed(target),), scale=10, data_seed=0)


def test_symbolic_witness_cardinality_comes_from_limit_not_role_name():
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair("limit", n=5)
    renamed = tuple(replace(demand, role_atom=f"atom_{i}") for i, demand in enumerate(demands))
    # Repeated relation domains still co-populate once even with different
    # provenance names. Role names never choose the graph construction.
    original = synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=12, data_seed=0)
    changed = synthesize_symbolic_state(source, demands=renamed, alternatives=(target,), scale=12, data_seed=0)
    assert original.spec.to_dict() == changed.spec.to_dict()
    assert original.expected_rows == [{"value": 1}] * 5
    conflicting = tuple(replace(demand, exact_matches=1) if demand.role_atom == "WITNESS" else demand
                        for demand in demands)
    with pytest.raises(PopulationError, match="below.*witness"):
        synthesize_symbolic_state(source, demands=conflicting, alternatives=(target,), scale=12, data_seed=0)


def test_symbolic_valid_growth_rejects_an_uncertified_second_read():
    from population import MatchDemand, _located, synthesize_symbolic_state
    source, target, demands = row_flow_pair("count", n=3)
    high = next(value for _, value in _located(target) if isinstance(value, Edge)
                and value.relationship.type == "LARGE")
    extra = Count(body=Query(high, None, (Alias(Literal(1), "value"),)))
    target = replace(target, returns=(*target.returns, Alias(extra, "unbounded")))
    with pytest.raises(PopulationError, match="outside.*certificate"):
        synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=10, data_seed=0)


def test_symbolic_zero_limit_is_empty_independently_of_valid_workload_growth():
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair("limit", n=0)
    result = synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=20, data_seed=0)
    assert result.expected_rows == []
    assert evaluate_query(target, result.spec) == []


def test_symbolic_anchor_scan_cannot_read_all_growing_endpoint_nodes():
    from population import synthesize_symbolic_state
    source, target, demands = row_flow_pair("exists", n=1)
    source = replace(source, pattern=Node("u"))
    target = replace(target, pattern=Node("u"))
    with pytest.raises(PopulationError, match="anchor node scan"):
        synthesize_symbolic_state(source, demands=demands, alternatives=(target,), scale=1_000_000, data_seed=0)


@pytest.mark.parametrize("kind", ["limit", "exists"])
@pytest.mark.parametrize("contexts", [("with", "exists", "count", "with"), ("count", "with", "with", "exists")])
def test_symbolic_witness_preserves_deep_with_alias_observation_contexts(kind, contexts):
    from composition import compose_fragment
    from population import MatchDemand, _located, synthesize_symbolic_state
    source, target, _ = row_flow_pair(kind, n=3)
    anchor = Node("u", ("Anchor",), (("key", Literal(19)),))
    source = compose_fragment(source, "rows", anchor, contexts)
    target = compose_fragment(target, "rows", anchor, contexts)
    demands = []
    for prefix, tree in ((('query',), source), (('alternatives', 0), target)):
        for address, value in _located(tree):
            if isinstance(value, Edge):
                relation = value.relationship.type
                demands.append(MatchDemand((*prefix, *address), relation,
                                           exact_matches=1 if relation == "SMALL" else None,
                                           scale_matches=relation == "LARGE"))
    result = synthesize_symbolic_state(source, demands=tuple(demands), alternatives=(target,), scale=17, data_seed=0)
    assert result.expected_rows
    assert _bag_for_test(evaluate_query(source, result.spec)) == _bag_for_test(evaluate_query(target, result.spec))
    assert any(certificate["required_rows"] == (3 if kind == "limit" else 1)
               for certificate in result.evidence["saturation_certificates"])


def test_relational_node_multilabel_population_is_a_real_near_miss():
    from population import MatchDemand, synthesize_relational_state
    query = Query(Node("a", ("Rare", "Common")), None, (Property("a", "uid"),))
    result = synthesize_relational_state(query, scale=9, data_seed=0,
        demands=(MatchDemand(("query", "pattern"), "scan", exact_matches=1, candidate_matches=10),))
    assert evaluate_query(query, result.spec) == result.expected_rows == [{"a.uid": 1}]
    nodes = list(result.spec.iter_nodes())
    assert sum("Common" in node["labels"] for node in nodes) == 10
    assert sum("Rare" in node["labels"] for node in nodes) == 1


def test_right_scalar_candidates_cannot_borrow_a_failed_left_anchor_or_right_label():
    from population import MatchDemand, synthesize_relational_state
    value = Property("v", "flag")
    tautology = Boolean("OR", (Comparison(value, "IS NULL", Literal(None)),
                                Comparison(value, "IS NOT NULL", Literal(None))))
    query = Query(Edge(Node("u", ("Root",), (("id", Literal(1)),)),
                       Relationship(type="R"), Node("v", ("Leaf",))),
                  tautology, (Property("v", "uid"),))
    demand = MatchDemand(("query", "pattern"), "work", exact_matches=1,
                         candidate_matches=8, candidate_kind="right_scalar")
    with pytest.raises(PopulationError, match="right population/scalar"):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(demand,))
    result = synthesize_relational_state(query, scale=7, data_seed=0,
                                        demands=(replace(demand, candidate_kind="right"),))
    assert len(result.expected_rows) == 1
    nodes = list(result.spec.iter_nodes())
    assert sum("Root" in node["labels"] for node in nodes) == 1
    assert sum("Leaf" in node["labels"] for node in nodes) == 1
    assert len(nodes) == 9


def test_right_scalar_candidates_preserve_right_labels_and_fail_the_actual_predicate():
    from population import MatchDemand, synthesize_relational_state
    query = Query(Edge(Node("u", ("Root",), (("id", Literal(1)),)),
                       Relationship(type="R"), Node("v", ("Leaf", "Entity"))),
                  Comparison(Property("v", "keep"), "=", Literal(True)), (Property("v", "uid"),))
    result = synthesize_relational_state(query, scale=7, data_seed=0,
        demands=(MatchDemand(("query", "pattern"), "work", exact_matches=1,
                             candidate_matches=8, candidate_kind="right_scalar"),))
    leaves = [node for node in result.spec.iter_nodes() if "Leaf" in node["labels"]]
    assert len(leaves) == 8
    assert all("Entity" in node["labels"] for node in leaves)
    assert sum(node["properties"].get("keep") is True for node in leaves) == 1
    assert len(result.expected_rows) == 1


def test_relational_chain_allocates_all_witnesses_before_noise():
    from population import MatchDemand, synthesize_relational_state
    query = Query(Edge(Node("u", ("Root",), (("id", Literal(1)),)), Relationship(type="A"), Node("v", ("Mid",))),
        Comparison(Property("v", "keep"), "=", Literal(True)), (Property("v", "uid"), Property("w", "uid")),
        matches=(Match(Edge(Node("v"), Relationship(type="B"), Node("w", ("Leaf",))),
                       Comparison(Property("w", "keep"), "=", Literal(True))),))
    demands = (MatchDemand(("query", "pattern"), "first", exact_matches=3),
               MatchDemand(("query", "matches", 0, "pattern"), "second", exact_matches=1, candidate_matches=12))
    small = synthesize_relational_state(query, scale=4, data_seed=0, demands=demands)
    large = synthesize_relational_state(query, scale=70, data_seed=0,
        demands=(demands[0], replace(demands[1], candidate_matches=71)))
    assert small.expected_rows == large.expected_rows
    assert len(large.expected_rows) == 3
    assert evaluate_query(query, large.spec) == large.expected_rows


def test_relational_optional_empty_matches_keep_null_and_do_not_rebind_it():
    from population import MatchDemand, synthesize_relational_state
    query = Query(Node("u", ("Root",), (("id", Literal(1)),)), None,
                  (Property("u", "uid"), Property("v", "uid"), Property("w", "uid")), matches=(
        Match(Edge(Node("u"), Relationship(type="A"), Node("v", ("Middle",))),
              Comparison(Property("v", "keep"), "=", Literal(True)), optional=True),
        Match(Edge(Node("v"), Relationship(type="B"), Node("w", ("Leaf",))), optional=True),))
    result = synthesize_relational_state(query, scale=8, data_seed=0, demands=(
        MatchDemand(("query", "matches", 0, "pattern"), "empty", exact_matches=0, candidate_matches=9),
        MatchDemand(("query", "matches", 1, "pattern"), "next", exact_matches=1)))
    assert result.expected_rows == [{"u.uid": 1, "v.uid": None, "w.uid": None}]
    assert evaluate_query(query, result.spec) == result.expected_rows


def test_relational_inverse_uses_the_same_vertices_and_pair_property():
    from population import InversePair, MatchDemand, synthesize_relational_state
    forward = Edge(Node("u", ("Root",), (("id", Literal(1)),)), Relationship(type="F"), Node("v", ("Leaf",)))
    reverse = replace(forward, relationship=Relationship(type="R"), direction="in")
    source = Query(forward, None, (Property("u", "uid"), Property("v", "uid")))
    target = replace(source, pattern=reverse)
    result = synthesize_relational_state(source, alternatives=(target,), scale=4, data_seed=0,
        demands=(MatchDemand(("query", "pattern"), "f", exact_matches=3),),
        obligations=(InversePair("F", "R", "pair"),))
    assert evaluate_query(source, result.spec) == evaluate_query(target, result.spec)
    edges = list(result.spec.iter_edges())
    assert {(e["source"],e["target"],e["properties"]["pair"]) for e in edges if e["type"] == "F"} == {
        (e["target"],e["source"],e["properties"]["pair"]) for e in edges if e["type"] == "R"}


def test_relational_masked_valid_population_and_conditional_rows():
    from ir import ConditionalQuery
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    high = Query(Edge(Node("u", ("Root",), (("id", Literal(1)),)), Relationship(type="H"), Node("v", ("Leaf",))),
                 None, (Alias(Literal(1), "value"),))
    low = Query(Node("u", ("Root",), (("id", Literal(1)),)), None, (Alias(Literal(1), "value"),))
    source = ConditionalQuery(Literal(False), high, low)
    result = synthesize_relational_state(source, alternatives=(low,), scale=40, data_seed=0,
        demands=(MatchDemand(("query", "then", "pattern"), "work", scale_matches=True),),
        obligations=(ValueDemand(("query", "condition"), False),))
    assert result.expected_rows == [{"value": 1}]
    assert len(list(result.spec.iter_edges())) == 40
    assert evaluate_query(source, result.spec) == result.expected_rows


def test_relational_same_printed_name_is_not_a_lexical_binding_identity():
    from population import MatchDemand, synthesize_relational_state
    root = Node("root", ("Root",), (("id", Literal(1)),))
    first = Query(Edge(root, Relationship(type="A"), Node("item", ("Shared",))), None, (Alias(Literal(1), "c"),))
    second = Query(Edge(root, Relationship(type="B"), Node("item", ("Shared",))), None, (Alias(Literal(1), "c"),))
    query = Query(root, None, (Alias(Count(body=first), "a"), Alias(Count(body=second), "b")))
    result = synthesize_relational_state(query, scale=4, data_seed=0)
    domains = [item for item in result.constraints["binding_domains"] if item["name"] == "item"]
    assert len(domains) == 2
    edges = list(result.spec.iter_edges())
    assert next(edge["target"] for edge in edges if edge["type"] == "A") != next(edge["target"] for edge in edges if edge["type"] == "B")


def test_relational_explicit_binding_equality_and_separation_are_enforced():
    from population import BindingRelation, synthesize_relational_state
    edge = Edge(Node("a", ("First",)), Relationship(type="R"), Node("b", ("Second",)))
    query = Query(edge, None, (Property("a", "uid"), Property("b", "uid")))
    # Equality on an edge would create a loop, outside the one-hop normal form;
    # it must be rejected rather than silently allocating independent vertices.
    with pytest.raises(PopulationError, match="Self-loop"):
        synthesize_relational_state(query, scale=2, data_seed=0, obligations=(
            BindingRelation(("query", "pattern", "left"), ("query", "pattern", "right")),))
    result = synthesize_relational_state(query, scale=2, data_seed=0, obligations=(
        BindingRelation(("query", "pattern", "left"), ("query", "pattern", "right"), equal=False),))
    assert result.expected_rows[0]["a.uid"] != result.expected_rows[0]["b.uid"]


@pytest.mark.parametrize("growing", [False, True])
def test_relational_quantity_is_not_clamped_to_a_fixed_singleton_target(growing):
    from population import MatchDemand, synthesize_relational_state
    query = Query(Edge(Node("u", ("Root",), (("id", Literal(1)),)), Relationship(type="R"),
                       Node("v", ("Leaf",), (("id", Literal(2)),))), None, (Property("v", "uid"),))
    demand = MatchDemand(("query", "pattern"), "work", exact_matches=None if growing else 3, scale_matches=growing)
    with pytest.raises(PopulationError, match="parallel|quantity|multiplicity"):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(demand,))


def test_relational_fixed_access_path_derives_declared_lookup_index():
    from population import synthesize_relational_state
    query = Query(Node("u", ("Origin",), (("token", Literal(17)),)), None, (Property("u", "uid"),))
    result = synthesize_relational_state(query, scale=2, data_seed=0)
    assert result.spec.indexes == [{"name": "gsp_cost_lookup", "label": "Origin", "property": "token"}]


def test_relational_mask_guard_must_cover_other_static_domains_with_the_same_label():
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    high = Query(Edge(Node("h", ("Anchor",), (("id", Literal(1)),)), Relationship(type="R"),
                      Node("x", ("Work",))), None, (Alias(Literal(1), "row"),))
    extra = Query(Node("other", ("Root",), (("enabled", Literal(False)), ("token", Literal(2)))),
                  None, (Alias(Literal(1), "row"),))
    condition = Comparison(Property("u", "enabled"), "=", Literal(True))
    query = Query(Node("u", ("Root",)), None, (
        Alias(Case(condition, Literal(0), Count(body=high)), "c"), Alias(Count(body=extra), "static")))
    with pytest.raises(PopulationError, match="certificate|obligations"):
        synthesize_relational_state(query, scale=5001, data_seed=0, demands=(
            MatchDemand(("query", "returns", 0, "expression", "otherwise", "body", "pattern"),
                        "work", scale_matches=True),), obligations=(
            ValueDemand(("query", "returns", 0, "expression", "condition"), True),))


def test_relational_binding_equality_can_identify_two_node_only_domains():
    from population import BindingRelation, synthesize_relational_state
    query = Query(Node("a", ("First",)), None, (Property("a", "uid"), Property("b", "uid")),
                  matches=(Match(Node("b", ("Second",))),))
    result = synthesize_relational_state(query, scale=2, data_seed=0, obligations=(
        BindingRelation(("query", "pattern"), ("query", "matches", 0, "pattern")),))
    assert result.expected_rows == [{"a.uid": 1, "b.uid": 1}]


def test_explicit_origins_align_a_same_label_chain_without_schema_guessing():
    from population import BindingRelation, MatchDemand, synthesize_relational_state
    first = Edge(Node("u", ("Root",), (("id", Literal(1)),)), Relationship(type="A"), Node("v", ("Shared",)))
    second = Edge(Node("v", ("Shared",)), Relationship(type="B"), Node("w", ("Shared",)))
    query = Query(first, None, (Property("v", "uid"), Property("w", "uid")),
                  matches=(Match(second, Comparison(Property("w", "keep"), "=", Literal(True))),))
    target = Query(second, query.matches[0].where, query.returns, matches=(Match(first),))
    pairs = ((('pattern', 'left'), ('matches', 0, 'pattern', 'left')),
             (('pattern', 'right'), ('matches', 0, 'pattern', 'right')),
             (('matches', 0, 'pattern', 'left'), ('pattern', 'left')),
             (('matches', 0, 'pattern', 'right'), ('pattern', 'right')))
    origins = tuple(BindingRelation(('query', *old), ('alternatives', 0, *new)) for old, new in pairs)
    result = synthesize_relational_state(query, alternatives=(target,), scale=6, data_seed=0,
        correspondence=origins, min_result_rows=3,
        demands=(MatchDemand(('query', 'pattern'), 'input', exact_matches=3),
                 MatchDemand(('query', 'matches', 0, 'pattern'), 'work', exact_matches=1, candidate_matches=7)))
    assert len(result.expected_rows) == 3
    assert evaluate_query(query, result.spec) == evaluate_query(target, result.spec)


def test_minimum_output_goal_rejects_an_otherwise_valid_empty_pair():
    from population import synthesize_relational_state
    query = Query(Node('u', ('Root',)), Literal(False), (Literal(1),))
    with pytest.raises(PopulationError, match='obligations|model'):
        synthesize_relational_state(query, scale=5, data_seed=0, min_result_rows=1)


@pytest.mark.parametrize('consumer', [Count, Exists])
def test_graph_dependent_guard_proves_mask_only_for_an_invariant_read(consumer):
    from ir import ConditionalQuery
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    guard_rows = Query(Edge(anchor, Relationship(type='GUARD'), Node('item', ('Guard',))),
                       None, (Alias(Literal(1), 'c'),))
    guard = (Comparison(Count(body=guard_rows), '>', Literal(2)) if consumer is Count
             else Exists(body=guard_rows))
    low = Query(anchor, None, (Alias(Literal(1), 'c'),))
    high = Query(Edge(anchor, Relationship(type='HIGH'), Node('item', ('Payload',))),
                 None, (Alias(Literal(1), 'c'),))
    query = ConditionalQuery(guard, low, low)
    target = replace(query, otherwise=high)
    result = synthesize_relational_state(query, alternatives=(target,), scale=5001, data_seed=0,
        demands=(MatchDemand(('query', 'condition', 'left', 'body', 'pattern') if consumer is Count
                             else ('query', 'condition', 'body', 'pattern'), 'guard', exact_matches=3),
                 MatchDemand(('alternatives', 0, 'otherwise', 'pattern'), 'work', scale_matches=True)),
        obligations=(ValueDemand(('query', 'condition'), True),), min_result_rows=1)
    assert result.expected_rows == [{'c': 1}]
    assert any(observation['mask'] and observation['mask']['kind'] == 'conditional_selected_other_branch'
               for certificate in result.evidence['growth_certificates'] for observation in certificate['observations'])


def test_graph_guard_cannot_use_a_small_count_when_it_reads_the_growing_domain():
    from ir import ConditionalQuery
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    observed = Query(Node('item', ('Shared',)), None, (Literal(1),))
    low = Query(anchor, None, (Alias(Literal(1), 'c'),))
    high = Query(Edge(anchor, Relationship(type='H'), Node('item', ('Shared',))),
                 None, (Alias(Literal(1), 'c'),))
    query = ConditionalQuery(Comparison(Count(body=observed), '>', Literal(2)), low, low)
    with pytest.raises(PopulationError, match='growing|obligations|certificate'):
        synthesize_relational_state(query, alternatives=(replace(query, otherwise=high),), scale=5001, data_seed=0,
            demands=(MatchDemand(('query', 'condition', 'left', 'body', 'pattern'), 'guard', exact_matches=3),
                     MatchDemand(('alternatives', 0, 'otherwise', 'pattern'), 'work', scale_matches=True)),
            obligations=(ValueDemand(('query', 'condition'), True),), min_result_rows=1)


def test_graph_guard_alias_resolves_its_producer_and_leaves_subquery_locals_unbound():
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    guard_rows = Query(Edge(anchor, Relationship(type='G'), Node('item', ('Guard',))), None, (Literal(1),))
    high_rows = Query(Edge(anchor, Relationship(type='H'), Node('item', ('Payload',))), None, (Literal(1),))
    condition = Comparison(Variable('n'), '>', Literal(2))
    query = Query(None, None, (Alias(Case(condition, Literal(1), Count(body=high_rows)), 'value'),),
                  pipeline=(Match(anchor), With((Variable('u'), Alias(Count(body=guard_rows), 'n')))))
    result = synthesize_relational_state(query, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'pipeline', 1, 'projections', 1, 'expression', 'body', 'pattern'),
                             'guard', exact_matches=3),
                 MatchDemand(('query', 'returns', 0, 'expression', 'otherwise', 'body', 'pattern'),
                             'work', scale_matches=True)),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression', 'condition'), True),))
    assert result.expected_rows == [{'value': 1}]


def test_graph_guard_alias_keeps_its_old_node_after_same_name_rebinding():
    from population import MatchDemand, ValueDemand, synthesize_relational_state
    old = Node('u', ('First',), (('id', Literal(1)),))
    new = Node('u', ('Second',), (('id', Literal(2)),))
    guard_rows = Query(Edge(old, Relationship(type='G'), Node('item', ('Guard',))), None, (Literal(1),))
    high_rows = Query(Edge(new, Relationship(type='H'), Node('item', ('Payload',))), None, (Literal(1),))
    condition = Comparison(Variable('n'), '>', Literal(2))
    query = Query(None, None, (Alias(Case(condition, Literal(1), Count(body=high_rows)), 'value'),),
                  pipeline=(Match(old), With((Alias(Count(body=guard_rows), 'n'),)), Match(new)))
    result = synthesize_relational_state(query, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'pipeline', 1, 'projections', 0, 'expression', 'body', 'pattern'),
                             'guard', exact_matches=3),
                 MatchDemand(('query', 'returns', 0, 'expression', 'otherwise', 'body', 'pattern'),
                             'work', scale_matches=True)),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression', 'condition'), True),))
    assert result.expected_rows == [{'value': 1}]


def test_origin_copy_does_not_multiply_the_finite_property_search_domain():
    from dependencies import _located
    from population import BindingRelation, synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    matches = tuple(Match(Edge(anchor, Relationship(type=f'R{i}'), Node(f'v{i}', (f'L{i}',))),
                          Negation(Comparison(Property(f'v{i}', 'keep'), '=', Literal(False))))
                    for i in range(8))
    query = Query(anchor, None, (Alias(Literal(1), 'value'),), matches=matches)
    origins = tuple(BindingRelation(('query', *path), ('alternatives', 0, *path))
                    for path, value in _located(query) if isinstance(value, Node))
    # Three distinct values per free property fit the model budget. Copying
    # the query must not turn that domain into six duplicate candidates.
    result = synthesize_relational_state(query, alternatives=(query,), scale=7, data_seed=0,
                                        correspondence=origins, min_result_rows=1)
    assert result.expected_rows == [{'value': 1}]


def test_positive_local_predicates_prune_before_the_global_property_model_budget():
    from population import synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    matches = tuple(Match(Edge(anchor, Relationship(type=f'R{i}'), Node(f'v{i}', (f'L{i}',))),
                          Negation(Comparison(Property(f'v{i}', 'keep'), '=', Literal(False))))
                    for i in range(12))
    query = Query(anchor, None, (Alias(Literal(1), 'value'),), matches=matches)
    # Each profile has one valid local model. The raw 3^12 product puts their
    # conjunction after the 50k global budget although no real search is needed.
    result = synthesize_relational_state(query, scale=7, data_seed=0, min_result_rows=1)
    assert result.expected_rows == [{'value': 1}]
    assert all(node['properties']['keep'] is True for node in result.spec.iter_nodes()
               if 'Root' not in node['labels'])


@pytest.mark.parametrize('truth', [False, None])
def test_local_witness_pruning_preserves_independent_false_and_null_input_facts(truth):
    from population import ValueDemand, synthesize_relational_state
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    matches = tuple(Match(Edge(anchor, Relationship(type=f'R{i}'), Node(f'v{i}', (f'L{i}',))),
                          Negation(Comparison(Property(f'v{i}', 'keep'), '=', Literal(False))))
                    for i in range(11))
    condition = Comparison(Property('u', 'flag'), '=', Literal(True))
    query = Query(anchor, None, (Alias(Case(condition, Literal(0), Literal(1)), 'value'),), matches=matches)
    result = synthesize_relational_state(query, scale=7, data_seed=0, min_result_rows=1,
        obligations=(ValueDemand(('query', 'returns', 0, 'expression', 'condition'), truth),))
    assert result.expected_rows == [{'value': 1}]
    root = next(node for node in result.spec.iter_nodes() if 'Root' in node['labels'])
    assert root['properties']['flag'] is truth
