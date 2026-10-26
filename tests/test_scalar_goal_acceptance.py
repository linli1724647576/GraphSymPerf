"""Independent scalar AST cases for semantic goals and guarded graph work."""

from dataclasses import fields, is_dataclass, replace

import pytest

import ir
from dependencies import find_mutation_sites, node_at, replace_at
from executor import typed_bag
from main import prepare_case
from population import PopulationError, evaluate_query
from state import GraphSpec
from symbolic import MutationMark, SymbolicSkeleton, build_symbolic_skeleton, construct_state, instantiate_pair


ANCHOR = ir.Node("u", ("ScalarAnchor",), (("id", ir.Literal(1)),))
FORMS = {"P4": "and", "P5": "or", "P13": "case_dual", "S1": "absorbing_or",
         "S2": "absorbing_and", "S4": "case_guard", "S7": "coalesce"}
UNSPECIFIED = object()


def walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from walk(item, (*address, index))


def region(name="work"):
    return ir.Query(ir.Edge(ANCHOR, ir.Relationship(type="R_" + name),
                            ir.Node("leaf_" + name, ("ScalarLeaf_" + name,))),
                    None, (ir.Alias(ir.Literal(1), "c"),))


def scalar_skeleton(expression, pattern, *, replacement=None, where=None, source=None, address=None,
                    seed=0, required_value=UNSPECIFIED):
    source = source or ir.Query(ANCHOR, where, (ir.Property("u", "uid"), ir.Alias(expression, "value")))
    ir.validate_query(source)
    sites = find_mutation_sites(source, pattern, FORMS[pattern])
    assert sites, "the independent source must contain the requested native scalar law"
    site = next(item for item in sites if item.address == address) if address is not None else sites[0]
    goals = ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed})
    if required_value is not UNSPECIFIED:
        goals += ({"kind": "value", "address": ("query", "returns", 1, "expression"),
                   "relation": "equal", "value": required_value},)
    return SymbolicSkeleton(pattern, FORMS[pattern], source, replacement, ANCHOR,
                            {"contexts": (), "threshold": 0}, seed, (),
                            MutationMark("scalar-acceptance", site.address), goals)


def solve(item, *, expected, scale=7):
    solved = construct_state(item, scale=scale, data_seed=3)
    pair = instantiate_pair(item, solved)
    assert typed_bag(solved.population.expected_rows) == typed_bag(expected)
    for query in (pair.query_a, pair.query_b):
        ir.validate_query(query)
        assert typed_bag(evaluate_query(query, solved.population.spec)) == typed_bag(expected)
    assert pair.query_a != pair.query_b
    assert pair.instantiation_record["uses_construction_assignment"] == solved.construction_record["assignment_digest"]
    assert pair.instantiation_record["graph_digest"] == solved.construction_record["graph_digest"]
    assert all(check in pair.checks for check in pair.bindings.get("required_state_checks", ()))
    return solved, pair


def coalesce_skeleton(value=0, *, physical=False):
    high = ir.Count(body=region())
    if type(value) is bool:
        fallback, replacement = ir.Literal(True), ir.Comparison(high, ">", ir.Literal(0))
    elif type(value) is str:
        fallback = ir.Literal("old fallback")
        replacement = ir.Case(ir.Comparison(high, ">", ir.Literal(0)),
                              ir.Literal("new fallback"), ir.Literal("empty fallback"))
    elif type(value) is float:
        fallback = ir.Literal(9.0)
        replacement = ir.Case(ir.Comparison(high, ">", ir.Literal(0)), ir.Literal(3.5), ir.Literal(0.0))
    else:
        fallback, replacement = ir.Literal(9), high
    prefix = ir.Property("u", "prefix") if physical else ir.Literal(None)
    selected = ir.Property("u", "middle") if physical else ir.Literal(value)
    expression = ir.Call("coalesce", (prefix, selected, fallback))
    return scalar_skeleton(expression, "S7", replacement=replacement, required_value=value,
                            address=("returns", 1, "expression", "args", 2))


def case_dual_skeleton(condition=None, value=11):
    high = ir.Count(body=region())
    if type(value) is bool:
        expensive = ir.Comparison(high, ">", ir.Literal(0))
    elif type(value) is str:
        expensive = ir.Case(ir.Comparison(high, ">", ir.Literal(0)),
                            ir.Literal("costly"), ir.Literal("empty"))
    elif type(value) is float:
        expensive = ir.Case(ir.Comparison(high, ">", ir.Literal(0)), ir.Literal(3.5), ir.Literal(0.0))
    else:
        expensive = high
    expression = ir.Case(ir.Literal(condition), expensive, ir.Literal(value))
    return scalar_skeleton(expression, "P13", required_value=value)


