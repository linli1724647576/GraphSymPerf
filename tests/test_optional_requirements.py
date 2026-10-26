"""OPTIONAL separates matching degree, null extension and real work inputs."""

from dataclasses import replace

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import plan_mutation_with_correspondence
from population import EntityBindingDemand, PopulationError, evaluate_query, synthesize_relational_state
from requirements import derive_requirement_plans


ROOT = ir.Node('u', ('InputRoot',), (('id', ir.Literal(1)),))


def branch(name):
    node = ir.Node(name, ('Domain_' + name,))
    return ir.Match(ir.Edge(ROOT, ir.Relationship(type='R_' + name), node),
                    ir.Comparison(ir.Property(name, 'keep'), '=', ir.Literal(True)), optional=True)


def optional_query():
    return ir.Query(ROOT, None, (ir.Property('a', 'uid'), ir.Property('b', 'uid')),
                    matches=(branch('a'), branch('b')))


def candidates(source, *, goals, mode=None, scale=7, form='optional_branches', pattern='P11'):
    site, = find_mutation_sites(source, pattern, form)
    target, correspondence = plan_mutation_with_correspondence(source, site)
    if mode is not None:
        goals += ({'kind': 'work', 'mode': mode},)
    return derive_requirement_plans(source, target, site, goals=goals, scale=scale,
                                    correspondence=correspondence), target, correspondence


def bindings(left, right, prefix=('query',)):
    return tuple({'kind': 'binding', 'address': (*prefix, 'matches', index, 'pattern', 'right'),
                  'relation': 'nonnull' if present else 'null'}
                 for index, present in enumerate((left, right)))


def populate(source, plan, target, correspondence):
    return synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)


@pytest.mark.parametrize('left,right', [(False, False), (True, False), (False, True), (True, True)])
def test_optional_matching_states_contribute_one_or_the_actual_positive_degree(left, right):
    source = optional_query()
    plans, target, correspondence = candidates(source, goals=({'kind': 'output', 'min_rows': 1}, *bindings(left, right)))
    plan = next(plans)
    demands = {d.address: d for d in plan.demands if d.address[0] == 'query'}
    for index, present in enumerate((left, right)):
        assert (demands[('query', 'matches', index, 'pattern')].exact_matches > 0) == present
    result = populate(source, plan, target, correspondence)
    assert result.expected_rows
    assert all((row['a.uid'] is not None) == left and (row['b.uid'] is not None) == right for row in result.expected_rows)
    steps = [step for step in plan.evidence['inward_steps'] if step.get('transfer') == 'max(1,m)']
    assert steps and {step['selected_transfer'] for step in steps} <= {'1', 'm'}


def test_outer_exact_count_uses_null_extension_one_and_infers_the_other_degree():
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=optional_query()), 'n'),))
    prefix = ('query', 'returns', 0, 'expression', 'body')
    goals = ({'kind': 'output', 'min_rows': 1}, *bindings(False, True, prefix),
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': 7})
    plans, target, correspondence = candidates(source, goals=goals)
    plan = next(plans)
    actual = {d.address: d.exact_matches for d in plan.demands}
    assert actual[(*prefix, 'matches', 0, 'pattern')] == 0
    assert actual[(*prefix, 'matches', 1, 'pattern')] == 7
    assert populate(source, plan, target, correspondence).expected_rows == [{'n': 7}]


def test_two_null_extended_bindings_cannot_satisfy_a_count_of_two():
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=optional_query()), 'n'),))
    prefix = ('query', 'returns', 0, 'expression', 'body')
    goals = ({'kind': 'output', 'min_rows': 1}, *bindings(False, False, prefix),
             {'kind': 'value', 'address': ('query', 'returns', 0, 'expression'), 'value': 2})
    plans, _, _ = candidates(source, goals=goals)
    with pytest.raises(PopulationError, match='fixed input|cardinality|allocation'):
        next(plans)


def test_neither_has_candidate_work_but_does_not_claim_nontrivial_repetition():
    source = optional_query()
    goals = ({'kind': 'output', 'min_rows': 1}, *bindings(False, False))
    plans, target, correspondence = candidates(source, goals=goals, mode='auto')
    plan = next(plans)
    work = plan.evidence['work']
    assert work['repetitions'] == 1 and work['nontrivial_repetition'] is False
    assert work['potential_extra_candidate_work'] >= 7
    assert all(row['a.uid'] is None and row['b.uid'] is None for row in populate(source, plan, target, correspondence).expected_rows)
    explicit, _, _ = candidates(source, goals=goals, mode='repeat')
    with pytest.raises(PopulationError, match='repeat|axis|allocation'):
        next(explicit)


