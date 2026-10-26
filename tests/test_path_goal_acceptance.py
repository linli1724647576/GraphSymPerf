"""Independent AST acceptance for bounded paths and universal predicates.

The authored cases use public semantic goals, then the ordinary two algorithms.
They intentionally do not copy the public P3 grammar's graph quantities.
"""

from dataclasses import fields, is_dataclass, replace

import pytest

import ir
from dependencies import find_mutation_sites
from executor import typed_bag
from population import PopulationError, evaluate_query
from symbolic import MutationMark, SymbolicSkeleton, build_symbolic_skeleton, construct_state, instantiate_pair


ANCHOR = ir.Node("u", ("PathInput",), (("id", ir.Literal(1)),))


def walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from walk(item, (*address, index))


def path_body(*, bounds=(1, 4), length=None, predicate=None, filtered=True, projection="constant"):
    pattern = ir.Edge(ANCHOR, ir.Relationship(type="PATH_LINK", min_hops=bounds[0], max_hops=bounds[1]),
                      ir.Node("v", ("PathOutput",)), "undirected", "p")
    universal = ir.All("x", ir.Call("nodes", (ir.Variable("p"),)),
                       predicate if predicate is not None else ir.Comparison(ir.Property("x", "keep"), "=", ir.Literal(True)))
    terms = [universal] if filtered else []
    if length is not None:
        terms.append(ir.Comparison(ir.Call("length", (ir.Variable("p"),)), "=", ir.Literal(length)))
    where = terms[0] if len(terms) == 1 else ir.Boolean("AND", tuple(terms)) if terms else None
    returns = ((ir.Property("u", "uid"), ir.Property("v", "uid"),
                ir.Alias(ir.Call("length", (ir.Variable("p"),)), "hops")) if projection == "bindings"
               else (ir.Alias(universal, "truth"),) if projection == "truth"
               else (ir.Alias(ir.Literal(1), "c"),))
    return ir.Query(pattern, where, returns)


def skeleton(source, *, work=True, extra_goals=(), selected=0):
    ir.validate_query(source)
    sites = find_mutation_sites(source, "P3", "bounded_path")
    assert sites, "an independent AST must contain its native reversal law"
    site = sites[selected]
    goals = ({"kind": "output", "min_rows": 1},)
    if work:
        goals += ({"kind": "work", "mode": "auto", "seed": 0},)
    return SymbolicSkeleton("P3", "bounded_path", source, None, ANCHOR,
                            {"contexts": (), "threshold": 0}, 0, (),
                            MutationMark("path-acceptance", site.address), (*goals, *extra_goals))


def counted_skeleton(*, threshold=4, length=2, bounds=(1, 4), alias=True):
    body = path_body(bounds=bounds, length=length)
    count = ir.Count(body=body)
    if alias:
        source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Variable("n")), pipeline=(
            ir.Match(ANCHOR), ir.With((ir.Variable("u"), ir.Alias(count, "n")),
                                     ir.Comparison(ir.Variable("n"), ">", ir.Literal(threshold))),
        ))
    else:
        source = ir.Query(ANCHOR, ir.Comparison(count, ">", ir.Literal(threshold)),
                          (ir.Property("u", "uid"), ir.Alias(count, "n")))
        # A second lexical path is a distinct read; use only one COUNT occurrence.
        source = replace(source, returns=(ir.Property("u", "uid"),))
    return skeleton(source)


def truth_skeleton(desired, *, negated=False):
    body = path_body(filtered=False, projection="truth", length=2)
    expression = body.returns[0].expression
    if negated:
        expression = ir.Negation(expression)
        body = replace(body, returns=(ir.Alias(expression, "truth"),))
    location = ("query", "returns", 0, "expression")
    return skeleton(body, work=False,
                    extra_goals=({"kind": "value", "address": location, "relation": "equal", "value": desired},))


def aliased_truth_skeleton(desired):
    body = path_body(filtered=False, projection="truth", length=2)
    universal = body.returns[0].expression
    source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Variable("observed")), pipeline=(
        ir.Match(body.pattern),
        ir.With((ir.Variable("u"), ir.Alias(universal, "observed"))),
    ))
    return skeleton(source, work=False,
        extra_goals=({"kind": "value", "address": ("query", "returns", 1), "relation": "equal", "value": desired},))


def solve(item, *, scale=7):
    solved = construct_state(item, scale=scale, data_seed=3)
    pair = instantiate_pair(item, solved)
    assert solved.population.expected_rows
    assert pair.query_a != pair.query_b
    assert pair.instantiation_record["uses_construction_assignment"] == solved.construction_record["assignment_digest"]
    assert pair.instantiation_record["graph_digest"] == solved.construction_record["graph_digest"]
    assert all(check in pair.checks for check in pair.bindings.get("required_state_checks", ()))
    if solved.population.spec.counts()["nodes"] <= 5000:
        for tree in (pair.query_a, pair.query_b):
            assert typed_bag(evaluate_query(tree, solved.population.spec)) == typed_bag(solved.population.expected_rows)
    return solved, pair