def nested_coalesce_skeleton(selected="inner"):
    inner = ir.Call("coalesce", (ir.Literal(None), ir.Literal(0), ir.Literal(1)))
    outer = ir.Call("coalesce", (ir.Literal(None), inner, ir.Literal(2)))
    location = ("returns", 1, "expression", *(('args', 1) if selected == "inner" else ()), "args", 2)
    return scalar_skeleton(outer, "S7", replacement=ir.Count(body=region()),
                            required_value=0, address=location)


def nested_case_skeleton(selected="inner"):
    inner = ir.Case(ir.Comparison(ir.Property("u", "inner_flag"), "=", ir.Literal(True)),
                    ir.Literal(0), ir.Literal(1))
    outer = ir.Case(ir.Comparison(ir.Property("u", "outer_flag"), "=", ir.Literal(True)),
                    ir.Literal(23), inner)
    location = ("returns", 1, "expression", *(('otherwise',) if selected == "inner" else ()), "otherwise")
    return scalar_skeleton(outer, "S4", replacement=ir.Count(body=region()),
                            required_value=0 if selected == "inner" else 23, address=location)


def boolean_skeleton(pattern="P4"):
    guard = ir.Comparison(ir.Property("u", "enabled"), "=", ir.Literal(True))
    low = ir.Literal(pattern == "S2") if pattern.startswith("S") else ir.Exists(body=region())
    expression = ir.Boolean("AND" if pattern in {"P4", "S2"} else "OR", (guard, low))
    expected = pattern not in {"P4", "S2"}
    return scalar_skeleton(expression, pattern,
                            replacement=ir.Comparison(ir.Count(body=region("replacement")), ">", ir.Literal(0))
                            if pattern.startswith("S") else None, required_value=expected)


def alias_rebinding_skeleton():
    expression = ir.Call("coalesce", (ir.Variable("x"), ir.Variable("saved"), ir.Literal(9)))
    source = ir.Query(None, None, (ir.Property("u", "uid"), ir.Alias(expression, "value")), pipeline=(
        ir.Match(ANCHOR),
        ir.With((ir.Variable("u"), ir.Alias(ir.Property("u", "middle"), "x"))),
        ir.With((ir.Variable("u"), ir.Alias(ir.Variable("x"), "saved"), ir.Alias(ir.Literal(None), "x"))),
    ))
    return scalar_skeleton(expression, "S7", source=source, replacement=ir.Count(body=region()),
                            required_value=0, address=("returns", 1, "expression", "args", 2))


def nested_query_skeleton(position="projection", *, blocked=False, optional=False):
    middle = ir.Node("nested_v", ("NestedV",))
    leaf = ir.Node("nested_w", ("NestedW",))
    inner = ir.Query(ir.Edge(middle, ir.Relationship(type="R_nested_work"), leaf),
                     None, (ir.Alias(ir.Literal(1), "c"),))
    prefix = ir.Edge(ANCHOR, ir.Relationship(type="R_nested_prefix"), middle)
    if position == "projection":
        outer = ir.Query(None, None, (ir.Alias(ir.Count(body=inner), "nested"),), pipeline=(
            ir.Match(prefix, ir.Literal(not blocked), optional=optional),
        ))
    else:
        tail = ir.Edge(middle, ir.Relationship(type="R_nested_tail"), ir.Node("nested_t", ("NestedT",)))
        outer = ir.Query(None, None, (ir.Alias(ir.Literal(1), "c"),), pipeline=(
            ir.Match(prefix, ir.Literal(not blocked)),
            ir.Match(tail, ir.Comparison(ir.Count(body=inner), ">", ir.Literal(0))),
        ))
    item = coalesce_skeleton(0)
    seed = 0 if blocked and position == "projection" and not optional else 1
    goals = tuple({**goal, "seed": seed} if goal["kind"] == "work" else goal for goal in item.goals)
    return replace(item, replacement=ir.Count(body=outer), seed=seed, goals=goals)


