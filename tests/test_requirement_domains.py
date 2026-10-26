"""Three-valued logical/domain branching and work-axis provenance."""

from dataclasses import replace
from itertools import islice

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import plan_mutation_with_correspondence
from population import PopulationError, evaluate_query, synthesize_relational_state
from requirements import derive_requirement_plans


def rows(name, root=None):
    root = root or ir.Node('root_' + name, ('Root',), (('id', ir.Literal(1)),))
    return ir.Query(ir.Edge(root, ir.Relationship(type=name), ir.Node('item_' + name, ('Leaf_' + name,))),
                    None, (ir.Alias(ir.Literal(1), 'c'),))


def selected():
    return ir.ConditionalQuery(ir.Literal(True), rows('yes'), rows('low'))


def flag(name):
    return ir.Comparison(ir.Property('u', name), '=', ir.Literal(True))


def plans(source, *, guard_value=None, seed=0, mark=None):
    sites = find_mutation_sites(source, 'S3', 'conditional')
    site = next(site for site in sites if site.address == mark) if mark is not None else sites[-1]
    target, correspondence = plan_mutation_with_correspondence(source, site, replacement=rows('high'))
    if guard_value is not ... and guard_value in (True, False, None):
        condition = next(path for path, value in located(source) if isinstance(value, ir.ConditionalQuery)) + ('condition',)
        if tuple(site.guards['envelope_site']) != condition[:-1]:
            site = replace(site, guards={**site.guards, 'premises': [*site.guards['premises'],
                {'address': list(condition), 'relation': {True: 'true', False: 'false', None: 'null'}[guard_value]}]})
    result = derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': seed}),
        scale=7, correspondence=correspondence)
    return result, target, correspondence


def located(value, path=()):
    from dataclasses import fields, is_dataclass
    yield path, value
    if is_dataclass(value):
        for field in fields(value):
            yield from located(getattr(value, field.name), (*path, field.name))
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            yield from located(item, (*path, i))


def solve(source, **options):
    candidates, target, correspondence = plans(source, **options)
    rejected = 0
    for plan in candidates:
        try:
            populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
                demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
        except PopulationError:
            rejected += 1
            continue
        return plan, populated, rejected
    pytest.fail('All finite logical/cardinality candidates were rejected')


@pytest.mark.parametrize('operator', ['AND', 'OR'])
def test_null_boolean_operands_follow_the_three_valued_truth_table(operator):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    condition = ir.Boolean(operator, (flag('a'), flag('b')))
    outer = ir.ConditionalQuery(condition, rows('outer_yes'), selected())
    source = ir.Query(root, None, (ir.Alias(ir.Count(body=outer), 'n'),))
    plan, populated, _ = solve(source, guard_value=None)
    facts = {fact.address: fact.value for fact in plan.obligations}
    assert any(value is None for value in facts.values())
    check = ir.Query(root, None, (ir.Alias(condition, 'value'),))
    assert evaluate_query(check, populated.spec) == [{'value': None}]
    operands = next(step for step in plan.evidence['inward_steps'] if step['operator'] == 'Boolean')
    assert operands['value'] is None and operands['quantifier'] == 'input_domain'


def test_not_preserves_null_instead_of_coercing_it_to_true():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    condition = ir.Negation(flag('nullable'))
    outer = ir.ConditionalQuery(condition, rows('outer_yes'), selected())
    source = ir.Query(root, None, (ir.Alias(ir.Count(body=outer), 'n'),))
    _, populated, _ = solve(source, guard_value=None)
    assert evaluate_query(ir.Query(root, None, (ir.Alias(condition, 'n'),)), populated.spec) == [{'n': None}]


def test_case_with_null_condition_selects_else_and_can_produce_a_null_guard():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    condition = ir.Case(ir.Literal(None), ir.Literal(True), flag('nullable'))
    outer = ir.ConditionalQuery(condition, rows('outer_yes'), selected())
    source = ir.Query(root, None, (ir.Alias(ir.Count(body=outer), 'n'),))
    plan, populated, _ = solve(source, guard_value=None)
    assert evaluate_query(ir.Query(root, None, (ir.Alias(condition, 'n'),)), populated.spec) == [{'n': None}]
    assert any(fact.value is None and fact.address[-1] == 'condition' for fact in plan.obligations)