@pytest.mark.parametrize("contexts", [[], ["exists"], ["count"], ["count", "with"]])
@pytest.mark.parametrize("bounds", [(1, 3), (2, 4)])
def test_public_paths_enter_the_same_goal_pipeline_inside_outer_observers(contexts, bounds):
    item = build_symbolic_skeleton("P3", {"contexts": contexts, "path_bounds": bounds, "threshold": 3}, 0)
    assert item.intents == ()
    assert item.mark is not None and item.goals
    solved, _ = solve(item)
    record = solved.construction_record["requirements"]
    assert record["derivation"]["inward_steps"]
    assert any(part.get("node") == "PathDemand" for part in record["demands"])
    assert record["derivation"]["work"]["potential_extra_candidate_work"] >= 7


@pytest.mark.parametrize("branch", ["then", "otherwise"])
@pytest.mark.parametrize("guard", ["count", "exists", "property"])
def test_public_path_leaf_can_recur_on_either_conditional_child(branch, guard):
    item = build_symbolic_skeleton("P3", {"contexts": ["count", "with"], "path_bounds": (2, 4),
        "threshold": 3, "conditional_depth": 2, "branch_path": [branch], "guard_kind": guard}, 0)
    solve(item)


def test_native_count_alias_propagates_both_quantity_and_exact_length_to_the_path():
    item = counted_skeleton(threshold=4, length=2)
    solved, pair = solve(item)
    assert solved.population.expected_rows[0]["n"] >= 5
    source = item.source.pipeline[1].projections[1].expression.body
    rows = evaluate_query(replace(source, returns=(ir.Alias(ir.Call("length", (ir.Variable("p"),)), "hops"),)),
                          solved.population.spec)
    assert len(rows) == solved.population.expected_rows[0]["n"]
    assert all(row["hops"] == 2 for row in rows)
    assert len(pair.bindings.get("required_state_checks", ())) > 0


@pytest.mark.parametrize("desired", [True, False, None])
@pytest.mark.parametrize("negated", [False, True])
def test_all_three_valued_result_is_constructed_without_conflating_null_and_false(desired, negated):
    solved, _ = solve(truth_skeleton(desired, negated=negated))
    assert all(row["truth"] is desired for row in solved.population.expected_rows)


@pytest.mark.parametrize("desired", [True, False, None])
def test_all_truth_propagates_through_the_actual_with_alias_producer(desired):
    solved, _ = solve(aliased_truth_skeleton(desired))
    assert all(row["observed"] is desired for row in solved.population.expected_rows)


def test_a_negated_node_predicate_uses_its_truth_semantics_instead_of_a_keep_false_recipe():
    predicate = ir.Negation(ir.Comparison(ir.Property("x", "disabled"), "=", ir.Literal(True)))
    item = skeleton(path_body(predicate=predicate, bounds=(2, 4), projection="bindings"))
    solved, _ = solve(item)
    nodes = list(solved.population.spec.iter_nodes())
    assert any(node["properties"].get("disabled") is False for node in nodes)
    assert any(node["properties"].get("disabled") is True for node in nodes)
    assert all("keep" not in node["properties"] for node in nodes)


def test_impossible_length_is_rejected_before_a_graph_can_be_accepted():
    with pytest.raises(PopulationError):
        construct_state(counted_skeleton(bounds=(2, 4), length=1), scale=7, data_seed=3)


def test_an_all_true_predicate_cannot_supply_false_prefix_growth():
    with pytest.raises(PopulationError):
        construct_state(skeleton(path_body(predicate=ir.Literal(True))), scale=7, data_seed=3)


def test_all_hidden_by_or_true_cannot_certify_safely_rejected_growth():
    body = path_body()
    body = replace(body, where=ir.Boolean("OR", (body.where, ir.Literal(True))))
    with pytest.raises(PopulationError):
        construct_state(skeleton(body), scale=7, data_seed=3)


def test_a_second_lexical_path_with_the_same_printed_name_is_not_one_quantity_atom():
    body = path_body()
    unguarded = replace(body, where=None)
    source = ir.Query(ANCHOR, None, (ir.Alias(ir.Count(body=body), "good"),
                                   ir.Alias(ir.Count(body=unguarded), "all_paths")))
    with pytest.raises(PopulationError):
        construct_state(skeleton(source), scale=7, data_seed=3)


