"""Independent AST contracts for matching components and OPTIONAL row flow."""

from dataclasses import fields, is_dataclass, replace
from collections import Counter

import pytest

import ir
from dependencies import find_mutation_sites, matching_quantity_observation, node_at, replace_at
from executor import typed_bag
from main import prepare_case
from mutator import plan_mutation_with_correspondence
from population import EntityBindingDemand, MatchDemand, PopulationError, evaluate_query, synthesize_relational_state
from state import GraphSpec
from symbolic import MutationMark, SymbolicSkeleton, build_symbolic_skeleton, construct_state, instantiate_pair


ANCHOR = ir.Node("u", ("OptionalAnchor",), (("id", ir.Literal(1)),))


def walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from walk(item, (*address, index))


def flag(variable):
    return ir.Comparison(ir.Property(variable, "accepted"), "=", ir.Literal(True))


def component(name, *, root=ANCHOR):
    return ir.Edge(root, ir.Relationship(type="R_" + name), ir.Node(name, ("Endpoint_" + name,)))


def skeleton(source, pattern="P11", form="optional_branches", *, work=False, address=None, seed=0, extra_goals=()):
    ir.validate_query(source)
    sites = find_mutation_sites(source, pattern, form)
    assert sites, "the authored AST must contain its native matching law"
    site = sites[0] if address is None else next(item for item in sites if item.address == address)
    goals = ({"kind": "output", "min_rows": 1},)
    if work:
        goals += ({"kind": "work", "mode": "auto", "seed": seed},)
    return SymbolicSkeleton(pattern, form, source, None, ANCHOR, {"contexts": (), "threshold": 0}, seed,
                            (), MutationMark("optional-acceptance", site.address), (*goals, *extra_goals))


def optional_skeleton(state="neither", *, work=False):
    left, right = component("left"), component("right")
    present = {"both": (True, True), "left_only": (True, False),
               "right_only": (False, True), "neither": (False, False)}[state]
    terms = tuple(ir.Comparison(ir.Property(name, "uid"), "IS NOT NULL" if has_match else "IS NULL", ir.Literal(None))
                  for name, has_match in zip(("left", "right"), present))
    source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Property("left", "uid"), ir.Property("right", "uid")), pipeline=(
        ir.Match(ANCHOR), ir.Match(left, flag("left"), optional=True), ir.Match(right, flag("right"), optional=True),
        ir.With((ir.Variable("u"), ir.Variable("left"), ir.Variable("right")), ir.Boolean("AND", terms)),
    ))
    return skeleton(source, work=work)


def matching_skeleton(form="shared_anchor", *, threshold=None):
    left = (ir.Node("left", ("IndependentRows",)) if form == "independent" else component("left"))
    right = component("right")
    flow = ir.Query(None, None, (ir.Property("u", "uid"), ir.Property("left", "uid"), ir.Property("right", "uid")), pipeline=(
        ir.Match(ANCHOR), ir.Match(left, flag("left")), ir.Match(right, flag("right")),
    ))
    if threshold is None:
        source, address = flow, ("pipeline", 1)
    else:
        source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Variable("observed")), pipeline=(
            ir.Match(ANCHOR),
            ir.With((ir.Variable("u"), ir.Alias(ir.Count(body=flow), "observed")),
                    ir.Comparison(ir.Variable("observed"), ">", ir.Literal(threshold))),
        ))
        address = ("pipeline", 1, "projections", 1, "expression", "body", "pipeline", 1)
    return skeleton(source, "P10", form, work=True, address=address)


