"""Independent counterexamples for shared recursive grammar and goal lowering."""

from dataclasses import fields, is_dataclass, replace
from itertools import islice

import pytest

import ir
from dependencies import find_mutation_sites, node_at
from executor import typed_bag
from main import prepare_case
from mutator import plan_mutation_with_correspondence
from population import PopulationError, evaluate_query, synthesize_relational_state
from requirements import derive_requirement_plans
from state import GraphSpec
from symbolic import MutationMark, SymbolicSkeleton, construct_state, instantiate_pair


ANCHOR = ir.Node("u", ("Anchor",), (("id", ir.Literal(1)),))


def walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from walk(item, (*address, index))


def region(name, *, where=None):
    root = ir.Node("root_" + name, ANCHOR.labels, ANCHOR.properties)
    leaf = ir.Node("leaf_" + name, ("Leaf_" + name,))
    return ir.Query(ir.Edge(root, ir.Relationship(type="R_" + name), leaf), where,
                    (ir.Alias(ir.Literal(1), "c"),))


def observe(flow, *, root_where=None):
    return ir.Query(None, None, (ir.Property("u", "uid"), ir.Variable("observed")), pipeline=(
        ir.Match(ANCHOR, root_where),
        ir.With((ir.Variable("u"), ir.Alias(ir.Count(body=flow), "observed")),
                ir.Comparison(ir.Variable("observed"), ">", ir.Literal(0))),
    ))


def skeleton(source, *, pattern="S3", replacement=None, seed=0):
    ir.validate_query(source)
    form = "conditional" if pattern == "S3" else "chain"
    sites = find_mutation_sites(source, pattern, form)
    site = max(sites, key=lambda candidate: len(candidate.address)) if pattern == "S3" else sites[0]
    return SymbolicSkeleton(pattern, form, source,
        replacement or (region("high") if pattern == "S3" else None), ANCHOR,
        {"contexts": (), "threshold": 0}, seed, (),
        MutationMark("acceptance-site", site.address),
        ({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed}))


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


def count_interval_skeleton(*, contradictory=False):
    counted = ir.Count(body=region("interval_guard"))
    number = ir.Variable("number")
    guard = ir.Boolean("AND", (
        ir.Comparison(number, ">", ir.Literal(2)),
        ir.Comparison(number, "<" if contradictory else "<>", ir.Literal(3)),
    ))
    conditional = ir.ConditionalQuery(guard, region("selected"), region("low"))
    source = ir.Query(None, None, (ir.Property("u", "uid"), number, ir.Variable("observed")), pipeline=(
        ir.Match(ANCHOR),
        ir.With((ir.Variable("u"), ir.Alias(counted, "number"))),
        ir.With((ir.Variable("u"), number, ir.Alias(ir.Count(body=conditional), "observed")),
                ir.Comparison(ir.Variable("observed"), ">", ir.Literal(0))),
    ))
    return skeleton(source)


def disjunction_skeleton():
    singleton = ir.Query(ir.Node("fixed", ANCHOR.labels, ANCHOR.properties), None,
                         (ir.Alias(ir.Literal(1), "c"),))
    guard = ir.Boolean("OR", (
        ir.Comparison(ir.Count(body=singleton), ">", ir.Literal(1)),
        ir.Comparison(ir.Count(body=region("or_guard")), ">", ir.Literal(3)),
    ))
    return skeleton(observe(ir.ConditionalQuery(guard, region("selected"), region("low"))))


def null_else_skeleton(*, physical_property=False):
    inner = ir.ConditionalQuery(ir.Exists(body=region("inner_guard")), region("selected"), region("low"))
    condition = (ir.Comparison(ir.Property("u", "nullable"), "=", ir.Literal(True))
                 if physical_property else ir.Literal(None))
    outer = ir.ConditionalQuery(condition, region("unreachable"), inner)
    where = ir.Comparison(ir.Property("u", "nullable"), "IS NULL", ir.Literal(None)) if physical_property else None
    return skeleton(observe(outer, root_where=where))


def local_with_guard_skeleton():
    matched = region("local_with_guard").pattern
    body = ir.Query(None, None, (ir.Alias(ir.Literal(1), "c"),), pipeline=(
        ir.Match(matched),
        ir.With((ir.Alias(ir.Property(matched.right.variable, "flag"), "flag"),)),
        ir.With((ir.Alias(ir.Variable("flag"), "ok"),),
                ir.Comparison(ir.Variable("ok"), "=", ir.Literal(True))),
    ))
    flow = ir.ConditionalQuery(ir.Exists(body=body), region("selected"), region("low"))
    return skeleton(observe(flow))


