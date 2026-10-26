"""Structural coverage separates operators while ignoring graph bindings."""

from dataclasses import replace

import pytest

import ir
from ast_pattern.contracts import ScopeState, TerminalSlot, shape, shape_digest


def query(expression):
    return ir.Query(None, None, (ir.Alias(expression, 'value'),))


@pytest.mark.parametrize('left,right', [
    (ir.Boolean('AND', (ir.Literal(True), ir.Literal(False))),
     ir.Boolean('OR', (ir.Literal(True), ir.Literal(False)))),
    (ir.Comparison(ir.Literal(1), '=', ir.Literal(2)),
     ir.Comparison(ir.Literal(1), '<>', ir.Literal(2))),
    (ir.Comparison(ir.Literal(1), '<', ir.Literal(2)),
     ir.Comparison(ir.Literal(1), '<=', ir.Literal(2))),
    (ir.Negation(ir.Literal(True)), ir.Literal(True)),
    (ir.Call('length', (ir.Variable('p'),)), ir.Call('size', (ir.Variable('p'),))),
    (ir.Call('coalesce', (ir.Literal(1), ir.Literal(2))),
     ir.Call('range', (ir.Literal(1), ir.Literal(2)))),
    (ir.Count(body=query(ir.Literal(1))), ir.Exists(body=query(ir.Literal(1)))),
])
def test_shape_preserves_operator_and_function_identity(left, right):
    assert shape_digest(query(left)) != shape_digest(query(right))


def test_shape_preserves_traversal_direction_and_path_mode():
    edge = ir.Edge(ir.Node('a'), ir.Relationship(type='T'), ir.Node('b'), 'out')
    assert shape_digest(edge) != shape_digest(replace(edge, direction='in'))
    assert shape_digest(edge) != shape_digest(replace(edge, direction='undirected'))
    assert shape_digest(edge) != shape_digest(replace(edge, relationship=replace(edge.relationship, path_mode='walk')))


def test_shape_preserves_optional_distinct_and_limit_presence():
    match = ir.Match(ir.Node('u'))
    assert shape_digest(match) != shape_digest(replace(match, optional=True))
    with_clause = ir.With((ir.Variable('u'),))
    assert shape_digest(with_clause) != shape_digest(replace(with_clause, distinct=True))
    source = query(ir.Literal(1))
    assert shape_digest(source) != shape_digest(replace(source, distinct=True))
    assert shape_digest(source) != shape_digest(replace(source, limit=1))
    assert shape_digest(replace(source, limit=1)) == shape_digest(replace(source, limit=9))


def test_shape_preserves_bounded_path_fields_and_normalizes_their_values():
    relation = ir.Relationship(type='T')
    bounded = replace(relation, min_hops=1, max_hops=3)
    assert shape_digest(relation) != shape_digest(bounded)
    assert shape_digest(bounded) == shape_digest(replace(bounded, min_hops=2, max_hops=5))
    assert shape_digest(bounded) != shape_digest(replace(bounded, max_hops=None))


def test_alpha_renaming_graph_names_and_literal_values_do_not_add_shapes():
    first = ir.Query(ir.Edge(ir.Node('a', ('Source',)), ir.Relationship(type='T'), ir.Node('b', ('Target',))),
                     ir.Comparison(ir.Property('a', 'score'), '>', ir.Literal(3)),
                     (ir.Alias(ir.Property('b', 'number'), 'result'),))
    second = ir.Query(ir.Edge(ir.Node('x', ('Input',)), ir.Relationship(type='LINK'), ir.Node('y', ('Output',))),
                      ir.Comparison(ir.Property('x', 'weight'), '>', ir.Literal(900)),
                      (ir.Alias(ir.Property('y', 'other'), 'renamed'),))
    assert shape_digest(first) == shape_digest(second)


def test_structural_child_order_is_retained():
    first = ir.Boolean('AND', (ir.Negation(ir.Literal(True)), ir.Literal(False)))
    second = ir.Boolean('AND', (ir.Literal(False), ir.Negation(ir.Literal(True))))
    assert shape_digest(first) != shape_digest(second)


@pytest.mark.parametrize('materialized', [None, True, False])
def test_nullable_literal_slots_keep_shape_after_terminal_materialization(materialized):
    slot = TerminalSlot('value', 'literal', (True, False, None), 'boolean?')
    assert shape_digest(query(ir.Literal(slot))) == shape_digest(query(ir.Literal(materialized)))


def test_graph_terminals_and_their_accepted_bindings_have_the_same_shape():
    source = ir.Edge(
        ir.Node('u', (TerminalSlot('label', 'label', ('Root',), 'string'),),
                ((TerminalSlot('key', 'property_key', ('id',), 'integer'),
                  ir.Literal(TerminalSlot('id', 'integer', (1, 2), 'integer'))),)),
        ir.Relationship(type=TerminalSlot('type', 'relationship_type', ('T',), 'string'),
                        min_hops=TerminalSlot('minimum', 'integer', (1, 2), 'integer'),
                        max_hops=TerminalSlot('maximum', 'integer', (3, 4), 'integer')),
        ir.Node('v'), 'out')
    accepted = ir.Edge(ir.Node('u', ('Root',), (('id', ir.Literal(2)),)),
                       ir.Relationship(type='T', min_hops=2, max_hops=4), ir.Node('v'), 'out')
    assert shape_digest(source) == shape_digest(accepted)


def test_stage_metadata_is_not_silently_normalized_as_query_shape():
    with pytest.raises(TypeError, match='query AST'):
        shape_digest(ScopeState())
    with pytest.raises(TypeError, match='query AST'):
        shape({'operator': 'AND', 'scope': 'scope:0'})


def test_real_m4_keeps_the_embedded_shape_while_materializing_graph_terminals():
    from ast_pattern.host_generator import generate_random_ast
    from ast_pattern.embedding import embed_patterns
    from ast_pattern.state_builder import construct_graph_state
    from ast_pattern.instantiation import instantiate_query

    host = generate_random_ast(
        grammar={'clauses': ['RETURN'], 'port_kinds': ['rows'], 'return_type': 'integer'},
        budget={'expression_depth': 0}, host_seed=0)
    embedded = embed_patterns(host, selection_policy='P3', embed_seed=0)
    state, plan, _ = construct_graph_state(embedded, 5, 3)
    instantiated = instantiate_query(embedded, state, plan)
    assert shape_digest(embedded.tree) == shape_digest(instantiated.source)