def optional_follow_skeleton(state="left_only", *, alias=False, work=True):
    item = optional_skeleton(state, work=work)
    parts, binding = list(item.source.pipeline), "left"
    if alias:
        binding = "saved"
        last = parts[-1]
        first_term, second_term = last.where.terms
        parts[-1] = replace(last, projections=(ir.Variable("u"), ir.Alias(ir.Variable("left"), binding), ir.Variable("right")),
                             where=ir.Boolean("AND", (replace(first_term, left=ir.Property(binding, "uid")), second_term)))
    endpoint = replace(component("left").right, variable=binding)
    parts.append(ir.Match(ir.Edge(endpoint, ir.Relationship(type="FOLLOW"), ir.Node("later", ("Later",)))))
    source = replace(item.source, pipeline=tuple(parts), returns=(ir.Property("u", "uid"),
        ir.Alias(ir.Property(binding, "uid"), "left_uid"), ir.Alias(ir.Property("right", "uid"), "right_uid"),
        ir.Alias(ir.Property("later", "uid"), "later_uid")))
    return replace(item, source=source)


def optional_property_null_skeleton():
    item = optional_skeleton("left_only", work=True)
    last = item.source.pipeline[-1]
    missing = ir.Comparison(ir.Property("left", "tag"), "IS NULL", ir.Literal(None))
    last = replace(last, where=ir.Boolean("AND", (*last.where.terms, missing)))
    source = replace(item.source, pipeline=(*item.source.pipeline[:-1], last),
                     returns=(*item.source.returns, ir.Alias(ir.Property("left", "tag"), "tag")))
    return replace(item, source=source)


def nested_optional_skeleton(selected="inner"):
    inner = ir.Query(None, None, (ir.Alias(ir.Literal(1), "c"),), pipeline=(
        ir.Match(ANCHOR), ir.Match(component("inner_left"), flag("inner_left"), optional=True),
        ir.Match(component("inner_right"), flag("inner_right"), optional=True),
    ))
    outer = optional_skeleton("left_only").source
    source = replace(outer, returns=(*outer.returns, ir.Alias(ir.Count(body=inner), "nested")))
    address = ("returns", 3, "expression", "body", "pipeline", 1) if selected == "inner" else ("pipeline", 1)
    return skeleton(source, work=True, address=address)


def per_input_population():
    root, left, right = ir.Node("u", ("InputRoot",)), ir.Node("a", ("LeftDomain",)), ir.Node("b", ("RightDomain",))
    source = ir.Query(root, None, (ir.Property("u", "uid"), ir.Property("a", "uid"), ir.Property("b", "uid")), matches=(
        ir.Match(ir.Edge(root, ir.Relationship(type="LEFT_R"), left)),
        ir.Match(ir.Edge(root, ir.Relationship(type="RIGHT_R"), right)),
    ))
    site, = find_mutation_sites(source, "P10", "shared_anchor")
    target, correspondence = plan_mutation_with_correspondence(source, site)
    demands = (MatchDemand(("query", "pattern"), "inputs", exact_matches=2),
               MatchDemand(("query", "matches", 0, "pattern"), "left", exact_matches=2),
               MatchDemand(("query", "matches", 1, "pattern"), "right", exact_matches=3))
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=3,
                                            demands=demands, correspondence=correspondence)
    return source, target, populated


def mixed_scalar_population():
    first, second = ir.Node("a", ("TypedDomain",)), ir.Node("b", ("TypedDomain",))
    source = ir.Query(first, ir.Comparison(ir.Property("a", "flag"), "=", ir.Literal(False)),
        (ir.Property("a", "uid"), ir.Property("b", "uid"), ir.Alias(ir.Property("a", "flag"), "a_flag"),
         ir.Alias(ir.Property("b", "flag"), "b_flag")), matches=(
        ir.Match(second, ir.Comparison(ir.Property("b", "flag"), "=", ir.Literal(0))),))
    site, = find_mutation_sites(source, "P10", "independent")
    target, correspondence = plan_mutation_with_correspondence(source, site)
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=3,
        correspondence=correspondence, demands=(MatchDemand(("query", "pattern"), "booleans", exact_matches=2),
            MatchDemand(("query", "matches", 0, "pattern"), "integers", exact_matches=3)))
    return source, target, populated


