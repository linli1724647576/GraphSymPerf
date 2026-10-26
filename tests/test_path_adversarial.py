"""Independent path semantics and lexical-observation counterexamples."""

from dataclasses import replace

import pytest

import ir
from dependencies import scalar_value_observation
from population import (MatchDemand, PathDemand, PopulationError, ValueDemand,
                        _Interpreter, evaluate_query, synthesize_relational_state)
from query import parse_query, render_query
from state import GraphSpec


def path_term(*, bounds=(1, 3), where=True):
    edge = ir.Edge(ir.Node('u', ('AuditRoot',), (('id', ir.Literal(1)),)),
                   ir.Relationship(type='AUDIT_LINK', min_hops=bounds[0], max_hops=bounds[1]),
                   ir.Node('v', ('AuditLeaf',)), 'undirected', 'p')
    universal = ir.All('x', ir.Call('nodes', (ir.Variable('p'),)),
                       ir.Comparison(ir.Property('x', 'keep'), '=', ir.Literal(True)))
    return ir.Query(edge, universal if where else None,
                    (ir.Alias(ir.Call('length', (ir.Variable('p'),)), 'hops'),
                     ir.Alias(universal, 'valid')))


def explicit_state(values, *, edge_pairs=None, leaf_first=False):
    last = len(values)
    nodes = tuple({'count': 1, 'labels': ['AuditRoot'] if i == 1 else ['AuditLeaf'] if i == last else ['AuditMiddle'],
                   'properties': {'uid': i, **({'id': 1} if i == 1 else {}),
                                  **({} if value is None else {'keep': value})}}
                  for i, value in enumerate(values, 1))
    if leaf_first:
        nodes = (dict(nodes[0], labels=['AuditRoot', 'AuditLeaf']), *nodes[1:])
    edge_pairs = edge_pairs if edge_pairs is not None else tuple((i, i + 1) for i in range(1, last))
    labels = {node['properties']['uid']: node['labels'][0] for node in nodes}
    edges = tuple({'count': 1, 'source': a, 'target': b, 'source_label': labels[a], 'target_label': labels[b],
                   'type': 'AUDIT_LINK', 'properties': {}} for a, b in edge_pairs)
    return GraphSpec('AST', 1, 0, include_reference=False, version=2, node_groups=nodes, edge_groups=edges)


@pytest.mark.parametrize('values, expected', [
    ((True, True, True), True), ((False, True, True), False),
    ((True, True, False), False), ((True, None, True), None),
    ((True, None, False), False),
])
def test_all_observes_root_endpoint_and_false_overrides_null(values, expected):
    query = path_term(bounds=(2, 2), where=False)
    assert evaluate_query(query, explicit_state(values)) == [{'hops': 2, 'valid': expected}]
    filtered = replace(query, where=query.returns[1].expression)
    assert evaluate_query(filtered, explicit_state(values)) == ([{'hops': 2, 'valid': True}] if expected is True else [])


@pytest.mark.parametrize('value', [True, False, None])
def test_zero_hop_nodes_is_a_singleton_and_not_an_empty_universal(value):
    query = path_term(bounds=(0, 0), where=False)
    assert evaluate_query(query, explicit_state((value,), leaf_first=True)) == [{'hops': 0, 'valid': value}]


@pytest.mark.parametrize('wanted, actual, holds', [(False, None, False), (False, False, True),
                                                (None, False, False), (None, None, True)])
def test_scalar_observer_never_confuses_false_and_null(wanted, actual, holds):
    query = path_term(bounds=(2, 2), where=False)
    observation = scalar_value_observation(query, ('returns', 1, 'expression'), value=wanted)
    check = ir.Query(None, None, (ir.Alias(observation['expression'], 'holds'),))
    reparsed = parse_query(render_query(check))
    assert render_query(reparsed) == render_query(check)
    assert evaluate_query(check, explicit_state((True, actual, True))) == [{'holds': holds}]
    assert evaluate_query(reparsed, explicit_state((True, actual, True))) == [{'holds': holds}]