def local_u_count_guard_skeleton():
    body = ir.Query(ir.Edge(ANCHOR, ir.Relationship(type="LOCAL_U"), ir.Node("local_item", ("LocalItem",))),
                    None, (ir.Alias(ir.Literal(1), "c"),))
    conditional = ir.ConditionalQuery(ir.Comparison(ir.Count(body=body), ">", ir.Literal(0)),
                                      region("selected"), region("low"))
    return skeleton(conditional)


@pytest.mark.parametrize("pattern,branch_path", [
    ("P10", ["then", "otherwise"]), ("P10", ["otherwise", "then"]),
    ("S3", ["then", "otherwise"]), ("S3", ["otherwise", "then"]),
])
def test_public_matching_and_conditionals_share_mixed_recursive_productions(pattern, branch_path):
    options = {"contexts": ["count", "with"], "threshold": 3, "chain_length": 3,
               "conditional_depth": 3, "guard_kind": "count", "branch_path": branch_path}
    if pattern == "P10":
        options["form"] = "chain"
    case = prepare_case(None, pattern, 7, 0, 3, composition=options)
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    source, target = (ir.from_data(case["query_ir"][side]) for side in ("a", "b"))
    conditionals = [(path, value) for path, value in walk(source) if isinstance(value, ir.ConditionalQuery)]
    assert len(conditionals) == 3
    ordered = sorted(conditionals, key=lambda item: len(item[0]))
    for (outer_path, _), (inner_path, _), selected in zip(ordered, ordered[1:], branch_path):
        assert inner_path[len(outer_path)] == selected
    assert any(isinstance(value, ir.Query) and len(ir.clauses(value)) == 3
               and all(isinstance(part, ir.Match) and isinstance(part.pattern, ir.Edge) for part in ir.clauses(value))
               for _, value in walk(source))
    graph = GraphSpec.from_dict(case["graph_spec"])
    assert typed_bag(evaluate_query(source, graph)) == typed_bag(evaluate_query(target, graph)) == typed_bag(case["expected_rows"])


def test_intersection_of_count_lower_bound_and_excluded_value_constructs_four():
    solved, _ = solve(count_interval_skeleton())
    assert [row["number"] for row in solved.population.expected_rows] == [4]


def test_disjunction_backtracks_after_a_nonliteral_unsatisfiable_branch():
    item = disjunction_skeleton()
    solved, _ = solve(item)
    assert len(evaluate_query(region("or_guard"), solved.population.spec)) == 4


def test_a_guard_subquery_keeps_its_local_with_producers_in_scope():
    solved, pair = solve(local_with_guard_skeleton())
    assert pair.bindings["goal_checks"]
    from query import parse_query
    for check in pair.bindings["goal_checks"]:
        observed = parse_query(check["query"])
        ir.validate_query(observed)
        assert evaluate_query(observed, solved.population.spec) == check["expected_rows"]


def test_closed_guard_observation_uses_a_fresh_anchor_without_capturing_local_u():
    from query import parse_query
    solved, pair = solve(local_u_count_guard_skeleton())
    checks = pair.bindings["goal_checks"]
    assert checks
    count_checks = [check for check in checks if "COUNT {" in check["query"]]
    assert count_checks
    for check in count_checks:
        query = parse_query(check["query"])
        assert query.pattern.variable != "u"
        assert any(isinstance(value, ir.Node) and value.variable == "u" for _, value in walk(query.returns))
        assert evaluate_query(query, solved.population.spec) == check["expected_rows"]


def test_an_imported_guard_preserves_its_real_anchor_domain():
    from dependencies import premise_obligations, verify_rule_premises
    from query import parse_query, render_query
    from symbolic import _checked_observation
    flow = ir.ConditionalQuery(ir.Comparison(ir.Property("u", "enabled"), "=", ir.Literal(True)),
                               region("selected"), region("low"))
    source = observe(flow)
    site, = find_mutation_sites(source, "S3", "conditional")
    fact, = premise_obligations(source, site)
    proof = GraphSpec("AST", 1, 0, version=2, include_reference=False,
                      node_groups=({"count": 1, "labels": ["Anchor"], "properties": {"uid": 1, "id": 1, "enabled": True}},))
    check = _checked_observation(ANCHOR, fact, proof)
    assert parse_query(check["query"]).pattern == ANCHOR
    assert verify_rule_premises(source, site, {"premise_checks": [check], "required_state_checks": [check],
                                             "validation_graph_spec": proof.to_dict()})["proof_model_checked"]
    observed = parse_query(check["query"])
    changed = replace(observed, pattern=replace(ANCHOR, properties=(("id", ir.Literal(2)),)))
    bad = {"query": render_query(changed), "expected_rows": check["expected_rows"]}
    with pytest.raises(ValueError, match="anchor|domain|binding|premise|import"):
        verify_rule_premises(source, site, {"premise_checks": [bad], "required_state_checks": [bad],
                                            "validation_graph_spec": proof.to_dict()})