def solve(item, *, scale=7):
    solved = construct_state(item, scale=scale, data_seed=3)
    pair = instantiate_pair(item, solved)
    assert solved.population.expected_rows
    for query in (pair.query_a, pair.query_b):
        ir.validate_query(query)
        assert typed_bag(evaluate_query(query, solved.population.spec)) == typed_bag(solved.population.expected_rows)
    assert pair.query_a != pair.query_b
    assert pair.instantiation_record["uses_construction_assignment"] == solved.construction_record["assignment_digest"]
    assert pair.instantiation_record["graph_digest"] == solved.construction_record["graph_digest"]
    assert all(check in pair.checks for check in pair.bindings.get("required_state_checks", ()))
    return solved, pair


def test_two_absent_optional_components_preserve_one_null_extended_row():
    solved, _ = solve(optional_skeleton("neither"))
    assert solved.population.expected_rows == [{"u.uid": 1, "left.uid": None, "right.uid": None}]
    for name in ("left", "right"):
        matched = ir.Query(component(name), flag(name), (ir.Alias(ir.Literal(1), "c"),))
        assert evaluate_query(matched, solved.population.spec) == []


@pytest.mark.parametrize("state,left_missing,right_missing", [
    ("both", False, False), ("left_only", False, True), ("right_only", True, False),
])
def test_optional_presence_filters_are_solved_as_row_binding_facts(state, left_missing, right_missing):
    solved, _ = solve(optional_skeleton(state))
    rows = solved.population.expected_rows
    assert all((row["left.uid"] is None) == left_missing and (row["right.uid"] is None) == right_missing for row in rows)
    left = len({row["left.uid"] for row in rows if row["left.uid"] is not None})
    right = len({row["right.uid"] for row in rows if row["right.uid"] is not None})
    assert len(rows) == max(1, left) * max(1, right)


@pytest.mark.parametrize("form", ["independent", "shared_anchor"])
def test_mandatory_components_keep_their_actual_structure_and_per_anchor_product(form):
    item = matching_skeleton(form, threshold=5)
    solved, pair = solve(item)
    flow = item.source.pipeline[1].projections[1].expression.body
    first, second = flow.pipeline[1].pattern, flow.pipeline[2].pattern
    left_vars, right_vars = set(ir.pattern_variables(first)), set(ir.pattern_variables(second))
    assert left_vars.isdisjoint(right_vars) if form == "independent" else left_vars & right_vars == {"u"}
    if form == "shared_anchor":
        assert first.left.variable == second.left.variable == "u"
        assert first.right.variable != second.left.variable
    left_rows = evaluate_query(ir.Query(first, flag("left"), (ir.Property("left", "uid"),)), solved.population.spec)
    right_rows = evaluate_query(ir.Query(second, flag("right"), (ir.Property("right", "uid"),)), solved.population.spec)
    assert len(left_rows) * len(right_rows) == solved.population.expected_rows[0]["observed"] >= 6
    work = solved.construction_record["requirements"]["derivation"]["work"]
    assert work["repetitions"] >= 2 and work["potential_extra_candidate_work"] >= 7
    assert sum(edge["type"] in {"R_left", "R_right"} for edge in solved.population.spec.iter_edges()) >= 7


@pytest.mark.parametrize("shape", ["scans", "shared_edges"])
def test_overlapping_physical_domains_cannot_claim_incompatible_exact_component_counts(shape):
    first, second = ir.Node("a", ("Same",)), ir.Node("b", ("Same",))
    if shape == "scans":
        source = ir.Query(first, None, (ir.Property("a", "uid"), ir.Property("b", "uid")),
                          matches=(ir.Match(second),))
        form = "independent"
        paths = (("query", "pattern"), ("query", "matches", 0, "pattern"))
    else:
        source = ir.Query(ANCHOR, None, (ir.Property("a", "uid"), ir.Property("b", "uid")), matches=(
            ir.Match(ir.Edge(ANCHOR, ir.Relationship(type="SAME_R"), first)),
            ir.Match(ir.Edge(ANCHOR, ir.Relationship(type="SAME_R"), second)),
        ))
        form = "shared_anchor"
        paths = (("query", "matches", 0, "pattern"), ("query", "matches", 1, "pattern"))
    site, = find_mutation_sites(source, "P10", form)
    target, correspondence = plan_mutation_with_correspondence(source, site)
    demands = tuple(MatchDemand(path, "component_" + str(index), exact_matches=count)
                    for index, (path, count) in enumerate(zip(paths, (2, 3))))
    with pytest.raises(PopulationError, match="match|quantity|bound|domain|overlap|count|satisf|exact"):
        synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=3,
                                    correspondence=correspondence, demands=demands)


