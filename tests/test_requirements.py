"""Inward row/truth goals are derived from consumers and lexical producers."""

from dataclasses import replace
from math import prod

import pytest

import ir
from dependencies import find_mutation_sites, node_at
from mutator import plan_mutation_with_correspondence
from population import PopulationError, evaluate_query, synthesize_relational_state
from requirements import derive_requirement_plans


def region(name):
    root = ir.Node('root_' + name, ('Anchor',), (('key', ir.Literal(4)),))
    return ir.Query(ir.Edge(root, ir.Relationship(type=name), ir.Node('item_' + name, ('Leaf_' + name,))),
                    None, (ir.Alias(ir.Literal(1), 'value'),))


def source_with_guard(guard):
    return ir.ConditionalQuery(guard, region('selected'), region('low'))


def solve(source, *, minimum=1, scale=7):
    site, = find_mutation_sites(source, 'S3', 'conditional')
    alternative, correspondence = plan_mutation_with_correspondence(source, site, replacement=region('high'))
    plans = derive_requirement_plans(source, alternative, site,
        goals=({'kind': 'output', 'min_rows': minimum}, {'kind': 'work', 'mode': 'masked'}),
        scale=scale, correspondence=correspondence)
    for plan in plans:
        populated = synthesize_relational_state(source, alternatives=(alternative,), scale=scale, data_seed=0,
            demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=minimum)
        return plan, populated
    pytest.fail('No candidate requirement allocation')


@pytest.mark.parametrize('operator,desired_bound', [('>', 4), ('>=', 3), ('=', 3)])
def test_count_guards_lower_to_integer_row_intervals(operator, desired_bound):
    guard = ir.Comparison(ir.Count(body=region('guard')), operator, ir.Literal(3))
    plan, populated = solve(source_with_guard(guard))
    assert len(evaluate_query(guard.body if isinstance(guard, ir.Count) else guard.left.body, populated.spec)) == desired_bound
    bound = next(item for item in plan.evidence['cardinality_bounds'] if item['address'] == ['query', 'condition', 'left', 'body'])
    assert bound['minimum'] == desired_bound
    assert bound['maximum'] == (3 if operator == '=' else None)


def test_false_requirement_through_not_count_can_keep_a_keyed_anchor_and_empty_the_edge_domain():
    count = ir.Count(body=region('guard'))
    guard = ir.Negation(ir.Comparison(count, '>', ir.Literal(2)))
    plan, populated = solve(source_with_guard(guard))
    assert evaluate_query(ir.Query(None, None, (ir.Alias(count, 'n'),)), populated.spec) == [{'n': 0}]
    assert any(item['maximum'] == 2 for item in plan.evidence['cardinality_bounds'])
    roots = [node for node in populated.spec.iter_nodes() if 'Anchor' in node['labels']]
    assert roots, 'a false existential/count demand must not delete the shared keyed anchor'


def test_false_filtered_existence_keeps_closed_guard_facts_instead_of_a_local_filter_fact():
    body = region('guard')
    body = replace(body, where=ir.Comparison(ir.Property('item_guard', 'keep'), '=', ir.Literal(True)))
    source = source_with_guard(ir.Negation(ir.Exists(body=body)))
    plan, populated = solve(source)
    assert any(fact.value is False and fact.address == ('query', 'condition', 'expression') for fact in plan.obligations)
    assert not any(fact.address[:5] == ('query', 'condition', 'expression', 'body', 'where') for fact in plan.obligations)
    guard_step = next(step for step in plan.evidence['inward_steps'] if step['address'] == ['query', 'condition', 'expression'])
    local_step = next(step for step in plan.evidence['inward_steps'] if step['address'] == ['query', 'condition', 'expression', 'body', 'where'])
    assert guard_step['quantifier'] == 'input_domain'
    assert local_step['quantifier'] == 'matching_witness'
    # Every exported Boolean fact can be observed without leaking the item's
    # local binding out of its existential row scope.
    for fact in plan.obligations:
        assert fact.address[0] == 'query'
        expression = node_at(source, fact.address[1:])
        check = ir.Query(None, None, (ir.Alias(expression, 'fact'),))
        ir.validate_query(check)
        assert evaluate_query(check, populated.spec) == [{'fact': fact.value}]