@pytest.mark.parametrize("physical_property", [False, True])
def test_null_condition_selects_else_and_keeps_the_observed_null_fact(physical_property):
    solved, pair = solve(null_else_skeleton(physical_property=physical_property))
    facts = solved.construction_record["requirements"]["obligations"]
    assert any(fact["value"] is None for fact in facts)
    assert any("IS NULL" in check["query"].upper() for check in pair.bindings["goal_checks"])
    if physical_property:
        roots = [node for node in solved.population.spec.iter_nodes() if "Anchor" in node["labels"]]
        assert roots and all(node["properties"]["nullable"] is None for node in roots)


def test_generated_else_recursion_accepts_a_null_outer_condition():
    from symbolic import build_symbolic_skeleton
    item = build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 2,
                                            "guard_kind": "count", "branch_path": ["otherwise"]}, 0)
    item = replace(item, source=replace(item.source, condition=ir.Literal(None)))
    solved, pair = solve(item)
    assert any(fact["value"] is None for fact in solved.construction_record["requirements"]["obligations"])
    assert any("IS NULL" in check["query"].upper() for check in pair.bindings["goal_checks"])


def test_contradictory_integer_interval_is_rejected_without_empty_success():
    with pytest.raises(PopulationError, match="goal|interval|bound|satisf|allocation"):
        construct_state(count_interval_skeleton(contradictory=True), scale=7, data_seed=3)


def test_work_search_changes_between_the_two_moved_reads_and_not_an_untouched_suffix():
    nodes = [ANCHOR, *(ir.Node(f"node_{index}", ("Shared",)) for index in range(1, 5))]
    parts = tuple(ir.Match(ir.Edge(left, ir.Relationship(type=f"R{index}"), right),
                          ir.Comparison(ir.Property(right.variable, "good"), "=", ir.Literal(True)))
                  for index, (left, right) in enumerate(zip(nodes, nodes[1:])))
    source = ir.Query(parts[0].pattern, parts[0].where,
                      tuple(ir.Property(node.variable, "uid") for node in nodes), matches=parts[1:])
    item = skeleton(source, pattern="P10")
    site = find_mutation_sites(source, "P10", "chain")[0]
    alternative, correspondence = plan_mutation_with_correspondence(source, site)
    axes = set()
    for seed in (0, 1):
        plans = derive_requirement_plans(source, alternative, site,
            goals=({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed}),
            scale=7, correspondence=correspondence)
        for plan in islice(plans, 40):
            chosen = set()
            for demand in plan.demands:
                if demand.candidate_matches is None:
                    continue
                prefix = 1 if demand.address[:1] == ("query",) else 2
                tree = source if prefix == 1 else alternative
                atom = node_at(tree, demand.address[prefix:])
                if isinstance(atom, ir.Edge):
                    chosen.add(atom.relationship.type)
            assert chosen <= {"R0", "R1"}, "an untouched suffix must not stand in for the marked order contrast"
            if chosen - axes:
                populated = synthesize_relational_state(source, alternatives=(alternative,), scale=7, data_seed=3,
                    demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
                assert populated.expected_rows
                assert typed_bag(evaluate_query(source, populated.spec)) == typed_bag(evaluate_query(alternative, populated.spec))
                vertices = {node["uid"]: node for node in populated.spec.iter_nodes()}
                edges = list(populated.spec.iter_edges())
                if "R0" in chosen:
                    bad_middle = {edge["target"] for edge in edges if edge["type"] == "R0"
                                  and vertices[edge["target"]]["properties"].get("good") is False}
                    extra_prefix_rows = [edge for edge in edges if edge["type"] == "R1"
                                         and edge["source"] in bad_middle
                                         and vertices[edge["target"]]["properties"].get("good") is True]
                    assert len(extra_prefix_rows) >= 7, "the moved later filter must have real extra producer rows"
                if "R1" in chosen:
                    good_middle = {edge["target"] for edge in edges if edge["type"] == "R0"
                                   and vertices[edge["target"]]["properties"].get("good") is True}
                    extra_candidates = [edge for edge in edges if edge["type"] == "R1"
                                        and edge["source"] in good_middle
                                        and vertices[edge["target"]]["properties"].get("good") is False]
                    assert len(extra_candidates) >= 7, "the repeated expansion must contain real rejected candidates"
                axes |= chosen
            if axes == {"R0", "R1"}:
                break
    assert axes == {"R0", "R1"}


def test_an_extra_unprotected_read_prevents_large_masked_growth():
    conditional = ir.ConditionalQuery(ir.Exists(body=region("guard")), region("selected"), region("low"))
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=conditional), "selected_rows"),
                                  ir.Alias(ir.Count(body=region("high")), "unprotected_rows")))
    with pytest.raises(PopulationError, match="goal|growth|visible|closure|shared state"):
        construct_state(skeleton(source), scale=5001, data_seed=3)