def test_shared_components_realize_two_by_three_matches_separately_for_each_input_anchor():
    source, target, populated = per_input_population()
    assert Counter(row["u.uid"] for row in populated.expected_rows) == {1: 6, 2: 6}
    assert typed_bag(evaluate_query(source, populated.spec)) == typed_bag(evaluate_query(target, populated.spec))
    edges = list(populated.spec.iter_edges())
    for uid in (1, 2):
        assert sum(edge["source"] == uid and edge["type"] == "LEFT_R" for edge in edges) == 2
        assert sum(edge["source"] == uid and edge["type"] == "RIGHT_R" for edge in edges) == 3


def test_boolean_false_and_integer_zero_domains_keep_separate_loaded_population_checks():
    _, _, populated = mixed_scalar_population()
    assert populated.spec.counts() == {"nodes": 5, "edges": 0}
    assert len(populated.expected_rows) == 6
    assert all(row["a_flag"] is False and type(row["b_flag"]) is int and row["b_flag"] == 0 for row in populated.expected_rows)
    counts = {(type(check["properties"]["flag"]).__name__, check["properties"]["flag"]): check["expected"]
              for check in populated.spec.state_checks() if check["kind"] == "node_count"
              and set(check["properties"]) == {"flag"}}
    assert counts == {("bool", False): 2, ("int", 0): 3}


@pytest.mark.parametrize("origin", ["outer", "local"])
def test_a_quantity_observation_traces_entity_aliases_to_their_true_input_domain(origin):
    outer_b = ir.Node("b", ("B",), (("id", ir.Literal(2)),))
    if origin == "outer":
        prefix = (ir.With((ir.Variable("u"), ir.Alias(ir.Variable("b"), "target"))),)
    else:
        prefix = (ir.With((ir.Variable("u"),)), ir.Match(ir.Node("local", ("B",))),
                  ir.With((ir.Variable("u"), ir.Alias(ir.Variable("local"), "target"))))
    inner = ir.Query(None, None, (ir.Alias(ir.Literal(1), "c"),), pipeline=(
        *prefix, ir.Match(ir.Edge(ANCHOR, ir.Relationship(type="R"), ir.Node("target", ("B",)))),
    ))
    source = ir.Query(ANCHOR, None, (ir.Alias(ir.Count(body=inner), "n"),), matches=(ir.Match(outer_b),))
    graph = GraphSpec("AST", 7, 3, version=2, include_reference=False, node_groups=(
        {"count": 1, "labels": ["OptionalAnchor"], "properties": {"uid": 1, "id": 1}},
        {"count": 1, "labels": ["B"], "properties": {"uid": 2, "id": 2}},
        {"count": 1, "labels": ["B"], "properties": {"uid": 3, "id": 3}},
    ), edge_groups=({"count": 1, "source": 1, "target": 3, "source_label": "OptionalAnchor", "target_label": "B",
                     "type": "R", "properties": {}},))
    assert evaluate_query(source, graph) == [{"n": 0 if origin == "outer" else 1}]
    address = ("returns", 0, "expression", "body", "pipeline", len(prefix), "pattern")
    fact = matching_quantity_observation(source, address, exact=1)
    probe = ir.Query(None, None, (ir.Alias(fact["expression"], "holds"),))
    assert evaluate_query(probe, graph) == [{"holds": origin == "local"}]
    assert fact["released_internal_target"] == ("target" if origin == "local" else None)