@pytest.mark.parametrize("value", [0, False, "", 1.5])
@pytest.mark.parametrize("physical", [False, True])
def test_a_nonnull_coalesce_prefix_preserves_its_exact_scalar_value(value, physical):
    solved, pair = solve(coalesce_skeleton(value, physical=physical), expected=[{"u.uid": 1, "value": value}])
    assert len(evaluate_query(region(), solved.population.spec)) >= 7
    assert pair.bindings["goal_checks"]
    if physical:
        anchors = [node for node in solved.population.spec.iter_nodes() if "ScalarAnchor" in node["labels"]]
        assert anchors
        assert all(node["properties"]["prefix"] is None for node in anchors)
        assert all(type(node["properties"]["middle"]) is type(value)
                   and node["properties"]["middle"] == value for node in anchors)


@pytest.mark.parametrize("condition", [False, None])
@pytest.mark.parametrize("value", [11, "kept", False, None, 1.5])
def test_case_dual_uses_a_null_safe_complement_and_preserves_general_else_values(condition, value):
    solved, _ = solve(case_dual_skeleton(condition, value), expected=[{"u.uid": 1, "value": value}])
    assert len(evaluate_query(region(), solved.population.spec)) >= 7


@pytest.mark.parametrize("selected", ["inner", "outer"])
def test_nested_coalesce_marks_change_only_the_selected_lexical_fallback(selected):
    item = nested_coalesce_skeleton(selected)
    solved, pair = solve(item, expected=[{"u.uid": 1, "value": 0}])
    other = ("returns", 1, "expression", *(('args', 1) if selected == "outer" else ()), "args", 2)
    assert node_at(pair.query_b, item.mark.address) == item.replacement
    assert node_at(pair.query_b, other) == node_at(pair.query_a, other)
    assert len(evaluate_query(region(), solved.population.spec)) >= 7


@pytest.mark.parametrize("selected", ["inner", "outer"])
def test_nested_case_marks_preserve_ancestor_selection_and_replace_only_the_marked_else(selected):
    item = nested_case_skeleton(selected)
    value = 0 if selected == "inner" else 23
    solved, pair = solve(item, expected=[{"u.uid": 1, "value": value}])
    source, target = pair.query_a.returns[1].expression, pair.query_b.returns[1].expression
    assert target.condition == source.condition and target.then == source.then
    assert node_at(pair.query_b, item.mark.address) == item.replacement
    assert len(evaluate_query(region(), solved.population.spec)) >= 7
    root, = (node for node in solved.population.spec.iter_nodes() if "ScalarAnchor" in node["labels"])
    if selected == "inner":
        assert root["properties"]["outer_flag"] is not True
        assert root["properties"]["inner_flag"] is True
    else:
        assert root["properties"]["outer_flag"] is True


@pytest.mark.parametrize("pattern", ["P4", "P5", "S1", "S2"])
def test_boolean_commutation_and_absorption_share_guarded_scalar_work(pattern):
    expected = pattern not in {"P4", "S2"}
    solved, _ = solve(boolean_skeleton(pattern), expected=[{"u.uid": 1, "value": expected}])
    if pattern.startswith("S"):
        assert len(evaluate_query(region("replacement"), solved.population.spec)) >= 7
    else:
        candidates = [edge for edge in solved.population.spec.iter_edges() if edge["type"] == "R_work"]
        assert len(candidates) >= 7
        assert len(evaluate_query(region(), solved.population.spec)) == 0


@pytest.mark.parametrize("pattern", ["S1", "S2"])
def test_scalar_low_to_high_amplifies_the_replacement_even_when_low_already_reads_a_graph(pattern):
    item = boolean_skeleton(pattern)
    source = replace_at(item.source, item.mark.address, ir.Exists(body=region("low")))
    item = replace(item, source=source)
    value = pattern == "S1"
    solved, _ = solve(item, expected=[{"u.uid": 1, "value": value}])
    assert len(evaluate_query(region("replacement"), solved.population.spec)) >= 7
    assert len(evaluate_query(region("low"), solved.population.spec)) < 7


def test_rebound_scalar_names_follow_their_lexical_producers_and_close_loaded_facts():
    from query import parse_query
    solved, pair = solve(alias_rebinding_skeleton(), expected=[{"u.uid": 1, "value": 0}])
    for check in pair.bindings["goal_checks"]:
        query = parse_query(check["query"])
        ir.validate_query(query)
        assert typed_bag(evaluate_query(query, solved.population.spec)) == typed_bag(check["expected_rows"])
    root, = (node for node in solved.population.spec.iter_nodes() if "ScalarAnchor" in node["labels"])
    assert type(root["properties"]["middle"]) is int and root["properties"]["middle"] == 0