def test_long_replacement_offers_more_than_one_actual_masked_work_axis():
    from symbolic import _assignment, _resolve, build_symbolic_skeleton

    item = build_symbolic_skeleton("S3", {"contexts": [], "chain_length": 3,
                                            "conditional_depth": 1, "guard_kind": "count"}, 0)
    assignment = _assignment(item)
    source, replacement = _resolve(item.source, assignment), _resolve(item.replacement, assignment)
    site = next(candidate for candidate in find_mutation_sites(source, "S3", "conditional")
                if candidate.address == item.mark.address)
    alternative, correspondence = plan_mutation_with_correspondence(source, site, replacement=replacement)
    axes = set()
    for seed in (0, 1, 2):
        plans = derive_requirement_plans(source, alternative, site,
            goals=({"kind": "output", "min_rows": 1}, {"kind": "work", "mode": "auto", "seed": seed}),
            scale=7, correspondence=correspondence)
        for plan in islice(plans, 40):
            chosen = {tuple(demand.address) for demand in plan.demands if demand.scale_matches}
            if not chosen - axes:
                continue
            assert all(address[:2] == ("alternatives", 0) and
                       address[2:2 + len(site.address)] == site.address for address in chosen)
            populated = synthesize_relational_state(source, alternatives=(alternative,), scale=7, data_seed=3,
                demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
            assert typed_bag(evaluate_query(source, populated.spec)) == typed_bag(evaluate_query(alternative, populated.spec))
            axes |= chosen
            if len(axes) >= 2:
                break
    assert len(axes) >= 2


@pytest.mark.parametrize("path", [[[]], [{}]])
def test_branch_path_cannot_hide_a_non_identifier_container(path):
    from symbolic import build_symbolic_skeleton
    with pytest.raises(ValueError, match="branch_path"):
        build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 2, "branch_path": path}, 0)


def test_outer_binding_cannot_be_captured_by_a_local_with_alias():
    body = ir.Query(None, None, (ir.Variable("u"),), pipeline=(
        ir.With((ir.Alias(ir.Literal(1), "u"),)),
    ))
    query = ir.Query(ANCHOR, None, (ir.Alias(ir.Exists(body=body), "value"),))
    with pytest.raises(ValueError):
        ir.validate_query(query)


@pytest.mark.parametrize("name", ["n1", "guard_root_0", "item"])
def test_row_grammar_allocates_names_around_an_existing_anchor(name):
    from row_grammar import RowGrammar
    from symbolic import Symbol, _assignment, _flag, _region, _resolve
    anchor = ir.Node(name, ("Anchor",), (("id", ir.Literal(1)),))
    grammar = RowGrammar(anchor, seed=0, symbol=Symbol, flag=_flag, region=_region)
    rows = grammar.expand(length=2, depth=2, guard_kind="count", branch_path=["otherwise"], projection="constant")
    shell = SymbolicSkeleton("S3", "conditional", rows.source, grammar.matching(1, name="replacement", projection="constant"),
                              anchor, {"contexts": ()}, 0)
    source = _resolve(rows.source, _assignment(shell))
    outer = ir.Query(anchor, None, (ir.Alias(ir.Count(body=source), "observed"),))
    ir.validate_query(outer)
    assert all(value.left.variable != value.right.variable for _, value in walk(source) if isinstance(value, ir.Edge))