def test_else_can_backtrack_from_false_to_null_when_the_outer_state_requires_null():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    condition = ir.Comparison(ir.Property('u', 'nullable'), '=', ir.Literal(True))
    outer = ir.ConditionalQuery(condition, rows('outer_yes'), selected())
    source = ir.Query(root, ir.Comparison(ir.Property('u', 'nullable'), 'IS NULL', ir.Literal(None)),
                      (ir.Alias(ir.Count(body=outer), 'n'),))
    # No explicit NULL premise: the outer row constraint and branch semantics
    # must reject FALSE and let the NULL candidate reach the graph solver.
    plan, populated, rejected = solve(source, guard_value=...)
    assert rejected > 0
    assert any(fact.value is None for fact in plan.obligations)
    assert evaluate_query(ir.Query(root, None, (ir.Alias(condition, 'n'),)), populated.spec) == [{'n': None}]


@pytest.mark.parametrize('order', ['lower_first', 'not_equal_first'])
def test_count_not_equal_union_intersects_lower_bound_at_four(order):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    counter = ir.Count(body=rows('counted'))
    comparisons = (ir.Comparison(ir.Variable('n'), '>', ir.Literal(2)),
                   ir.Comparison(ir.Variable('n'), '<>', ir.Literal(3)))
    if order == 'not_equal_first':
        comparisons = tuple(reversed(comparisons))
    source = ir.Query(None, None, (ir.Variable('n'), ir.Alias(ir.Count(body=selected()), 'selected')), pipeline=(
        ir.Match(root), ir.With((ir.Variable('u'), ir.Alias(counter, 'n')), ir.Boolean('AND', comparisons))))
    plan, populated, _ = solve(source, guard_value=...)
    assert populated.expected_rows == [{'n': 4, 'selected': 1}]
    assert any(domain['intervals'] == [[4, None]] for domain in plan.evidence['count_domains'])


def test_boolean_allocation_backtracks_when_its_first_satisfying_assignment_conflicts_with_outer_rows():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    condition = ir.Boolean('OR', (flag('a'), flag('b')))
    source = ir.Query(root, ir.Comparison(ir.Property('u', 'b'), '=', ir.Literal(False)),
                      (ir.Alias(ir.Count(body=ir.ConditionalQuery(condition, selected(), rows('outer_no'))), 'n'),))
    # The marked inner conditional lies in the outer THEN, so OR must be TRUE.
    plan, populated, rejected = solve(source, guard_value=...)
    assert rejected > 0
    assert evaluate_query(ir.Query(root, None, (ir.Alias(condition, 'v'), ir.Alias(ir.Property('u', 'b'), 'b'))),
                          populated.spec) == [{'v': True, 'b': False}]
    assert plan.evidence['search_budget']['complete'] is False


def chain():
    nodes = [ir.Node('u', ('Root',), (('id', ir.Literal(1)),)), ir.Node('v', ('V',)), ir.Node('w', ('W',)), ir.Node('n', ('N',))]
    parts = tuple(ir.Match(ir.Edge(left, ir.Relationship(type='R' + str(i)), right),
                          ir.Comparison(ir.Property(right.variable, 'good'), '=', ir.Literal(True)))
                  for i, (left, right) in enumerate(zip(nodes, nodes[1:])))
    return ir.Query(parts[0].pattern, parts[0].where, tuple(ir.Property(node.variable, 'uid') for node in nodes), matches=parts[1:])


def test_auto_enumerates_the_moved_atoms_only_and_seed_rotates_the_first_axis():
    source = chain()
    site = find_mutation_sites(source, 'P10', 'chain')[0]
    target, correspondence = plan_mutation_with_correspondence(source, site)
    def candidates(seed):
        return derive_requirement_plans(source, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': seed}),
            scale=7, correspondence=correspondence)
    first, second = list(islice(candidates(0), 2))
    assert {first.evidence['work']['side'], second.evidence['work']['side']} == {'query', 'alternatives'}
    assert next(candidates(1)).evidence['work']['address'] != first.evidence['work']['address']
    origins = set()
    for plan in (first, second):
        for demand in plan.demands:
            if demand.candidate_matches is not None and demand.address[0] == 'query':
                origins.add(demand.address)
        assert plan.evidence['work']['potential_extra_candidate_work'] >= 7
        populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
            demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
        assert populated.expected_rows
    assert origins == {('query', 'pattern'), ('query', 'matches', 0, 'pattern')}
    assert ('query', 'matches', 1, 'pattern') not in origins


