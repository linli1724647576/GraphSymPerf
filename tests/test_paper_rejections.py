"""Counterexamples at the applicability boundary of the paper's query laws."""

from dataclasses import replace

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import mutate, plan_mutation
from paper_patterns import compositional_rule
from population import MatchDemand, PopulationError, evaluate_query, synthesize_relational_state
from query import parse_query
from state import GraphSpec


def reject_paper_site(query, pattern):
    rule = compositional_rule(pattern)
    assert find_mutation_sites(query, pattern, rule.form) == ()
    with pytest.raises(ir.QueryError, match='applicable paper'):
        mutate(query, rule.legacy_pattern, bindings={
            'paper_pattern': pattern, 'form': rule.form,
            'legacy_pattern': rule.legacy_pattern, 'pattern_catalog': 'paper-v1',
        })


@pytest.mark.parametrize('observation', ['relationship_property', 'relationship_constraint', 'named_path'])
def test_p2_rejects_observable_relationship_domains(observation):
    relationship = ir.Relationship(type='FWD')
    returns, path = (ir.Property('a', 'uid'),), None
    if observation == 'relationship_property':
        relationship = replace(relationship, variable='r')
        returns = (ir.Property('r', 'uid'),)
    elif observation == 'relationship_constraint':
        relationship = replace(relationship, properties=(('visible', ir.Literal(1)),))
    else:
        path, returns = 'p', (ir.Alias(ir.Call('length', (ir.Variable('p'),)), 'length'),)
    query = ir.Query(ir.Edge(ir.Node('a'), relationship, ir.Node('b'), path_variable=path), None, returns)
    ir.validate_query(query)
    reject_paper_site(query, 'P2')


def test_p9_rejects_a_predicate_whose_binding_does_not_exist_at_the_earlier_site():
    query = ir.Query(ir.Node('a'), None, (ir.Property('b', 'uid'),), matches=(
        ir.Match(ir.Node('b'), ir.Comparison(ir.Property('b', 'flag'), '=', ir.Literal(True))),
    ))
    ir.validate_query(query)
    reject_paper_site(query, 'P9')
    # Attaching the same predicate to the earlier MATCH is an invalid query.
    invalid = replace(query, where=query.matches[0].where,
                      matches=(replace(query.matches[0], where=None),))
    with pytest.raises(ir.QueryError, match='Unbound'):
        ir.validate_query(invalid)


@pytest.mark.parametrize('dependency', ['endpoint', 'predicate'])
def test_p11_rejects_cross_optional_dependencies(dependency):
    first = ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='R'), ir.Node('a')), optional=True)
    second = ir.Match(ir.Edge(ir.Node('a' if dependency == 'endpoint' else 'u'),
                              ir.Relationship(type='S'), ir.Node('b')), optional=True)
    if dependency == 'predicate':
        second = replace(second, where=ir.Comparison(ir.Property('a', 'flag'), '=', ir.Literal(True)))
    query = ir.Query(ir.Node('u'), None, (ir.Property('a', 'uid'), ir.Property('b', 'uid')),
                     matches=(first, second))
    ir.validate_query(query)
    reject_paper_site(query, 'P11')


def test_p11_rejects_an_anchor_introduced_only_by_an_optional_match():
    query = ir.Query(ir.Node('root'), None, (ir.Property('a', 'uid'),), matches=(
        ir.Match(ir.Edge(ir.Node('root'), ir.Relationship(type='ROOT'), ir.Node('u')), optional=True),
        ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='R'), ir.Node('a')), optional=True),
        ir.Match(ir.Edge(ir.Node('u'), ir.Relationship(type='S'), ir.Node('b')), optional=True),
    ))
    ir.validate_query(query)
    reject_paper_site(query, 'P11')