def test_both_count_dependencies_of_a_true_conjunction_receive_requirements():
    left = ir.Comparison(ir.Count(body=region('guard_a')), '>', ir.Literal(2))
    right = ir.Comparison(ir.Count(body=region('guard_b')), '>=', ir.Literal(5))
    plan, populated = solve(source_with_guard(ir.Boolean('AND', (left, right))))
    assert len(evaluate_query(left.left.body, populated.spec)) == 3
    assert len(evaluate_query(right.left.body, populated.spec)) == 5
    assert sorted(item['minimum'] for item in plan.evidence['cardinality_bounds'] if item['minimum'] > 1) == [3, 5]


def test_a_false_literal_does_not_prevent_another_disjunct_from_satisfying_the_guard():
    guard = ir.Boolean('OR', (ir.Literal(False), ir.Comparison(ir.Count(body=region('guard')), '>', ir.Literal(3))))
    _, populated = solve(source_with_guard(guard))
    assert len(evaluate_query(region('guard'), populated.spec)) == 4


def test_incompatible_literal_guard_is_rejected_as_unsatisfied_not_as_an_empty_success():
    with pytest.raises(PopulationError, match='literal|truth'):
        solve(source_with_guard(ir.Literal(False)))


def test_repeat_allocation_follows_bound_variables_and_outer_count_instead_of_schema_or_eight():
    nodes = [ir.Node('a', ('Same',), (('key', ir.Literal(4)),)),
             ir.Node('b', ('Same',)), ir.Node('c', ('Same',)), ir.Node('d', ('Same',))]
    parts = tuple(ir.Match(ir.Edge(left, ir.Relationship(type='R' + str(i)), right))
                  for i, (left, right) in enumerate(zip(nodes, nodes[1:])))
    chain = ir.Query(parts[0].pattern, None, tuple(ir.Property(node.variable, 'uid') for node in nodes), matches=parts[1:])
    source = ir.Query(None, None, (ir.Variable('number'),), pipeline=(
        ir.With((ir.Alias(ir.Count(body=chain), 'number'),), ir.Comparison(ir.Variable('number'), '>', ir.Literal(11))),))
    site = find_mutation_sites(source, 'P10', 'chain')[0]
    alternative, correspondence = plan_mutation_with_correspondence(source, site)
    plan = next(derive_requirement_plans(source, alternative, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'repeat'}), scale=7, correspondence=correspondence))
    quantities = [item['per_input_matches'] for item in plan.evidence['allocation']]
    assert prod(quantities) >= 12
    assert plan.evidence['work']['repetitions'] >= 2
    assert plan.evidence['work']['potential_extra_candidate_work'] >= 7
    assert not all(quantity in (1, 8) for quantity in quantities)
    populated = synthesize_relational_state(source, alternatives=(alternative,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
    assert populated.expected_rows[0]['number'] > 11


def test_fixed_anchor_count_cannot_be_enlarged_to_fake_an_impossible_outer_threshold():
    body = ir.Query(ir.Node('a', ('Anchor',), (('key', ir.Literal(4)),)), None, (ir.Literal(1),))
    guard = ir.Comparison(ir.Count(body=body), '>', ir.Literal(1))
    with pytest.raises(PopulationError, match='fixed input row|cardinality'):
        solve(source_with_guard(guard))


def test_a_marked_envelope_in_an_outer_else_propagates_false_to_the_outer_guard():
    outer_count = ir.Count(body=region('outer_guard'))
    inner = source_with_guard(ir.Comparison(ir.Count(body=region('inner_guard')), '>', ir.Literal(1)))
    source = ir.ConditionalQuery(ir.Comparison(outer_count, '>', ir.Literal(0)), region('outer_selected'), inner)
    site = next(site for site in find_mutation_sites(source, 'S3', 'conditional') if site.address == ('otherwise', 'otherwise'))
    alternative, correspondence = plan_mutation_with_correspondence(source, site, replacement=region('high'))
    plan = next(derive_requirement_plans(source, alternative, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'masked'}), scale=7, correspondence=correspondence))
    populated = synthesize_relational_state(source, alternatives=(alternative,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
    assert evaluate_query(ir.Query(None, None, (ir.Alias(outer_count, 'n'),)), populated.spec) == [{'n': 0}]
    assert populated.expected_rows
    assert any(step['address'] == ['query', 'condition'] and step.get('value') is False for step in plan.evidence['inward_steps'])
