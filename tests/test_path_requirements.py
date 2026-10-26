"""Path quantities, three-valued ALL and length intervals share goal lowering."""

from dataclasses import replace

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import plan_mutation_with_correspondence
from population import MatchDemand, PathDemand, PopulationError
from requirements import derive_requirement_plans


def leaf(*, bounds=(1, 4), filtered=True, predicate=None):
    root = ir.Node('u', ('Start',), (('id', ir.Literal(1)),))
    path = ir.Edge(root, ir.Relationship(type='R', min_hops=bounds[0], max_hops=bounds[1]),
                   ir.Node('v', ('End',)), 'undirected', path_variable='p')
    every = ir.All('x', ir.Call('nodes', (ir.Variable('p'),)),
                   predicate or ir.Comparison(ir.Property('x', 'keep'), '=', ir.Literal(True)))
    query = ir.Query(path, every if filtered else None,
                     (ir.Alias(every, 'truth'), ir.Alias(ir.Call('length', (ir.Variable('p'),)), 'hops')))
    return query


def plans(source, goals, *, work=False):
    site, = find_mutation_sites(source, 'P3', 'bounded_path')
    target, links = plan_mutation_with_correspondence(source, site)
    if work:
        goals += ({'kind': 'work', 'mode': 'auto'},)
    return derive_requirement_plans(source, target, site, goals=goals, scale=7, correspondence=links)


def test_guarded_path_quantity_is_inferred_from_rows_and_work_not_a_recipe():
    plan = next(plans(leaf(), ({'kind': 'output', 'min_rows': 3},), work=True))
    matches = [d for d in plan.demands if isinstance(d, MatchDemand)]
    assert len(matches) == 2
    assert {d.exact_matches for d in matches} == {3}
    assert {d.candidate_matches for d in matches} == {10}
    assert all(d.candidate_kind == 'path_false_prefix' for d in matches)
    paths = [d for d in plan.demands if isinstance(d, PathDemand)]
    assert len(paths) == 2
    assert all(2 <= d.min_length == d.max_length <= 4 for d in paths)
    assert all(any(truth is True for _, truth in d.all_values) for d in paths)
    assert plan.evidence['work']['potential_extra_candidate_work'] == 7


def test_outer_count_exact_value_reaches_the_same_path_quantity():
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=leaf()), 'n'),))
    goals = ({'kind': 'output', 'min_rows': 1},
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': 5})
    plan = next(plans(source, goals, work=True))
    assert {d.exact_matches for d in plan.demands if isinstance(d, MatchDemand)} == {5}
    assert any(step['operator'] == 'Count' and step.get('intervals') == [[5, 5]]
               for step in plan.evidence['inward_steps'])


def test_zero_count_can_keep_the_guard_and_grow_only_rejected_paths():
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=leaf()), 'n'),))
    goals = ({'kind': 'output', 'min_rows': 1},
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': 0})
    plan = next(plans(source, goals, work=True))
    assert {d.exact_matches for d in plan.demands if isinstance(d, MatchDemand)} == {0}
    assert {d.candidate_matches for d in plan.demands if isinstance(d, MatchDemand)} == {7}


def test_length_comparisons_intersect_with_pattern_bounds_and_outer_count():
    query = leaf()
    length = ir.Call('length', (ir.Variable('p'),))
    query = replace(query, where=ir.Boolean('AND', (query.where,
        ir.Comparison(length, '>=', ir.Literal(2)), ir.Comparison(length, '<', ir.Literal(4)))))
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=query), 'n'),))
    goals = ({'kind': 'output', 'min_rows': 1},
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': 4})
    plan = next(plans(source, goals, work=True))
    assert {d.exact_matches for d in plan.demands if isinstance(d, MatchDemand)} == {4}
    assert all(2 <= d.min_length == d.max_length < 4 for d in plan.demands if isinstance(d, PathDemand))


@pytest.mark.parametrize('truth,transfer', [(True, 'all_nodes_true'), (False, 'at_least_one_false'),
                                          (None, 'no_false_and_at_least_one_null')])
def test_projected_all_preserves_true_false_and_unknown(truth, transfer):
    source = leaf(filtered=False)
    plan = next(plans(source, ({'kind': 'output', 'min_rows': 1},
        {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': truth})))
    path, = [d for d in plan.demands if isinstance(d, PathDemand)]
    assert path.all_values == ((('query', 'returns', 0, 'expression'), truth),)
    assert any(step.get('transfer') == transfer and step['includes_endpoints']
               for step in plan.evidence['inward_steps'])


def test_exact_projected_length_reaches_its_path_producer():
    source = leaf(filtered=False)
    plan = next(plans(source, ({'kind': 'output', 'min_rows': 1},
        {'kind': 'value', 'address': ('query', 'returns', 1, 'expression'), 'value': 3})))
    path, = [d for d in plan.demands if isinstance(d, PathDemand)]
    assert (path.min_length, path.max_length) == (3, 3)


@pytest.mark.parametrize('truth', [True, False, None])
def test_all_fact_follows_a_with_alias_and_bare_return_export(truth):
    query = leaf(filtered=False)
    source = ir.Query(None, None, (ir.Variable('observed'),), pipeline=(ir.Match(query.pattern),
        ir.With((ir.Alias(query.returns[0].expression, 'observed'),))))
    plan = next(plans(source, ({'kind': 'output', 'min_rows': 1},
        {'kind': 'value', 'address': ('query', 'returns', 0), 'value': truth})))
    path, = [d for d in plan.demands if isinstance(d, PathDemand)]
    assert path.all_values == ((('query', 'pipeline', 1, 'projections', 0, 'expression'), truth),)


@pytest.mark.parametrize('truth', [False, None])
def test_where_cannot_simultaneously_supply_nonempty_rows_and_nontrue_all(truth):
    source = leaf()
    with pytest.raises(PopulationError, match='ALL|allocation'):
        next(plans(source, ({'kind': 'output', 'min_rows': 1},
            {'kind': 'value', 'address': ('query', 'where'), 'value': truth})))


@pytest.mark.parametrize('value', [0, 5, None, False])
def test_path_length_cannot_escape_bounds_or_become_null_or_boolean(value):
    source = leaf(filtered=False)
    with pytest.raises(PopulationError, match='length|truth|allocation'):
        next(plans(source, ({'kind': 'output', 'min_rows': 1},
            {'kind': 'value', 'address': ('query', 'returns', 1, 'expression'), 'value': value})))


def test_all_unknown_is_not_a_safe_false_prefix_growth_witness():
    source = leaf(filtered=False)
    with pytest.raises(PopulationError, match='ALL|allocation'):
        next(plans(source, ({'kind': 'output', 'min_rows': 1},
            {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': None}), work=True))


def test_one_hop_path_has_no_interior_false_prefix_suffix_axis():
    with pytest.raises(PopulationError, match='ALL|allocation'):
        next(plans(leaf(bounds=(1, 1)), ({'kind': 'output', 'min_rows': 1},), work=True))


def test_nonempty_path_all_has_no_empty_list_vacuity():
    source = leaf(filtered=False, predicate=ir.Literal(None))
    with pytest.raises(PopulationError, match='nonempty|allocation'):
        next(plans(source, ({'kind': 'output', 'min_rows': 1},
            {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': True})))