@pytest.mark.parametrize("labels", [(), ("PathOutput",)])
def test_an_unprotected_scan_cannot_read_the_expanding_family_even_below_a_late_threshold(labels):
    body = path_body()
    source = ir.Query(ANCHOR, None, (ir.Alias(ir.Exists(body=body), "protected"),
        ir.Alias(ir.Comparison(ir.Count(ir.Node("z", labels)), ">", ir.Literal(6000)), "late")))
    with pytest.raises(PopulationError):
        construct_state(skeleton(source), scale=7000, data_seed=3)


def test_large_path_growth_keeps_the_proof_small_and_the_accepted_assignment_fixed():
    solved, pair = solve(counted_skeleton(threshold=4, length=2), scale=6000)
    assert solved.population.spec.counts()["nodes"] > 5000
    assert solved.construction_record["requirements"]["derivation"]["work"]["potential_extra_candidate_work"] >= 6000
    assert pair.instantiation_record["graph_digest"] == solved.construction_record["graph_digest"]
    assert len(solved.population.evidence["validation_graph_spec"]["node_groups"]) < 100


def test_a_single_hop_path_is_a_valid_non_growing_semantic_instance():
    item = skeleton(path_body(bounds=(1, 1), projection="bindings"), work=False)
    solved, _ = solve(item)
    assert all(row["hops"] == 1 for row in solved.population.expected_rows)


@pytest.mark.parametrize("bounds", [(0, 3), (1, 9)])
def test_path_bounds_outside_the_current_construction_certificate_are_explicitly_rejected(bounds):
    with pytest.raises(PopulationError):
        construct_state(skeleton(path_body(bounds=bounds)), scale=7, data_seed=3)


def test_walk_semantics_cannot_borrow_the_trail_tree_expansion_certificate():
    body = path_body()
    body = replace(body, pattern=replace(body.pattern,
        relationship=replace(body.pattern.relationship, path_mode="walk")))
    with pytest.raises(PopulationError):
        construct_state(skeleton(body), scale=7, data_seed=3)


def test_count_interval_and_length_interval_are_jointly_propagated():
    body = path_body(bounds=(1, 4))
    hops = ir.Call("length", (ir.Variable("p"),))
    body = replace(body, where=ir.Boolean("AND", (body.where,
        ir.Comparison(hops, ">=", ir.Literal(2)), ir.Comparison(hops, "<", ir.Literal(4)))))
    source = ir.Query(None, None, (ir.Variable("n"),), pipeline=(ir.Match(ANCHOR),
        ir.With((ir.Variable("u"), ir.Alias(ir.Count(body=body), "n")), ir.Boolean("AND", (
            ir.Comparison(ir.Variable("n"), ">=", ir.Literal(2)),
            ir.Comparison(ir.Variable("n"), "<=", ir.Literal(4))))),))
    solved, _ = solve(skeleton(source))
    assert 2 <= solved.population.expected_rows[0]["n"] <= 4
    actual = evaluate_query(replace(body, returns=(ir.Alias(hops, "hops"),)), solved.population.spec)
    assert all(2 <= row["hops"] < 4 for row in actual)


@pytest.mark.parametrize("endpoint", ["start", "end"])
def test_all_includes_the_anchor_and_terminal_node_in_its_truth_obligation(endpoint):
    body = path_body(bounds=(2, 3))
    pattern = body.pattern
    if endpoint == "start":
        pattern = replace(pattern, left=replace(pattern.left, properties=(*pattern.left.properties, ("keep", ir.Literal(False)))))
    else:
        pattern = replace(pattern, right=replace(pattern.right, properties=(("keep", ir.Literal(False)),)))
    item = skeleton(replace(body, pattern=pattern))
    with pytest.raises(PopulationError):
        construct_state(item, scale=7, data_seed=3)


def test_count_of_a_scalar_projection_counts_one_row_not_the_inner_path_count():
    scalar_row = ir.Query(None, None, (ir.Alias(ir.Count(body=path_body()), "nested_count"),))
    source = ir.Query(ANCHOR, ir.Comparison(ir.Count(body=scalar_row), ">", ir.Literal(2)),
                      (ir.Property("u", "uid"),))
    with pytest.raises(PopulationError):
        construct_state(skeleton(source), scale=7, data_seed=3)


def test_shadowing_a_count_alias_does_not_add_a_huge_path_quantity_requirement():
    source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Variable("n")), pipeline=(
        ir.Match(ANCHOR), ir.With((ir.Variable("u"), ir.Alias(ir.Count(body=path_body()), "n"))),
        ir.With((ir.Variable("u"), ir.Alias(ir.Literal(895440), "n")),
                ir.Comparison(ir.Variable("n"), "=", ir.Literal(895440))),
    ))
    solved, _ = solve(skeleton(source))
    assert solved.population.expected_rows == [{"u.uid": 1, "n": 895440}]
    for domain in solved.construction_record["requirements"]["derivation"]["count_domains"]:
        assert all(bound is None or bound < 895440 for interval in domain["intervals"] for bound in interval)
