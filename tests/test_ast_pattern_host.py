"""M1 grammar, scope, replay and budget contracts without any graph input."""

import inspect
import json
from dataclasses import fields, is_dataclass, replace

import pytest

import ir
from ast_pattern import host_generator
from ast_pattern.contracts import HostAst, Port, ScopeState, StageFailure, TerminalSlot
from ast_pattern.host_generator import (advance_scope, expand_host_structure,
                                        generate_random_ast, sample_clause_skeleton,
                                        validate_abstract_types_and_scopes)


def walk(value):
    yield value
    if isinstance(value, (TerminalSlot, Port)):
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def test_public_host_boundary_has_no_rule_or_graph_input():
    assert tuple(inspect.signature(generate_random_ast).parameters) == (
        "grammar", "capability_profile", "budget", "host_seed")
    source = inspect.getsource(host_generator)
    for forbidden in ("import symbolic", "import pattern_registry", "from symbolic", "from .pattern_registry",
                      "GraphManager", "varToProperties", "build_symbolic_skeleton", "_BUILDERS"):
        assert forbidden not in source
    first = generate_random_ast(host_seed=17)
    second = generate_random_ast(host_seed=17)
    assert first.to_data() == second.to_data()
    assert first.host_digest == second.host_digest


def test_skeleton_is_a_separate_reproducible_first_pass():
    skeleton = sample_clause_skeleton(clause_budget=4, seed=11)
    assert skeleton == sample_clause_skeleton(clause_budget=4, seed=11)
    assert skeleton.clauses[-1]["kind"] == "RETURN"
    assert 1 <= len(skeleton.clauses) <= 4
    assert all(item["syntax_slots"] for item in skeleton.clauses)
    host = expand_host_structure(skeleton, seed=8)
    assert host.clause_skeleton == skeleton
    assert host.expansion_trace
    assert all(not isinstance(item, dict) for item in walk(host.tree))


def test_return_count_host_needs_no_initial_match_and_keeps_only_a_row_port():
    host = generate_random_ast(grammar={"clauses": ["RETURN"], "port_kinds": ["rows"],
                                       "return_type": "integer"},
                               budget={"expression_depth": 0}, host_seed=0)
    assert isinstance(host.tree, ir.Query) and not ir.clauses(host.tree)
    assert isinstance(host.tree.returns[0].expression, ir.Count)
    assert isinstance(host.tree.returns[0].expression.body, Port)
    assert len(host.ports) == 1 and host.ports[0].kind == "rows"
    assert host.ports[0].reserved_budget == 128
    assert len([x for x in walk(host.tree) if isinstance(x, Port)]) == 1


@pytest.mark.parametrize("kind,expected", [("scalar", "integer"), ("predicate", "boolean"), ("rows", "rows")])
def test_ports_preserve_their_typed_interface(kind, expected):
    host = generate_random_ast(grammar={"port_kinds": [kind]}, host_seed=9)
    port = next(x for x in walk(host.tree) if isinstance(x, Port))
    assert (port.kind, port.expected_type) == (kind, expected)
    assert host.ports[0].port_id == port.port_id
    assert host.ports[0].address


def test_different_seeds_change_topology_and_nested_consumers():
    hosts = [generate_random_ast(host_seed=seed) for seed in range(80)]
    assert len({host.shape_digest for host in hosts}) >= 60
    kinds = {type(node) for host in hosts for node in walk(host.tree)}
    assert {ir.Match, ir.With, ir.Count, ir.Exists, ir.Case, ir.Call, ir.Boolean} <= kinds
    assert any(node.optional for host in hosts for node in walk(host.tree) if isinstance(node, ir.Match))
    assert any(isinstance(node, TerminalSlot) for host in hosts for node in walk(host.tree))
    assert {host.ports[0].kind for host in hosts} == {"rows", "scalar", "predicate"}


def test_host_archive_roundtrip_keeps_both_passes_and_symbolic_terminals():
    host = generate_random_ast(host_seed=33)
    restored = HostAst.from_data(json.loads(json.dumps(host.to_data())))
    assert restored == host
    assert restored.host_digest == host.host_digest
    assert restored.shape_digest == host.shape_digest
    assert restored.scope_graph and restored.clause_skeleton