def test_overlapping_optional_domains_cannot_make_one_binding_null_and_the_other_present():
    first, second = ir.Node("a", ("Same",)), ir.Node("b", ("Same",))
    source = ir.Query(ANCHOR, None, (ir.Property("a", "uid"), ir.Property("b", "uid")), matches=(
        ir.Match(ir.Edge(ANCHOR, ir.Relationship(type="SAME_R"), first), optional=True),
        ir.Match(ir.Edge(ANCHOR, ir.Relationship(type="SAME_R"), second), optional=True),
    ))
    site, = find_mutation_sites(source, "P11", "optional_branches")
    target, correspondence = plan_mutation_with_correspondence(source, site)
    demands = (MatchDemand(("query", "matches", 0, "pattern"), "absent", exact_matches=0),
               MatchDemand(("query", "matches", 1, "pattern"), "present", exact_matches=2))
    fact = EntityBindingDemand(("query", "matches", 0, "pattern", "right"), "null")
    with pytest.raises(PopulationError, match="match|quantity|bound|domain|overlap|count|satisf|exact|binding"):
        synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=3,
                                    demands=demands, obligations=(fact,), correspondence=correspondence)


def assert_public_case(case):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    graph = GraphSpec.from_dict(case["graph_spec"])
    for side in ("a", "b"):
        query = ir.from_data(case["query_ir"][side])
        assert typed_bag(evaluate_query(query, graph)) == typed_bag(case["expected_rows"])
    first, second = case["construction_record"], case["instantiation_record"]
    assert first["assignment_digest"] == second["uses_construction_assignment"]
    assert first["graph_digest"] == second["graph_digest"]
    assert all(check in case["checks"] for check in case["bindings"].get("required_state_checks", ()))
    return ir.from_data(case["query_ir"]["a"]), graph


@pytest.mark.parametrize("form", ["independent", "shared_anchor"])
@pytest.mark.parametrize("branch", ["then", "otherwise"])
def test_public_matching_forms_keep_their_binding_policy_through_recursive_conditionals(form, branch):
    options = {"contexts": ["count", "with"], "threshold": 3, "form": form, "chain_length": 3,
               "conditional_depth": 2, "guard_kind": "count", "branch_path": [branch]}
    original = build_symbolic_skeleton("P10", options, 0)
    assert original.intents == () and original.mark is not None
    case = prepare_case(None, "P10", 7, 0, 3, composition=options)
    source, _ = assert_public_case(case)
    owners = [(path, query) for path, query in walk(source) if isinstance(query, ir.Query)
              and original.mark.address[:len(path)] == path]
    _, owner = max(owners, key=lambda item: len(item[0]))
    parts = ir.clauses(owner)
    producers = parts[1:] if form == "shared_anchor" else parts
    assert len(producers) == 3 and all(isinstance(part, ir.Match) and not part.optional for part in producers)
    names = [set(ir.pattern_variables(part.pattern)) for part in producers]
    if form == "independent":
        assert all(names[i].isdisjoint(names[j]) for i in range(3) for j in range(i + 1, 3))
    else:
        assert all(isinstance(part.pattern, ir.Edge) and part.pattern.left.variable == "u" for part in producers)
        assert len({part.pattern.right.variable for part in producers}) == 3
    assert next(value for name, value in case["expected_rows"][0].items() if name != "u.uid") >= 4


def test_unmoved_third_optional_can_supply_output_without_inflating_the_marked_work_prefix():
    options = {"contexts": ["count", "with"], "threshold": 9,
               "chain_length": 3, "match_state": "neither"}
    original = build_symbolic_skeleton("P11", options, 0)
    assert original.intents == ()
    facts = [goal for goal in original.goals if goal["kind"] == "binding"]
    assert len(facts) == 2 and all(goal["relation"] == "null" for goal in facts)
    case = prepare_case(None, "P11", 7, 0, 3, composition=options)
    _, _ = assert_public_case(case)
    assert next(value for name, value in case["expected_rows"][0].items() if name != "u.uid") >= 10
    work = case["construction_record"]["requirements"]["derivation"]["work"]
    assert work["repetitions"] == 1
    assert work["potential_extra_candidate_work"] >= 7