def test_both_present_prefers_a_real_repeated_work_axis_without_fixed_degrees():
    source = optional_query()
    plans, target, correspondence = candidates(source,
        goals=({'kind': 'output', 'min_rows': 1}, *bindings(True, True)), mode='auto')
    plan = next(plans)
    assert plan.evidence['work']['repetitions'] >= 2
    assert plan.evidence['work']['nontrivial_repetition']
    assert populate(source, plan, target, correspondence).expected_rows


def nullable_independent(*, present=False, renamed=False):
    nullable = ir.Node('n', ('Nullable',))
    optional = ir.Match(ir.Edge(ROOT, ir.Relationship(type='OPTIONAL_R'), nullable), ir.Literal(present), optional=True)
    parts = [ir.Match(ROOT), optional]
    if renamed:
        parts.append(ir.With((ir.Variable('u'), ir.Alias(ir.Variable('n'), 'saved'))))
    incoming = ir.Node('saved', ('Nullable',)) if renamed else nullable
    scan = ir.Node('a', ('Independent',))
    edge = ir.Edge(incoming, ir.Relationship(type='B'), ir.Node('b', ('Cost',)))
    parts.extend((ir.Match(scan), ir.Match(edge, ir.Comparison(ir.Property('b', 'keep'), '=', ir.Literal(True)))))
    flow = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'c'),), pipeline=tuple(parts))
    return ir.Query(None, None, (ir.Alias(ir.Count(body=flow), 'n'),))


@pytest.mark.parametrize('renamed', [False, True])
def test_null_imported_entities_cannot_supply_a_repeated_edge_work_axis(renamed):
    source = nullable_independent(renamed=renamed)
    plans, _, _ = candidates(source, goals=({'kind': 'output', 'min_rows': 1},), mode='auto', pattern='P10', form='independent')
    with pytest.raises(PopulationError, match='matching|entity|binding|axis|allocation'):
        next(plans)


def test_nonnull_entity_alias_reuses_the_original_optional_binding_and_can_expand():
    source = nullable_independent(present=True, renamed=True)
    plans, target, correspondence = candidates(source, goals=({'kind': 'output', 'min_rows': 1},), mode='auto', pattern='P10', form='independent')
    plan = next(plans)
    assert plan.evidence['work']['repetitions'] >= 2
    assert any(isinstance(fact, EntityBindingDemand) and fact.relation == 'nonnull' for fact in plan.obligations)
    result = populate(source, plan, target, correspondence)
    flow = source.returns[0].expression.body
    prefix = replace(flow, pipeline=flow.pipeline[:-1], returns=(ir.Property('saved', 'uid'),))
    inputs = {row['saved.uid'] for row in evaluate_query(prefix, result.spec)}
    assert inputs and None not in inputs
    assert len([edge for edge in result.spec.iter_edges() if edge['type'] == 'B' and edge['source'] in inputs]) >= 7


def test_null_ordinary_property_does_not_force_the_entity_to_be_null():
    source = optional_query()
    source = replace(source, returns=(ir.Property('a', 'uid'), ir.Property('a', 'tag')))
    goals = ({'kind': 'output', 'min_rows': 1}, *bindings(True, False),
             {'kind': 'value', 'address': ('query', 'returns', 1), 'value': None})
    plans, target, correspondence = candidates(source, goals=goals)
    result = populate(source, next(plans), target, correspondence)
    assert result.expected_rows[0]['a.uid'] is not None and result.expected_rows[0]['a.tag'] is None


@pytest.mark.parametrize('renamed', [False, True])
def test_direct_scalar_count_cannot_read_a_null_optional_import(renamed):
    nullable = ir.Node('n', ('Nullable',))
    optional = ir.Match(ir.Edge(ROOT, ir.Relationship(type='OPTIONAL_R'), nullable), ir.Literal(False), optional=True)
    parts = [ir.Match(ROOT), optional]
    if renamed:
        parts.append(ir.With((ir.Variable('u'), ir.Alias(ir.Variable('n'), 'saved'))))
    incoming = ir.Node('saved', ('Nullable',)) if renamed else nullable
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    source = ir.Query(None, None, (ir.Alias(expression, 'value'),), pipeline=tuple(parts))
    high = ir.Count(ir.Edge(incoming, ir.Relationship(type='B'), ir.Node('b', ('Cost',))))
    site, = find_mutation_sites(source, 'S7', 'coalesce')
    target, correspondence = plan_mutation_with_correspondence(source, site, replacement=high)
    with pytest.raises(PopulationError, match='matching|binding|entity|allocation'):
        next(derive_requirement_plans(source, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))