def test_scalar_observation_follows_with_scalar_shadowing_of_path_name():
    base = path_term(bounds=(2, 2))
    query = ir.Query(None, None, (ir.Variable('p'),), pipeline=(
        ir.Match(base.pattern, base.where), ir.With((ir.Variable('p'),)),
        ir.With((ir.Alias(ir.Call('length', (ir.Variable('p'),)), 'p'),)),
    ))
    observation = scalar_value_observation(query, ('returns', 0), value=2)
    check = ir.Query(None, None, (ir.Alias(observation['expression'], 'holds'),))
    assert parse_query(render_query(check)) == check
    assert evaluate_query(check, explicit_state((True, True, True))) == [{'holds': True}]
    observation = scalar_value_observation(query, ('returns', 0), value=3)
    check = ir.Query(None, None, (ir.Alias(observation['expression'], 'holds'),))
    assert evaluate_query(check, explicit_state((True, True, True))) == [{'holds': False}]


def test_trail_can_revisit_a_node_but_cannot_reuse_a_relationship():
    base = path_term(bounds=(3, 3), where=False)
    edge = replace(base.pattern, right=ir.Node('v', ('AuditRoot',)))
    query = replace(base, pattern=edge)
    state = explicit_state((True, True, True), edge_pairs=((1, 2), (2, 3), (3, 1)))
    assert evaluate_query(query, state) == [{'hops': 3, 'valid': True}, {'hops': 3, 'valid': True}]
    acyclic = replace(query, pattern=replace(edge, relationship=replace(edge.relationship, path_mode='acyclic')))
    assert evaluate_query(acyclic, state) == []
    too_long = replace(query, pattern=replace(edge, relationship=replace(edge.relationship, min_hops=6, max_hops=6)))
    assert evaluate_query(too_long, state) == []


def test_conflicting_all_demand_is_not_merely_recorded_in_construction_metadata():
    query = path_term(bounds=(2, 2))
    with pytest.raises(PopulationError):
        synthesize_relational_state(query, scale=7, data_seed=0, min_result_rows=1, demands=(
            MatchDemand(('query', 'pattern'), 'paths', exact_matches=1),
            PathDemand(('query', 'pattern'), min_length=2, max_length=2,
                       all_values=((('query', 'where'), False),)),
        ))


@pytest.mark.parametrize('operator,literal', [('<>', 6000), ('IS NOT NULL', None), ('=', 1)])
def test_uid_dependent_universal_is_rejected_without_affine_interior_proof(operator, literal):
    query = path_term(bounds=(3, 3))
    predicate = ir.Boolean('AND', (query.where.predicate,
        ir.Comparison(ir.Property('x', 'uid'), operator, ir.Literal(literal))))
    query = replace(query, where=replace(query.where, predicate=predicate), returns=(ir.Literal(1),))
    with pytest.raises(PopulationError, match='UID|uid|identity|affine'):
        synthesize_relational_state(query, scale=7000, data_seed=0, min_result_rows=1, demands=(
            MatchDemand(('query', 'pattern'), 'paths', exact_matches=1,
                        candidate_matches=7001, candidate_kind='path_false_prefix'),
            PathDemand(('query', 'pattern'), min_length=3, max_length=3),
        ))


def test_lifted_interiors_are_fresh_against_zero_population_ast_reads():
    body = path_term(bounds=(3, 3))
    observed = ir.Comparison(ir.Count(body=ir.Query(ir.Node('z', ('GspPathInterior',)),
        None, (ir.Literal(1),))), '>', ir.Literal(100))
    query = ir.Query(None, None, (ir.Alias(ir.Count(body=body), 'n'),
        ir.Alias(ir.Call('coalesce', (ir.Literal(0), observed)), 'masked')))
    path = ('query', 'returns', 0, 'expression', 'body', 'pattern')
    fact = ('query', 'returns', 1, 'expression', 'args', 1)
    populated = synthesize_relational_state(query, scale=6000, data_seed=0, min_result_rows=1, demands=(
        MatchDemand(path, 'paths', exact_matches=1, candidate_matches=6001, candidate_kind='path_false_prefix'),
        PathDemand(path, min_length=3, max_length=3),
        MatchDemand((*fact, 'left', 'body', 'pattern'), 'unused_scan', exact_matches=0),
    ), obligations=(ValueDemand(fact, False),))
    proof = GraphSpec.from_dict(populated.evidence['validation_graph_spec'])
    check = ir.Query(None, None, (ir.Alias(observed, 'actual'),))
    assert populated.spec.counts()['nodes'] > 5000
    assert _Interpreter(proof).run(check) == [{'actual': False}]
    assert _Interpreter(populated.spec).run(check) == [{'actual': False}]
    assert not any('GspPathInterior' in group['labels'] for group in populated.spec.node_groups)
