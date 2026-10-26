"""Actual row bindings and per-input matching quantities for OPTIONAL flow."""

from dataclasses import replace

import pytest

import ir
from dependencies import entity_binding_observation, find_mutation_sites
from population import (EntityBindingDemand, InversePair, MatchDemand, PopulationError, ValueDemand,
                        evaluate_query, synthesize_relational_state)


def optional_chain():
    return ir.Query(ir.Node('u', ('Root',), (('id', ir.Literal(1)),)), None,
        (ir.Property('v', 'uid'), ir.Property('w', 'uid')), matches=(
        ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='A'), ir.Node('v', ('Item',))), optional=True),
        ir.Match(ir.Edge(ir.Node('v'), ir.Relationship(type='B'), ir.Node('w', ('Tail',))), optional=True)))


@pytest.mark.parametrize('quantity,relation', [(0, 'null'), (2, 'nonnull')])
def test_binding_facts_observe_optional_output_instead_of_orphan_physical_nodes(quantity, relation):
    query = optional_chain()
    address = ('query', 'matches', 0, 'pattern', 'right')
    result = synthesize_relational_state(query, scale=7, data_seed=0, demands=(
        MatchDemand(('query', 'matches', 0, 'pattern'), 'first', exact_matches=quantity),
        MatchDemand(('query', 'matches', 1, 'pattern'), 'second', exact_matches=1)),
        obligations=(EntityBindingDemand(address, relation),), min_result_rows=1)
    assert any('Item' in node['labels'] for node in result.spec.iter_nodes())
    assert all((row['v.uid'] is None) == (quantity == 0) for row in result.expected_rows)
    with pytest.raises(PopulationError, match='binding'):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(
            MatchDemand(('query', 'matches', 0, 'pattern'), 'first', exact_matches=quantity),),
            obligations=(EntityBindingDemand(address, 'nonnull' if relation == 'null' else 'null'),))


def test_imported_nonnull_input_is_observed_before_a_zero_result_optional_match():
    query = optional_chain()
    fact = entity_binding_observation(query, ('matches', 1, 'pattern', 'left'), 'nonnull')
    assert fact['position'] == 'before_match'
    result = synthesize_relational_state(query, scale=7, data_seed=0, demands=(
        MatchDemand(('query', 'matches', 0, 'pattern'), 'first', exact_matches=2),
        MatchDemand(('query', 'matches', 1, 'pattern'), 'second', exact_matches=0)),
        obligations=(EntityBindingDemand(('query', 'matches', 1, 'pattern', 'left')),))
    assert all(row['v.uid'] is not None and row['w.uid'] is None for row in result.expected_rows)


def test_an_empty_prior_row_stream_cannot_vacuously_prove_a_null_binding_fact():
    query = replace(optional_chain(), where=ir.Literal(False))
    with pytest.raises(PopulationError, match='binding'):
        synthesize_relational_state(query, scale=7, data_seed=0,
            obligations=(EntityBindingDemand(('query', 'matches', 0, 'pattern', 'right'), 'null'),))


def test_actual_same_label_scans_do_not_satisfy_separate_recipe_counts():
    query = ir.Query(ir.Node('a', ('Same',)), None, (ir.Property('a', 'uid'), ir.Property('b', 'uid')),
                     matches=(ir.Match(ir.Node('b', ('Same',))),))
    with pytest.raises(PopulationError, match='matching|quantity'):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(
            MatchDemand(('query', 'pattern'), 'first', exact_matches=2),
            MatchDemand(('query', 'matches', 0, 'pattern'), 'second', exact_matches=3)))


def test_same_relationship_domain_cannot_claim_one_optional_is_empty_and_the_other_nonempty():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    query = ir.Query(root, None, (ir.Property('a', 'uid'), ir.Property('b', 'uid')), matches=tuple(
        ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='R'), ir.Node(name, ('Same',))), optional=True)
        for name in ('a', 'b')))
    with pytest.raises(PopulationError, match='matching|quantity|binding'):
        synthesize_relational_state(query, scale=7, data_seed=0, demands=(
            MatchDemand(('query', 'matches', 0, 'pattern'), 'first', exact_matches=0),
            MatchDemand(('query', 'matches', 1, 'pattern'), 'second', exact_matches=2)),
            obligations=(EntityBindingDemand(('query', 'matches', 0, 'pattern', 'right'), 'null'),))


@pytest.mark.parametrize('present', [False, True])
def test_entity_alias_keeps_null_extension_and_uses_the_same_physical_binding(present):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    query = ir.Query(None, None, (ir.Property('saved', 'uid'), ir.Property('w', 'uid')), pipeline=(
        ir.Match(root),
        ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='A'), ir.Node('a', ('Item',))), optional=True),
        ir.With((ir.Alias(ir.Variable('a'), 'saved'),)),
        ir.Match(ir.Edge(ir.Node('saved'), ir.Relationship(type='B'), ir.Node('w', ('Tail',))), optional=True)))
    result = synthesize_relational_state(query, scale=7, data_seed=0, min_result_rows=1, demands=(
        MatchDemand(('query', 'pipeline', 1, 'pattern'), 'first', exact_matches=2 if present else 0),
        MatchDemand(('query', 'pipeline', 3, 'pattern'), 'second', exact_matches=1)),
        obligations=(EntityBindingDemand(('query', 'pipeline', 3, 'pattern', 'left'), 'nonnull' if present else 'null'),))
    assert all((row['saved.uid'] is not None) == present for row in result.expected_rows)
    assert all((row['w.uid'] is not None) == present for row in result.expected_rows)
    domains = [domain for domain in result.constraints['binding_domains'] if 'Item' in domain['labels']]
    assert len(domains) == 1