def test_a_marked_boolean_inside_case_and_coalesce_activates_its_real_ancestor_branch():
    kernel = boolean_skeleton("P4").source.returns[1].expression
    wrapped = ir.Case(ir.Comparison(ir.Property("u", "route"), "=", ir.Literal(True)),
                      ir.Call("coalesce", (ir.Literal(None), kernel, ir.Literal(False))), ir.Literal(False))
    item = scalar_skeleton(wrapped, "P4", required_value=False,
                            address=("returns", 1, "expression", "then", "args", 1))
    solved, _ = solve(item, expected=[{"u.uid": 1, "value": False}])
    root, = (node for node in solved.population.spec.iter_nodes() if "ScalarAnchor" in node["labels"])
    assert root["properties"]["route"] is True
    assert sum(edge["type"] == "R_work" for edge in solved.population.spec.iter_edges()) >= 7


@pytest.mark.parametrize("position", ["projection", "where"])
def test_a_nested_scalar_read_has_real_outer_query_bindings_before_its_work_grows(position):
    item = nested_query_skeleton(position)
    solved, _ = solve(item, expected=[{"u.uid": 1, "value": 0}])
    outer_rows = evaluate_query(item.replacement.body, solved.population.spec)
    assert outer_rows
    if position == "projection":
        assert all(row["nested"] >= 7 for row in outer_rows)
    edges = list(solved.population.spec.iter_edges())
    reachable = {edge["target"] for edge in edges if edge["type"] == "R_nested_prefix" and edge["source"] == 1}
    if position == "where":
        reachable &= {edge["source"] for edge in edges if edge["type"] == "R_nested_tail"}
    assert reachable
    assert any(sum(edge["source"] == uid and edge["type"] == "R_nested_work" for edge in edges) >= 7
               for uid in reachable)


@pytest.mark.parametrize("position,optional", [("projection", False), ("where", False), ("projection", True)])
def test_nested_scalar_work_cannot_skip_a_false_outer_query_prefix_or_use_a_null_import(position, optional):
    item = nested_query_skeleton(position, blocked=True, optional=optional)
    with pytest.raises(PopulationError, match="work|goal|mask|branch|route|reach|optional|input|bound|satisf"):
        construct_state(item, scale=7, data_seed=3)


def test_an_unprotected_observer_of_the_scalar_growth_domain_is_rejected_at_large_scale():
    item = coalesce_skeleton(0)
    source = replace(item.source, returns=(*item.source.returns,
                                          ir.Alias(ir.Count(body=region()), "unprotected")))
    with pytest.raises(PopulationError, match="growth|read|goal|closed|preserv|unprotect"):
        construct_state(replace(item, source=source), scale=5001, data_seed=3)


@pytest.mark.parametrize("dead_operator", ["case", "coalesce"])
def test_an_unreachable_read_inside_high_cannot_supply_the_low_to_high_work_goal(dead_operator):
    item = coalesce_skeleton(0)
    read = ir.Count(body=region())
    high = (ir.Case(ir.Literal(False), read, ir.Literal(0)) if dead_operator == "case"
            else ir.Call("coalesce", (ir.Literal(0), read)))
    with pytest.raises(PopulationError, match="work|goal|mask|branch|route|reach"):
        construct_state(replace(item, replacement=high), scale=7, data_seed=3)


def test_a_null_coalesce_middle_cannot_satisfy_the_nonnull_mutation_premise():
    item = coalesce_skeleton(0)
    source = replace_at(item.source, ("returns", 1, "expression", "args", 1), ir.Literal(None))
    with pytest.raises((PopulationError, ValueError), match="nonnull|non.null|literal|goal|satisf|site|applic"):
        construct_state(replace(item, source=source), scale=7, data_seed=3)


def test_a_requested_scalar_value_conflicting_with_the_selected_literal_is_rejected():
    item = coalesce_skeleton(0)
    item = replace(item, goals=(*item.goals[:-1], {**item.goals[-1], "value": 1}))
    with pytest.raises(PopulationError, match="value|literal|goal|satisf"):
        construct_state(item, scale=7, data_seed=3)


def test_a_null_case_result_cannot_also_satisfy_a_nonnull_value_goal():
    item = case_dual_skeleton(False, None)
    item = replace(item, goals=(*item.goals, {"kind": "value", "address": ("query", "returns", 1, "expression"),
                                           "relation": "nonnull"}))
    with pytest.raises(PopulationError, match="nonnull|non.null|literal|goal|satisf"):
        construct_state(item, scale=7, data_seed=3)