def test_producer_growth_work_has_real_extra_prefix_paths_then_fails_the_later_predicate():
    source = chain()
    site = find_mutation_sites(source, 'P10', 'chain')[0]
    target, correspondence = plan_mutation_with_correspondence(source, site)
    candidates = derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': 1}), scale=7,
        correspondence=correspondence)
    plan = next(candidates)
    assert plan.evidence['work']['axis_kind'] == 'producer_growth_through_prefix'
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
    nodes = {node['uid']: node for node in populated.spec.iter_nodes()}
    bad = {uid for uid, node in nodes.items() if 'V' in node['labels'] and node['properties'].get('good') is not True}
    prefix_paths = [edge for edge in populated.spec.iter_edges() if edge['type'] == 'R1' and edge['source'] in bad]
    assert len(prefix_paths) >= 7
    assert all(nodes[edge['source']]['properties'].get('good') is False for edge in prefix_paths)


def test_a_label_removal_that_the_prefix_cannot_read_is_not_a_producer_growth_axis():
    source = chain()
    source = replace(source, where=None)
    site = find_mutation_sites(source, 'P10', 'chain')[0]
    target, correspondence = plan_mutation_with_correspondence(source, site)
    candidates = derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}), scale=7,
        correspondence=correspondence)
    assert all(plan.evidence['work']['axis_kind'] != 'producer_growth_through_prefix' for plan in islice(candidates, 3))


def test_tautological_right_filter_cannot_fall_back_to_left_population_and_fake_prefix_work():
    source = chain()
    value = ir.Property('v', 'flag')
    source = replace(source, where=ir.Boolean('OR', (ir.Comparison(value, 'IS NULL', ir.Literal(None)),
                                                    ir.Comparison(value, 'IS NOT NULL', ir.Literal(None)))))
    site = find_mutation_sites(source, 'P10', 'chain')[0]
    target, correspondence = plan_mutation_with_correspondence(source, site)
    candidates = derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': 1}), scale=7,
        correspondence=correspondence)
    impossible = next(candidates)
    assert impossible.evidence['work']['axis_kind'] == 'producer_growth_through_prefix'
    with pytest.raises(PopulationError, match='candidate|right|near-miss|scalar'):
        synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
            demands=impossible.demands, obligations=impossible.obligations, correspondence=correspondence, min_result_rows=1)
    fallback = next(candidates)
    assert fallback.evidence['work']['axis_kind'] == 'matching_candidate_fanout'
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
        demands=fallback.demands, obligations=fallback.obligations, correspondence=correspondence, min_result_rows=1)
    assert populated.expected_rows


def projected_prefix_chain(boundary):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    discarded, middle, end = ir.Node('discarded', ('Discarded',)), ir.Node('v', ('Mid',)), ir.Node('w', ('End',))
    def matching(left, relation, right):
        return ir.Match(ir.Edge(left, ir.Relationship(type=relation), right),
                        ir.Comparison(ir.Property(right.variable, 'keep'), '=', ir.Literal(True)))
    return ir.Query(None, None, (ir.Property('u', 'uid'), ir.Property('v', 'uid'), ir.Property('w', 'uid')),
                    pipeline=(matching(root, 'R', discarded), boundary,
                              matching(root, 'A', middle), matching(middle, 'B', end)))


@pytest.mark.parametrize('boundary', [
    ir.With((ir.Variable('u'),), distinct=True),
    ir.With((ir.Variable('u'), ir.Alias(ir.Property('discarded', 'uid'), 'duid')),
            ir.Comparison(ir.Variable('duid'), '=', ir.Literal(2))),
])
def test_a_collapsing_or_filtering_with_cannot_certify_the_uncut_matching_product_as_work(boundary):
    source = projected_prefix_chain(boundary)
    site, = find_mutation_sites(source, 'P10', 'chain')
    target, correspondence = plan_mutation_with_correspondence(source, site)
    with pytest.raises(PopulationError, match='WITH'):
        next(derive_requirement_plans(source, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))