def test_p12_rejects_a_consumer_that_observes_the_first_union_row():
    branches = tuple(ir.Query(None, None, (ir.Alias(ir.Literal(n), 'value'),)) for n in (1, 2))
    union = ir.UnionAll(branches)
    query = ir.RowLimit(union, 1)
    spec = GraphSpec('P1', 1, 0)
    assert evaluate_query(query, spec) == [{'value': 1}]
    assert evaluate_query(ir.RowLimit(ir.UnionAll(tuple(reversed(branches))), 1), spec) == [{'value': 2}]
    reject_paper_site(query, 'P12')


def test_p12_rejects_a_branch_local_limit_even_without_an_outer_limit():
    first = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'value'),), limit=1)
    second = ir.Query(None, None, (ir.Alias(ir.Literal(2), 'value'),))
    reject_paper_site(ir.UnionAll((first, second)), 'P12')


def test_s9_does_not_treat_scalar_count_as_a_row_producing_aggregate():
    query = ir.Query(None, None, (ir.Variable('n'),), pipeline=(
        ir.Match(ir.Node('a'), ir.Literal(False)),
        ir.With((ir.Alias(ir.Count(body=ir.Query(ir.Node('z'), None, (ir.Literal(1),))), 'n'),)),
        ir.Match(ir.Node('b')),
    ))
    assert evaluate_query(query, GraphSpec('P1', 1, 0)) == []
    reject_paper_site(query, 'S9')
    # A native ungrouped aggregate can instead restore one row after empty
    # input. It is rejected at the IR boundary, not assumed to preserve empty.
    with pytest.raises(ir.QueryError):
        parse_query('MATCH (a) WHERE false WITH count(*) AS n MATCH (b) RETURN n')


@pytest.mark.parametrize('guard', [True, False, None])
def test_p13_uses_a_null_aware_complement_and_the_naive_not_has_a_counterexample(guard):
    original = ir.Case(ir.Literal(guard), ir.Literal(1), ir.Literal(2))
    query = ir.Query(None, None, (ir.Alias(original, 'value'),))
    site, = find_mutation_sites(query, 'P13', 'case_dual')
    changed = plan_mutation(query, site)
    spec = GraphSpec('P1', 1, 0)
    assert evaluate_query(query, spec) == evaluate_query(changed, spec)
    replacement = changed.returns[0].expression
    assert replacement.condition == ir.Negation(ir.Call('coalesce', (original.condition, ir.Literal(False))))
    if guard is None:
        naive = ir.Query(None, None, (ir.Alias(ir.Case(ir.Negation(original.condition),
                                                      original.otherwise, original.then), 'value'),))
        assert evaluate_query(query, spec) == [{'value': 2}]
        assert evaluate_query(naive, spec) == [{'value': 1}]


@pytest.mark.parametrize('extra_read', ['same_label_node', 'same_type_edge', 'uid_union_column'])
def test_large_growth_rejects_other_profiles_that_read_the_same_physical_domain(extra_read):
    anchor = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    work = ir.Query(ir.Edge(anchor, ir.Relationship(type='R'), ir.Node('x', ('Shared',))),
                    None, (ir.Alias(ir.Literal(1), 'c'),))
    hidden = ir.Boolean('OR', (ir.Literal(True), ir.Comparison(ir.Count(body=work), '>', ir.Literal(0))))
    if extra_read == 'same_type_edge':
        other = ir.Query(ir.Edge(ir.Node('other_root', ('Root',)), ir.Relationship(type='R'),
                                 ir.Node('other', ('Shared',))), None, (ir.Literal(1),))
    else:
        other = ir.Query(ir.Node('other', ('Shared',)), None, (ir.Literal(1),))
    if extra_read == 'uid_union_column':
        # This consumer observes UID values and another output kind rather
        # than merely asking how many matching nodes exist.
        first = ir.Query(anchor, None, (ir.Alias(hidden, 'value'),))
        second = replace(other, returns=(ir.Alias(ir.Property('other', 'uid'), 'value'),))
        query = ir.UnionAll((first, second))
        address = ('query', 'branches', 0, 'returns', 0, 'expression', 'terms', 1, 'left', 'body', 'pattern')
    else:
        query = ir.Query(anchor, None, (ir.Alias(hidden, 'flag'), ir.Alias(ir.Count(body=other), 'n')))
        address = ('query', 'returns', 0, 'expression', 'terms', 1, 'left', 'body', 'pattern')
    # Above the full-interpretation cutoff, a bounded proof graph alone cannot
    # justify preserving these extra reads. Every compatible physical domain
    # needs its own exclusion/masking argument, regardless of symbol identity.
    with pytest.raises(PopulationError, match='growing|growth|visible|observation|closure'):
        synthesize_relational_state(query, scale=5001, data_seed=0,
                                   demands=(MatchDemand(address, 'work', scale_matches=True),))