def test_nonnull_entity_and_null_ordinary_property_are_distinct_obligations():
    query = optional_chain()
    query = replace(query, returns=(*query.returns, ir.Alias(ir.Property('v', 'tag'), 'tag')))
    result = synthesize_relational_state(query, scale=7, data_seed=0, min_result_rows=1, demands=(
        MatchDemand(('query', 'matches', 0, 'pattern'), 'first', exact_matches=2),), obligations=(
        EntityBindingDemand(('query', 'matches', 0, 'pattern', 'right')),
        ValueDemand(('query', 'returns', 2, 'expression'), None),))
    assert all(row['v.uid'] is not None and row['tag'] is None for row in result.expected_rows)


def test_hidden_graph_guard_cannot_flip_an_independent_optional_binding_or_quantity_fact():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    high = ir.Query(ir.Edge(root, ir.Relationship(type='HIGH'), ir.Node('h', ('High',))), None, (ir.Literal(1),))
    observed = ir.Query(root, None, (ir.Property('a', 'uid'),), matches=(
        ir.Match(ir.Edge(root, ir.Relationship(type='R'), ir.Node('a', ('Item',))),
                 ir.Comparison(ir.Count(body=high), '>', ir.Literal(5)), optional=True),))
    producer = ir.Query(ir.Edge(root, ir.Relationship(type='R'), ir.Node('c', ('Item',))), None, (ir.Literal(1),))
    query = ir.Query(root, None, (ir.Alias(ir.Call('coalesce', (ir.Literal(0), ir.Count(body=observed),
                                                            ir.Count(body=producer))), 'value'),))
    base = ('query', 'returns', 0, 'expression', 'args', 1, 'body')
    with pytest.raises(PopulationError, match='row observation.*growth'):
        synthesize_relational_state(query, scale=5001, data_seed=0, demands=(
            MatchDemand((*base, 'matches', 0, 'pattern'), 'optional', exact_matches=0),
            MatchDemand((*base, 'matches', 0, 'where', 'left', 'body', 'pattern'), 'high', scale_matches=True)),
            obligations=(EntityBindingDemand((*base, 'matches', 0, 'pattern', 'right'), 'null'),))


@pytest.mark.parametrize('filter_kind', ['map', 'where'])
def test_affine_relationship_properties_cannot_flip_a_hidden_optional_null_fact(filter_kind):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    relationship = ir.Relationship(type='R', properties=(('pair', ir.Literal(7)),)) if filter_kind == 'map' else ir.Relationship(variable='r', type='R')
    predicate = None if filter_kind == 'map' else ir.Comparison(ir.Property('r', 'pair'), '=', ir.Literal(7))
    observed = ir.Query(root, None, (ir.Property('a', 'uid'),), matches=(
        ir.Match(ir.Edge(root, relationship, ir.Node('a', ('Same',))), predicate, optional=True),))
    high = ir.Query(ir.Edge(root, ir.Relationship(type='R'), ir.Node('h', ('Same', 'High'))), None, (ir.Literal(1),))
    query = ir.Query(root, None, (ir.Alias(ir.Call('coalesce', (ir.Literal(0), ir.Count(body=observed),
                                                            ir.Count(body=high))), 'value'),))
    with pytest.raises(PopulationError, match='row observation.*(affine|growth)'):
        synthesize_relational_state(query, scale=5001, data_seed=0, demands=(
            MatchDemand(('query', 'returns', 0, 'expression', 'args', 2, 'body', 'pattern'), 'high', scale_matches=True),),
            obligations=(InversePair('R', 'RI', 'pair'),
                EntityBindingDemand(('query', 'returns', 0, 'expression', 'args', 1, 'body', 'matches', 0, 'pattern', 'right'), 'null')))


@pytest.mark.parametrize('nullable_anchor', [False, True])
def test_optional_exchange_anchor_alias_follows_the_real_mandatory_or_nullable_definition(nullable_anchor):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    prefix = (ir.Match(root),)
    if nullable_anchor:
        prefix = (*prefix, ir.Match(ir.Edge(root, ir.Relationship(type='SEED'), ir.Node('n', ('Anchor',))), optional=True))
    alias = ir.With((ir.Alias(ir.Variable('n' if nullable_anchor else 'u'), 'saved'),))
    branches = tuple(ir.Match(ir.Edge(ir.Node('saved'), ir.Relationship(type=kind), ir.Node(name, (kind,))), optional=True)
                     for kind, name in [('A', 'a'), ('B', 'b')])
    query = ir.Query(None, None, (ir.Property('a', 'uid'), ir.Property('b', 'uid')), pipeline=(*prefix, alias, *branches))
    sites = find_mutation_sites(query, 'P11', 'optional_branches')
    assert bool(sites) is not nullable_anchor
    if sites:
        site, = sites
        assert site.address == ('pipeline', len(prefix) + 1)