@pytest.mark.parametrize("state,left_missing,right_missing", [
    ("both", False, False), ("left_only", False, True),
    ("right_only", True, False), ("neither", True, True),
])
@pytest.mark.parametrize("length", [2, 4])
def test_public_optional_presence_targets_constrain_only_the_marked_pair(state, left_missing, right_missing, length):
    original = build_symbolic_skeleton("P11", {"contexts": [], "match_state": state, "chain_length": length}, 0)
    assert original.intents == () and original.mark is not None
    assert len([goal for goal in original.goals if goal["kind"] == "binding"]) == 2
    case = prepare_case(None, "P11", 7, 0, 3, composition={"contexts": [], "match_state": state, "chain_length": length})
    source, graph = assert_public_case(case)
    branches = [part for part in ir.clauses(source) if isinstance(part, ir.Match) and part.optional]
    assert len(branches) == length
    assert all(part.pattern.left.variable == "u" for part in branches)
    names = [part.pattern.right.variable + ".uid" for part in branches[:2]]
    for name, absent in zip(names, (left_missing, right_missing)):
        assert all((row[name] is None) == absent for row in case["expected_rows"])
    from query import parse_query
    checks = case["bindings"]["binding_checks"]
    assert checks and all(check in case["bindings"]["required_state_checks"] for check in checks)
    for check in checks:
        assert typed_bag(evaluate_query(parse_query(check["query"]), graph)) == typed_bag(check["expected_rows"])


@pytest.mark.parametrize("state", ["left_only", "neither"])
def test_optional_binding_goals_survive_conditional_else_and_outer_count_consumers(state):
    options = {"contexts": ["count", "with"], "threshold": 3, "match_state": state, "chain_length": 3,
               "conditional_depth": 2, "guard_kind": "exists", "branch_path": ["otherwise"]}
    case = prepare_case(None, "P11", 7, 0, 3, composition=options)
    _, graph = assert_public_case(case)
    assert next(value for name, value in case["expected_rows"][0].items() if name != "u.uid") >= 4
    from query import parse_query
    assert case["bindings"]["binding_checks"]
    for check in case["bindings"]["binding_checks"]:
        assert typed_bag(evaluate_query(parse_query(check["query"]), graph)) == typed_bag(check["expected_rows"])


def test_two_absent_optionals_cannot_supply_two_rows_to_an_outer_count():
    with pytest.raises((PopulationError, ValueError), match="bound|fixed|cardinality|row|goal|satisf"):
        prepare_case(None, "P11", 7, 0, 3, composition={"contexts": ["count", "with"], "threshold": 1,
            "match_state": "neither", "chain_length": 2})


@pytest.mark.parametrize("dependency", ["endpoint", "predicate", "optional_anchor"])
def test_optional_exchange_rejects_cross_branch_dependencies_or_a_nullable_anchor(dependency):
    item = optional_skeleton("both")
    parts = list(item.source.pipeline)
    if dependency == "endpoint":
        parts[2] = replace(parts[2], pattern=replace(parts[2].pattern, left=parts[1].pattern.right))
    elif dependency == "predicate":
        parts[2] = replace(parts[2], where=ir.Comparison(ir.Property("left", "uid"), "=", ir.Property("right", "uid")))
    else:
        parts[0] = replace(parts[0], optional=True)
    source = replace(item.source, pipeline=tuple(parts))
    ir.validate_query(source)
    assert find_mutation_sites(source, "P11", "optional_branches") == ()
    with pytest.raises(ValueError, match="site|applic|mark|pattern"):
        construct_state(replace(item, source=source), scale=7, data_seed=3)