def test_large_growth_cannot_use_an_optional_false_predicate_as_an_empty_input_proof():
    anchor = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    query = ir.Query(ir.Edge(anchor, ir.Relationship(type='R'), ir.Node('x', ('Work',))), None,
                     (ir.Alias(ir.Literal(1), 'value'),), matches=(
                         ir.Match(ir.Node('z', ('Other',)), ir.Literal(False), optional=True),
                     ))
    # OPTIONAL's failed predicate null-extends each input row; it does not
    # eliminate the growing mandatory input.
    with pytest.raises(PopulationError, match='growing|growth|visible|observation|closure'):
        synthesize_relational_state(query, scale=5001, data_seed=0,
                                   demands=(MatchDemand(('query', 'pattern'), 'work', scale_matches=True),))


def test_large_growth_guard_uses_its_lexical_binding_not_a_same_named_union_variable():
    anchor = ir.Node('u', ('Root',), (('id', ir.Literal(1)), ('enabled', ir.Literal(False))))
    work = ir.Query(ir.Edge(anchor, ir.Relationship(type='R'), ir.Node('x', ('Work',))),
                    None, (ir.Literal(1),))
    guard = ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True))
    first = ir.Query(anchor, None, (ir.Alias(ir.Case(guard, ir.Literal(0), ir.Count(body=work)), 'c'),))
    second = ir.Query(ir.Node('u', ('Other',), (('id', ir.Literal(2)), ('enabled', ir.Literal(True)))),
                      None, (ir.Alias(ir.Literal(0), 'c'),))
    query = ir.UnionAll((first, second))
    ir.validate_query(query)
    # The first CASE selects its expensive ELSE. The unrelated second u must
    # not overwrite that binding and certify the ELSE as unselected.
    with pytest.raises(PopulationError, match='growing|growth|visible|observation|closure'):
        synthesize_relational_state(query, scale=5001, data_seed=0, demands=(
            MatchDemand(('query', 'branches', 0, 'returns', 0, 'expression', 'otherwise', 'body', 'pattern'),
                        'work', scale_matches=True),
        ))


def test_large_growth_guard_closes_alias_producers_before_resolving_a_rebound_name():
    old = ir.Node('u', ('A',), (('id', ir.Literal(1)), ('enabled', ir.Literal(False))))
    current = ir.Node('u', ('B',), (('id', ir.Literal(2)), ('enabled', ir.Literal(True))))
    work = ir.Query(ir.Edge(current, ir.Relationship(type='R'), ir.Node('x', ('Work',))),
                    None, (ir.Literal(1),))
    guard = ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Variable('saved'))
    query = ir.Query(None, None, (ir.Alias(ir.Case(guard, ir.Literal(0), ir.Count(body=work)), 'value'),),
                     pipeline=(ir.Match(old), ir.With((ir.Alias(ir.Property('u', 'enabled'), 'saved'),)),
                               ir.Match(current)))
    ir.validate_query(query)
    # saved belongs to the dropped A binding, while u now denotes B. Closing
    # saved's producer must not overwrite u when evaluating the final guard.
    with pytest.raises(PopulationError, match='growing|growth|visible|observation|closure'):
        synthesize_relational_state(query, scale=5001, data_seed=0, demands=(
            MatchDemand(('query', 'returns', 0, 'expression', 'otherwise', 'body', 'pattern'),
                        'work', scale_matches=True),
        ))
