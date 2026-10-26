"""Adversarial boundaries for path facts, row counts and lexical aliases."""

from dataclasses import replace

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import plan_mutation_with_correspondence
from population import (MatchDemand, PathDemand, PopulationError, evaluate_query,
                        synthesize_relational_state)
from requirements import derive_requirement_plans
from test_path_goal_acceptance import ANCHOR, path_body, skeleton, solve


def native_pair(source):
    site, = find_mutation_sites(source, 'P3', 'bounded_path')
    target, links = plan_mutation_with_correspondence(source, site)
    return target, links, site


def test_reversed_copy_length_goals_intersect_on_the_same_physical_paths():
    source = path_body(filtered=False, projection='bindings')
    target, links, site = native_pair(source)
    goals = ({'kind': 'output', 'min_rows': 1},
             {'kind': 'value', 'address': ('query', 'returns', 2, 'expression'), 'value': 2},
             {'kind': 'value', 'address': ('alternatives', 0, 'returns', 2, 'expression'), 'value': 3})
    with pytest.raises(PopulationError, match='length|allocation'):
        next(derive_requirement_plans(source, target, site, goals=goals, scale=3, correspondence=links))


def test_low_level_source_and_copy_path_length_demands_cannot_disagree():
    source = path_body()
    target, links, _ = native_pair(source)
    demands = (MatchDemand(('query', 'pattern'), 'valid', exact_matches=1),
               PathDemand(('query', 'pattern'), min_length=2, max_length=2),
               PathDemand(('alternatives', 0, 'pattern'), min_length=3, max_length=3))
    with pytest.raises(PopulationError, match='length.*intersection'):
        synthesize_relational_state(source, alternatives=(target,), scale=3, data_seed=0,
            demands=demands, correspondence=links, min_result_rows=1)


@pytest.mark.parametrize('state', [False, None])
def test_positive_all_filter_cannot_supply_positive_witnesses_with_nontrue_all_fact(state):
    source = path_body()
    target, links, _ = native_pair(source)
    demands = (MatchDemand(('query', 'pattern'), 'valid', exact_matches=1),
               PathDemand(('query', 'pattern'), min_length=2, max_length=2,
                          all_values=((('query', 'where'), state),)))
    with pytest.raises(PopulationError, match='PathDemand|witness'):
        synthesize_relational_state(source, alternatives=(target,), scale=3, data_seed=0,
            demands=demands, correspondence=links, min_result_rows=1)


@pytest.mark.parametrize('kind,state', [('negated', False), ('nulltest', None)])
def test_nontrue_all_facts_are_checked_on_real_surviving_match_witnesses(kind, state):
    source = path_body(projection='truth')
    where = (ir.Negation(source.where) if kind == 'negated'
             else ir.Comparison(source.where, 'IS NULL', ir.Literal(None)))
    source = replace(source, where=where)
    target, links, _ = native_pair(source)
    address = ('query', 'where', 'expression' if kind == 'negated' else 'left')
    demands = (MatchDemand(('query', 'pattern'), 'valid', exact_matches=1),
               PathDemand(('query', 'pattern'), min_length=2, max_length=2, all_values=((address, state),)))
    result = synthesize_relational_state(source, alternatives=(target,), scale=3, data_seed=0,
        demands=demands, correspondence=links, min_result_rows=1)
    assert result.expected_rows == [{'truth': state}]
    assert evaluate_query(target, result.spec) == result.expected_rows


def test_unknown_all_does_not_pass_a_negated_filter_as_false_would():
    source = path_body()
    source = replace(source, where=ir.Negation(source.where))
    target, links, _ = native_pair(source)
    demands = (MatchDemand(('query', 'pattern'), 'valid', exact_matches=1),
               PathDemand(('query', 'pattern'), min_length=2, max_length=2,
                          all_values=((('query', 'where', 'expression'), None),)))
    with pytest.raises(PopulationError, match='PathDemand|witness'):
        synthesize_relational_state(source, alternatives=(target,), scale=3, data_seed=0,
            demands=demands, correspondence=links, min_result_rows=1)


def nested_counts(inner_count, outer_count):
    body = path_body(bounds=(2, 4), length=2)
    middle = ir.Query(None, None, (ir.Alias(ir.Count(body=body), 'inner_count'),))
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=middle), 'outer_count'),))
    goals = ({'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': outer_count},
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression', 'body',
                                          'returns', 0, 'expression'), 'value': inner_count})
    return skeleton(source, extra_goals=goals)


@pytest.mark.parametrize('inner_count', [0, 4])
def test_count_of_a_scalar_projection_counts_one_row_not_its_inner_value(inner_count):
    solved, _ = solve(nested_counts(inner_count, 1))
    assert solved.population.expected_rows == [{'outer_count': 1}]
    quantities = solved.construction_record['requirements']['derivation']['allocation']
    assert len(quantities) == 1 and quantities[0]['per_input_matches'] == inner_count


@pytest.mark.parametrize('outer_count', [0, 2])
def test_inner_count_value_cannot_change_the_outer_projection_row_cardinality(outer_count):
    with pytest.raises(PopulationError, match='fixed input|cardinality|allocation'):
        solve(nested_counts(4, outer_count))


def shadowed_count(final):
    count = ir.Count(body=path_body(bounds=(2, 4), length=2))
    source = ir.Query(None, None, (ir.Variable('n'),), pipeline=(
        ir.Match(ANCHOR),
        ir.With((ir.Variable('u'), ir.Alias(count, 'n'))),
        ir.With((ir.Variable('u'), ir.Alias(ir.Literal(3), 'n'))),
    ))
    goals = ({'kind': 'value', 'address': ('query', 'pipeline', 1, 'projections', 1, 'expression'), 'value': 4},
             {'kind': 'value', 'address': ('query', 'returns', 0), 'value': final})
    return skeleton(source, extra_goals=goals)


def test_same_name_count_and_literal_aliases_keep_separate_scalar_producers():
    solved, _ = solve(shadowed_count(3))
    assert solved.population.expected_rows == [{'n': 3}]
    quantities = solved.construction_record['requirements']['derivation']['allocation']
    assert len(quantities) == 1 and quantities[0]['per_input_matches'] == 4


def test_shadowed_count_value_cannot_satisfy_a_requirement_on_the_later_literal_alias():
    with pytest.raises(PopulationError, match='literal|allocation'):
        solve(shadowed_count(4))
