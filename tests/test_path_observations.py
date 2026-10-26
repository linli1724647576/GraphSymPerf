"""Scalar checks retain real path/alias producers and exact three-valued facts."""

from dataclasses import replace

import pytest

import ir
from dependencies import scalar_value_observation
from population import evaluate_query
from state import GraphSpec


def example(value):
    edge = ir.Edge(ir.Node('u', ('Root',)), ir.Relationship(type='R', min_hops=1, max_hops=3),
                   ir.Node('v', ('End',)), 'undirected', 'p')
    universal = ir.All('x', ir.Call('nodes', (ir.Variable('p'),)),
                       ir.Comparison(ir.Property('x', 'keep'), '=', ir.Literal(True)))
    query = ir.Query(edge, None, (ir.Alias(universal, 'truth'),))
    spec = GraphSpec('AST', 1, 0, include_reference=False, version=2,
        node_groups=({'count': 1, 'labels': ['Root'], 'properties': {'uid': 1, 'keep': True}},
                     {'count': 1, 'labels': ['End'], 'properties': {'uid': 2, 'keep': value}}),
        edge_groups=({'count': 1, 'source': 1, 'target': 2, 'source_label': 'Root',
                      'target_label': 'End', 'type': 'R'},))
    return query, spec


def observed(query, address, value, spec):
    obligation = scalar_value_observation(query, address, 'equal', value)
    check = ir.Query(None, None, (ir.Alias(obligation['expression'], 'fact'),))
    ir.validate_query(check)
    return evaluate_query(check, spec)


@pytest.mark.parametrize('actual', [True, False, None])
@pytest.mark.parametrize('requested', [True, False, None])
def test_path_truth_checks_distinguish_false_and_unknown(actual, requested):
    query, spec = example(actual)
    assert observed(query, ('returns', 0, 'expression'), requested, spec) == [{'fact': actual is requested}]


def test_empty_path_domain_does_not_establish_a_scalar_fact():
    query, spec = example(None)
    spec = replace(spec, edge_groups=())
    assert observed(query, ('returns', 0, 'expression'), None, spec) == [{'fact': False}]


def test_where_scalar_is_observed_before_its_filter_can_hide_unknown():
    query, spec = example(None)
    query = replace(query, where=query.returns[0].expression)
    assert evaluate_query(query, spec) == []
    assert observed(query, ('where',), True, spec) == [{'fact': False}]
    assert observed(query, ('where',), None, spec) == [{'fact': True}]


def test_with_alias_observation_keeps_its_producer_without_reintroducing_dropped_path():
    query, spec = example(None)
    query = ir.Query(None, None, (ir.Variable('truth'),), pipeline=(
        ir.Match(query.pattern), ir.With((query.returns[0],)),))
    assert observed(query, ('returns', 0), None, spec) == [{'fact': True}]