def test_with_projection_drops_old_bindings_and_preserves_entity_alias_identity():
    incoming = ScopeState("scope:1", visible_symbols=(("a", "node?"), ("b", "node"), ("old", "integer")))
    projected = advance_scope(ir.With((ir.Alias(ir.Variable("a"), "saved"),
                                       ir.Alias(ir.Literal(3), "n"))), incoming)
    assert dict(projected.visible_symbols) == {"saved": "node?", "n": "integer"}
    assert projected.nullable_types == ("saved",)
    assert projected.alias_sources[0]["source_symbols"] == ("a",)
    with pytest.raises(StageFailure, match="unbound_symbol"):
        advance_scope(ir.With((ir.Variable("b"),)), projected)


def test_with_sibling_alias_cannot_be_referenced_in_same_projection():
    with pytest.raises(StageFailure, match="unbound_symbol"):
        advance_scope(ir.With((ir.Alias(ir.Literal(1), "a"), ir.Alias(ir.Variable("a"), "b"))), ScopeState())


def test_optional_introductions_are_nullable_but_existing_inputs_keep_their_type():
    incoming = ScopeState(visible_symbols=(("a", "node"),))
    clause = ir.Match(ir.Edge(ir.Node("a"), ir.Relationship(variable="r", type="T"), ir.Node("b")), optional=True)
    scope = advance_scope(clause, incoming)
    assert dict(scope.visible_symbols) == {"a": "node", "b": "node?", "r": "relationship?"}
    assert set(scope.nullable_types) == {"r", "b"}


def test_budget_exhaustion_is_stage_specific_and_never_fills_an_illegal_default():
    with pytest.raises(StageFailure) as error:
        generate_random_ast(budget={"nodes": 1}, host_seed=0)
    assert error.value.stage == "M1" and error.value.code == "generation_exhausted"
    with pytest.raises(StageFailure, match="Clause skeleton"):
        generate_random_ast(grammar={"clauses": ["MATCH", "WITH", "RETURN"]}, budget={"clauses": 2})


def test_disabled_subqueries_cannot_supply_a_row_port():
    with pytest.raises(StageFailure, match="row port requires"):
        generate_random_ast(grammar={"clauses": ["RETURN"], "port_kinds": ["rows"]},
                            capability_profile={"subqueries": False}, budget={"expression_depth": 0})


def test_budget_is_shared_between_nested_subqueries_and_host_clauses():
    for seed in range(15):
        host = generate_random_ast(budget={"nodes": 180}, host_seed=seed)
        count = sum(is_dataclass(node) and not isinstance(node, TerminalSlot) for node in walk(host.tree))
        assert count <= 180
        # Insertion has a separate grammar-independent allowance; graph scale
        # is not an input to either of these two structural budgets.
        assert host.budget["port_nodes"] == host.ports[0].reserved_budget == 128


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
def test_port_type_adapters_do_not_exceed_expression_depth(limit):
    composite = (ir.Comparison, ir.Boolean, ir.Negation, ir.Case, ir.Call, ir.All)

    def expression_depth(value):
        if not isinstance(value, composite):
            return 0
        children = []
        for field in fields(value):
            child = getattr(value, field.name)
            children.extend(child if isinstance(child, tuple) else (child,))
        return 1 + max((expression_depth(child) for child in children), default=0)

    # COUNT/EXISTS open a query block: their nesting is constrained by the
    # separate shared subquery budget, not hidden scalar children.
    for seed in range(25):
        host = generate_random_ast(budget={"expression_depth": limit}, host_seed=seed)
        assert max(expression_depth(node) for node in walk(host.tree)) <= limit


@pytest.mark.parametrize("grammar,capabilities", [({"clauses": 1}, None),
                                                  (None, {"clause_kinds": "RETURN"}),
                                                  (None, {"expression_operators": ["invalid"]})])
def test_malformed_grammar_inputs_are_structured_failures(grammar, capabilities):
    with pytest.raises(StageFailure) as failure:
        generate_random_ast(grammar, capabilities)
    assert failure.value.stage == "M1" and failure.value.code == "invalid_configuration"


def test_incorrect_port_address_and_leaked_variables_are_rejected():
    host = generate_random_ast(grammar={"clauses": ["RETURN"], "port_kinds": ["scalar"]},
                               budget={"expression_depth": 0})
    with pytest.raises(StageFailure, match="accurately located"):
        validate_abstract_types_and_scopes(replace(host, ports=(replace(host.ports[0], address=("wrong",)),)))
    altered = replace(host.tree, returns=(ir.Variable("not_in_scope"),))
    with pytest.raises(StageFailure, match="Unbound variable"):
        validate_abstract_types_and_scopes(replace(host, tree=altered))