def test_plain_with_alias_projection_preserves_actual_prefix_multiplicity():
    source = projected_prefix_chain(ir.With((ir.Variable('u'), ir.Alias(ir.Property('discarded', 'uid'), 'duid'))))
    site, = find_mutation_sites(source, 'P10', 'chain')
    target, correspondence = plan_mutation_with_correspondence(source, site)
    plan = next(derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
        scale=7, correspondence=correspondence))
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
    prefix = ir.Query(None, None, (ir.Property('u', 'uid'), ir.Property('v', 'uid')), pipeline=source.pipeline[:3])
    actual_inputs = len(evaluate_query(prefix, populated.spec))
    assert actual_inputs == plan.evidence['work']['repetitions']
    assert actual_inputs * plan.evidence['work']['extra_candidates_per_input'] >= 7


def test_count_threshold_cannot_use_pre_distinct_match_population_as_distinct_rows():
    body = projected_prefix_chain(ir.With((ir.Variable('u'),), distinct=True))
    body = replace(body, returns=(ir.Property('u', 'uid'),), pipeline=body.pipeline[:2])
    source = ir.ConditionalQuery(ir.Comparison(ir.Count(body=body), '>', ir.Literal(1)), rows('yes'), rows('low'))
    candidates, _, _ = plans(source, guard_value=...)
    with pytest.raises(PopulationError, match='WITH DISTINCT|distinct-value'):
        next(candidates)


@pytest.mark.parametrize('filter_kind', ['uid', 'exists', 'count'])
def test_node_scan_input_filter_needs_a_uniform_constructed_domain_certificate(filter_kind):
    root = ir.Node('u', ('Root',))
    middle, end = ir.Node('v', ('Mid',)), ir.Node('w', ('End',))
    graph_read = ir.Edge(root, ir.Relationship(type='Guard'), ir.Node('g', ('Guard',)))
    predicate = {
        'uid': ir.Comparison(ir.Property('u', 'uid'), '=', ir.Literal(1)),
        'exists': ir.Exists(graph_read),
        'count': ir.Comparison(ir.Count(graph_read), '>', ir.Literal(0)),
    }[filter_kind]
    matches = tuple(ir.Match(ir.Edge(left, ir.Relationship(type=relation), right),
                            ir.Comparison(ir.Property(right.variable, 'keep'), '=', ir.Literal(True)))
                    for left, relation, right in ((root, 'A', middle), (middle, 'B', end)))
    source = ir.Query(root, predicate, tuple(ir.Property(name, 'uid') for name in ('u', 'v', 'w')), matches=matches)
    site, = find_mutation_sites(source, 'P10', 'chain')
    target, correspondence = plan_mutation_with_correspondence(source, site)
    with pytest.raises(PopulationError, match='MATCH input-domain filter'):
        next(derive_requirement_plans(source, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))


def test_with_scalar_alias_filter_is_certified_after_lexical_producer_expansion():
    boundary = ir.With((ir.Variable('u'), ir.Alias(ir.Property('discarded', 'keep'), 'retained')),
                       ir.Comparison(ir.Variable('retained'), '=', ir.Literal(True)))
    source = projected_prefix_chain(boundary)
    site, = find_mutation_sites(source, 'P10', 'chain')
    target, correspondence = plan_mutation_with_correspondence(source, site)
    plan = next(derive_requirement_plans(source, target, site,
        goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
        scale=7, correspondence=correspondence))
    populated = synthesize_relational_state(source, alternatives=(target,), scale=7, data_seed=0,
        demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
    prefix = ir.Query(None, None, (ir.Property('u', 'uid'), ir.Property('v', 'uid')), pipeline=source.pipeline[:3])
    assert len(evaluate_query(prefix, populated.spec)) == plan.evidence['work']['repetitions']
    assert any(step.get('requirement') == 'uniform_constructed_domain'
               and step['address'] == ['query', 'pipeline', 1, 'where'] for step in plan.evidence['inward_steps'])


def test_with_graph_read_alias_does_not_hide_a_nonuniform_predicate():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    graph_read = ir.Count(ir.Edge(root, ir.Relationship(type='Guard'), ir.Node('g', ('Guard',))))
    boundary = ir.With((ir.Variable('u'), ir.Alias(graph_read, 'guard_count')),
                       ir.Comparison(ir.Variable('guard_count'), '>', ir.Literal(0)))
    source = projected_prefix_chain(boundary)
    site, = find_mutation_sites(source, 'P10', 'chain')
    target, correspondence = plan_mutation_with_correspondence(source, site)
    with pytest.raises(PopulationError, match='WITH input-domain filter'):
        next(derive_requirement_plans(source, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))
