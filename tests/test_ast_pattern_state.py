"""Six-stage state seams: exact truth lowering, immutable graph and one mutation."""

from dataclasses import replace
from itertools import product

import pytest

import ir
from ast_pattern.contracts import EmbeddingPort, HostAst, Port, StageFailure, TerminalSlot
from ast_pattern.embedding import embed_patterns
from ast_pattern.instantiation import instantiate_query
from ast_pattern.mutation import mutate_target
from ast_pattern.state_builder import construct_graph_state, decompose_requirement
from executor import typed_bag
from population import evaluate_query


def embedded(pattern="P3", *, mode="count", options=None):
    port = Port()
    expression = ir.Count(body=port) if mode == "count" else ir.Exists(body=port)
    tree = ir.Query(None, None, (ir.Alias(expression, "result"),))
    host = HostAst(tree, (EmbeddingPort("port:0", ("returns", 0, "expression", "body"), reserved_budget=512),))
    return embed_patterns(host, selection_policy={"pattern": pattern, "options": options or {}}, embed_seed=0)


def constructed(pattern="P3", **kwargs):
    item = embedded(pattern, **kwargs)
    state, plan, record = construct_graph_state(item, 7, 3)
    return item, state, plan, record


@pytest.mark.parametrize("pattern", [*(f"P{i}" for i in range(1, 14)), *(f"S{i}" for i in range(1, 11))])
def test_every_local_rule_uses_the_same_state_materialize_mutate_seams(pattern):
    item, state, plan, construction = constructed(pattern)
    source = instantiate_query(item, state, plan)
    pair = mutate_target(source, state, item.target_id)
    assert construction["method_stage"] == "M3"
    assert source.record["method_stage"] == "M4"
    assert pair.mutation_record["method_stage"] == "M5"
    assert state.graph_spec["version"] == 2 and state.graph_spec["pattern"] == "AST"
    assert pair.mutation_record["graph_digest"] == state.graph_digest
    assert pair.mutation_record["binding_plan_digest"] == plan.plan_digest
    assert pair.mutation_record["target_id"] == item.target_id
    assert typed_bag(evaluate_query(pair.query_a, state.spec)) == typed_bag(state.population.expected_rows)
    assert typed_bag(evaluate_query(pair.query_b, state.spec)) == typed_bag(state.population.expected_rows)


def test_m4_has_no_mutation_side_effect_and_retains_full_outer_terminals(monkeypatch):
    tree = ir.Query(None, None, (ir.Alias(ir.Count(body=Port()), "n"),
                               ir.Alias(ir.Literal(TerminalSlot("outer:k", "integer", (3, 9))), "outer")))
    host = HostAst(tree, (EmbeddingPort("port:0", ("returns", 0, "expression", "body"), reserved_budget=512),))
    item = embed_patterns(host, selection_policy="P3")
    state, plan, _ = construct_graph_state(item, 7, 2)
    import mutator
    monkeypatch.setattr(mutator, "mutate", lambda *args, **kwargs: pytest.fail("M4 invoked mutation"))
    source = instantiate_query(item, state, plan)
    assert source.source.returns[1].expression.value == plan.slot_assignments["outer:k"]
    assert source.record["structure_unchanged"]


@pytest.mark.parametrize("tamper", ["plan", "graph", "facts", "population"])
def test_m4_rejects_mutable_construction_contract_drift(tamper):
    item, state, plan, _ = constructed()
    if tamper == "plan":
        plan.slot_assignments[next(iter(plan.slot_assignments))] = "tampered"
    elif tamper == "graph":
        state.graph_spec["node_groups"][0]["count"] += 1
    elif tamper == "facts":
        state.facts["construction_record"]["joint_binding"] = False
    else:
        state.population.expected_rows.append({"result": 99})
    with pytest.raises(StageFailure, match="contract_drift"):
        instantiate_query(item, state, plan)


@pytest.mark.parametrize("tamper", ["source", "bindings", "checks", "record", "target"])
def test_m5_rejects_changes_after_source_materialization(tamper):
    item, state, plan, _ = constructed("S7")
    source = instantiate_query(item, state, plan)
    target_id = item.target_id
    if tamper == "source":
        source = replace(source, source=ir.Query(None, None, (ir.Literal(0),)))
    elif tamper == "bindings":
        source.bindings["premise_checks"] = []
    elif tamper == "checks":
        source = replace(source, checks=())
    elif tamper == "record":
        source.record["binding_plan_digest"] = "invalid"
    else:
        target_id = "other-instance"
    with pytest.raises(StageFailure):
        mutate_target(source, state, target_id)


def truth(operator, values):
    if operator == "AND":
        return False if False in values else None if None in values else True
    return True if True in values else None if None in values else False