def test_equality_to_null_cannot_make_an_optional_filter_true():
    item = optional_skeleton("neither")
    last = replace(item.source.pipeline[-1], where=ir.Comparison(ir.Property("left", "uid"), "=", ir.Literal(None)))
    source = replace(item.source, pipeline=(*item.source.pipeline[:-1], last))
    with pytest.raises(PopulationError, match="NULL|null|satisf|truth|goal|comparison"):
        construct_state(replace(item, source=source), scale=7, data_seed=3)


def test_missing_property_does_not_turn_a_present_optional_entity_into_a_null_binding():
    solved, _ = solve(optional_property_null_skeleton())
    assert all(row["left.uid"] is not None and row["right.uid"] is None and row["tag"] is None
               for row in solved.population.expected_rows)


def test_null_optional_import_cannot_be_rebound_to_make_a_later_mandatory_match_run():
    item = optional_skeleton("neither")
    follow = ir.Match(ir.Edge(component("left").right, ir.Relationship(type="FOLLOW"), ir.Node("later", ("Later",))))
    source = replace(item.source, pipeline=(*item.source.pipeline, follow),
                     returns=(*item.source.returns, ir.Property("later", "uid")))
    with pytest.raises(PopulationError, match="NULL|null|satisf|input|bound|binding|goal|row"):
        construct_state(replace(item, source=source), scale=7, data_seed=3)


@pytest.mark.parametrize("alias", [False, True])
def test_a_nonnull_optional_import_supplies_real_following_match_inputs(alias):
    solved, _ = solve(optional_follow_skeleton(alias=alias))
    rows = solved.population.expected_rows
    assert all(row["left_uid"] is not None and row["right_uid"] is None and row["later_uid"] is not None for row in rows)
    edges = {(edge["source"], edge["target"]) for edge in solved.population.spec.iter_edges() if edge["type"] == "FOLLOW"}
    assert all((row["left_uid"], row["later_uid"]) in edges for row in rows)


@pytest.mark.parametrize("selected", ["inner", "outer"])
def test_nested_optional_queries_keep_only_the_marked_exchange_and_real_mandatory_import(selected):
    item = nested_optional_skeleton(selected)
    solved, pair = solve(item)
    assert all(row["left.uid"] is not None and row["right.uid"] is None and row["nested"] >= 1
               for row in solved.population.expected_rows)
    if selected == "inner":
        assert pair.query_a.pipeline == pair.query_b.pipeline
        assert pair.query_a.returns[:3] == pair.query_b.returns[:3]
    else:
        assert pair.query_a.returns == pair.query_b.returns
    roots = {node["uid"] for node in solved.population.spec.iter_nodes() if "OptionalAnchor" in node["labels"]}
    assert roots == {1}


def test_both_absent_optionals_allow_single_input_candidate_work_but_not_nontrivial_repeat():
    item = optional_skeleton("neither", work=True)
    solved, _ = solve(item)
    assert solved.population.expected_rows == [{"u.uid": 1, "left.uid": None, "right.uid": None}]
    work = solved.construction_record["requirements"]["derivation"]["work"]
    assert work["repetitions"] == 1 and work["potential_extra_candidate_work"] >= 7
    item = replace(item, goals=tuple({**goal, "mode": "repeat"} if goal["kind"] == "work" else goal for goal in item.goals))
    with pytest.raises(PopulationError, match="repeat|satisf|bound|goal|work"):
        construct_state(item, scale=7, data_seed=3)


def test_an_uncertified_observer_of_both_candidate_regions_blocks_optional_work_growth():
    item = optional_skeleton("left_only", work=True)
    observers = []
    for name in ("left", "right"):
        edge = ir.Edge(ANCHOR, ir.Relationship(type="R_" + name), ir.Node("probe_" + name))
        read = ir.Count(body=ir.Query(edge, None, (ir.Alias(ir.Literal(1), "c"),)))
        observers.append(ir.Alias(read, "all_" + name))
    source = replace(item.source, returns=(*item.source.returns, *observers))
    with pytest.raises(PopulationError, match="growth|read|goal|closed|preserv|unprotect|quantity|matching|satisf"):
        construct_state(replace(item, source=source), scale=5001, data_seed=3)