def test_a_dropped_entity_name_cannot_be_captured_by_a_later_scalar_projection():
    expression = ir.Call("coalesce", (ir.Literal(None), ir.Property("u", "middle"), ir.Literal(9)))
    source = ir.Query(None, None, (ir.Alias(expression, "value"),), pipeline=(
        ir.Match(ANCHOR), ir.With((ir.Alias(ir.Literal(0), "u"),)),
    ))
    with pytest.raises(ValueError, match="property|entity|node|bound|binding"):
        ir.validate_query(source)


PUBLIC_CASES = [
    ("P4", "boolean", False, ["or", "case_then", "coalesce"], 2),
    ("P5", "boolean", True, ["and", "case_else", "coalesce"], 3),
    ("P13", "integer", 0, ["case_else", "coalesce"], 3),
    ("S1", "boolean", True, ["and", "case_then"], 2),
    ("S2", "boolean", False, ["or", "case_else"], 2),
    ("S4", "float", 1.5, ["coalesce", "case_then"], 3),
    ("S7", "string", "", ["case_else", "coalesce"], 2),
]


@pytest.mark.parametrize("contexts", [[], ["count", "with"]])
@pytest.mark.parametrize("pattern,kind,value,wrappers,length", PUBLIC_CASES)
def test_public_scalar_grammar_composes_typed_values_matching_bodies_and_row_consumers(pattern, kind, value, wrappers, length, contexts):
    options = {"contexts": contexts, "scalar_type": kind, "scalar_value": value,
               "scalar_contexts": wrappers, "chain_length": length}
    skeleton = build_symbolic_skeleton(pattern, options, 0)
    assert skeleton.intents == ()
    assert skeleton.mark is not None
    assert any(goal["kind"] == "work" and goal["mode"] == "auto" for goal in skeleton.goals)
    value_goal, = (goal for goal in skeleton.goals if goal["kind"] == "value")
    assert type(value_goal["value"]) is type(value) and value_goal["value"] == value
    case = prepare_case(None, pattern, 7, 0, 3, composition=options)
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    graph = GraphSpec.from_dict(case["graph_spec"])
    queries = [ir.from_data(case["query_ir"][side]) for side in ("a", "b")]
    for query in queries:
        assert typed_bag(evaluate_query(query, graph)) == typed_bag(case["expected_rows"])
    bodies = [part.body for query in queries for _, part in walk(query)
              if isinstance(part, (ir.Count, ir.Exists)) and part.body is not None]
    assert any(len(ir.clauses(body)) == length and all(isinstance(part, ir.Match) for part in ir.clauses(body))
               for body in bodies)
    if not contexts:
        name = "predicate" if pattern == "P4" else "value"
        assert typed_bag(case["expected_rows"]) == typed_bag([{"u.uid": 1, name: value}])
    assert case["construction_record"]["assignment_digest"] == case["instantiation_record"]["uses_construction_assignment"]
    assert case["construction_record"]["graph_digest"] == case["instantiation_record"]["graph_digest"]
    assert all(check in case["checks"] for check in case["bindings"].get("required_state_checks", ()))


@pytest.mark.parametrize("pattern,extra", [
    ("P4", {"scalar_type": "integer"}),
    ("S7", {"scalar_type": "integer", "scalar_value": False}),
    ("S4", {"scalar_type": "float", "scalar_value": 1}),
    ("P13", {"scalar_type": "string", "scalar_contexts": ["and"]}),
    ("S7", {"scalar_depth": True}),
    ("S4", {"scalar_depth": 5}),
    ("S7", {"scalar_contexts": [[]]}),
    ("S1", {"scalar_depth": 1, "scalar_contexts": []}),
    ("P3", {"scalar_type": "integer"}),
    ("P13", {"scalar_value": [1]}),
])
def test_public_scalar_options_reject_type_budget_and_context_conflicts(pattern, extra):
    with pytest.raises(ValueError):
        prepare_case(None, pattern, 7, 0, 3, composition={"contexts": [], **extra})


def test_a_correlated_scalar_kernel_cannot_duplicate_its_bound_anchor_to_pass_count_greater_than_one():
    options = {"contexts": ["count", "with"], "scalar_depth": 0,
               "scalar_type": "integer", "scalar_value": 0}
    accepted = prepare_case(None, "S7", 7, 0, 3, composition={**options, "threshold": 0})
    assert accepted["expected_rows"] == [{"u.uid": 1, "gsp_value_0": 1}]
    with pytest.raises((PopulationError, ValueError), match="bound|fixed|cardinality|row|goal|satisf"):
        prepare_case(None, "S7", 7, 0, 3, composition={**options, "threshold": 1})