@pytest.mark.parametrize("operator", ["AND", "OR"])
@pytest.mark.parametrize("requested", [True, False, None])
def test_fixed_boolean_requirements_exactly_cover_the_three_valued_truth_table(operator, requested):
    node = ir.Boolean(operator, (ir.Property("a", "flag"), ir.Property("b", "flag")))
    alternatives = decompose_requirement(node, requested, {"id": "inputs", "quantifier": "forall_reachable"})
    observed = {tuple(task.required_observation["value"] for task in branch) for branch in alternatives.alternatives}
    expected = {values for values in product((True, False, None), repeat=2) if truth(operator, values) is requested}
    assert observed == expected
    assert all(task.quantifier == "forall_reachable" for branch in alternatives.alternatives for task in branch)


@pytest.mark.parametrize("requested,child", [(True, False), (False, True), (None, None)])
def test_not_inverts_known_boolean_targets_and_preserves_null(requested, child):
    result = decompose_requirement(ir.Negation(ir.Property("a", "flag")), requested)
    task, = result.alternatives[0]
    assert task.address == ("expression",)
    assert task.required_observation["value"] is child


def test_count_inequality_is_an_interval_union_not_a_single_range():
    count = ir.Count(body=ir.Query(ir.Node("u"), None, (ir.Literal(1),)))
    expression = ir.Comparison(count, "<>", ir.Literal(2))
    result = decompose_requirement(expression, True)
    assert [branch[0].required_observation["value"] for branch in result.alternatives] == [(0, 1), (3, None)]
    assert decompose_requirement(expression, None).analysis_status == "refuted"


def test_unknown_null_and_empty_input_are_distinct():
    value = ir.Property("u", "answer")
    known_null = decompose_requirement(value, None)
    assert known_null.analysis_status == "decomposed"
    assert known_null.alternatives[0][0].required_observation["value"] is None
    assert decompose_requirement(value, {"analysis_status": "unknown"}).analysis_status == "unknown"
    assert decompose_requirement(value, None, {"cardinality": 0, "requires_nonempty": True}).analysis_status == "refuted"
    assert decompose_requirement(ir.Call("unmodeled", ()), True).analysis_status == "unknown"


def test_m3_graph_budget_is_enforced_before_accepting_the_state():
    with pytest.raises(StageFailure, match="budget_exhausted"):
        construct_graph_state(embedded(), 7, 3, {"terminal_assignments": 1, "max_nodes": 1})


@pytest.mark.parametrize("seed", [0, 2, 5, 6])
def test_random_with_hosts_keep_all_terminal_bindings_and_complete_result_bags(seed):
    from ast_pattern.host_generator import generate_random_ast
    host = generate_random_ast({"clauses": ["MATCH", "WITH", "RETURN"],
                                "expression_operators": ["leaf", "count", "exists"]}, host_seed=seed)
    item = embed_patterns(host, selection_policy="P3", embed_seed=seed)
    state, plan, _ = construct_graph_state(item, 7, 3)
    source = instantiate_query(item, state, plan)
    pair = mutate_target(source, state, item.target_id)
    assert all(slot.slot_id in plan.slot_assignments for slot in host.slots)
    assert typed_bag(evaluate_query(pair.query_a, state.spec)) == typed_bag(state.population.expected_rows)
    assert typed_bag(evaluate_query(pair.query_b, state.spec)) == typed_bag(state.population.expected_rows)


def test_structural_holes_cannot_be_resolved_during_graph_search():
    from symbolic import Hole
    item = embedded()
    skeleton = replace(item.skeleton, source=Hole("unexpanded", "rows", (item.skeleton.source,)))
    item = replace(item, skeleton=skeleton)
    with pytest.raises(StageFailure, match="unexpanded_structure"):
        construct_graph_state(item, 7, 3)


@pytest.mark.parametrize("pattern", ["P4", "P10", "S7"])
def test_goal_checks_close_random_outer_with_bindings_instead_of_only_the_rule_anchor(pattern):
    from ast_pattern.host_generator import generate_random_ast
    from query import parse_query
    host = generate_random_ast(host_seed=3)
    item = embed_patterns(host, selection_policy=pattern, embed_seed=3)
    state, plan, _ = construct_graph_state(item, 7, 3)
    source = instantiate_query(item, state, plan)
    pair = mutate_target(source, state, item.target_id)
    assert pair.bindings["goal_checks"]
    for check in pair.bindings["goal_checks"]:
        query = parse_query(check["query"])
        ir.validate_query(query)
        assert typed_bag(evaluate_query(query, state.spec)) == typed_bag(check["expected_rows"])
