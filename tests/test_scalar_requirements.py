"""Typed scalar requirements and semantic masks drive graph work domains."""

import pytest

import ir
from dependencies import find_mutation_sites
from mutator import plan_mutation_with_correspondence
from population import BindingRelation, PopulationError, evaluate_query, synthesize_relational_state
from requirements import _Lowering, derive_requirement_plans


ANCHOR = ir.Node('u', ('ScalarRoot',), (('id', ir.Literal(1)),))
FORMS = {'P4': 'and', 'P5': 'or', 'P13': 'case_dual', 'S1': 'absorbing_or',
         'S2': 'absorbing_and', 'S4': 'case_guard', 'S7': 'coalesce'}


def region(name='cost', *, filtered=False):
    leaf = ir.Node('leaf_' + name, ('Leaf_' + name,))
    predicate = ir.Comparison(ir.Property(leaf.variable, 'keep'), '=', ir.Literal(True)) if filtered else None
    return ir.Query(ir.Edge(ANCHOR, ir.Relationship(type='R_' + name), leaf), predicate,
                    (ir.Alias(ir.Literal(1), 'c'),))


def source(expression):
    return ir.Query(ANCHOR, None, (ir.Property('u', 'uid'), ir.Alias(expression, 'value')))


def solve(expression, pattern, *, expected, replacement=None, query=None, seed=0):
    query = query or source(expression)
    site = find_mutation_sites(query, pattern, FORMS[pattern])[0]
    target, correspondence = plan_mutation_with_correspondence(query, site, replacement=replacement)
    goals = ({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': seed},
             {'kind': 'value', 'address': ('query', 'returns', 1, 'expression'), 'value': expected})
    failures = []
    for plan in derive_requirement_plans(query, target, site, goals=goals, scale=7, correspondence=correspondence):
        try:
            populated = synthesize_relational_state(query, alternatives=(target,), scale=7, data_seed=0,
                demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
        except PopulationError as error:
            failures.append(str(error))
            continue
        actual = populated.expected_rows[0]['value']
        assert type(actual) is type(expected) and actual == expected
        return plan, populated
    pytest.fail('No scalar requirement plan was feasible: ' + repr(failures[-3:]))


@pytest.mark.parametrize('expected', [0, False, '', 11, 'kept', 1.25])
def test_nonnull_coalesce_does_not_coerce_zero_false_or_empty_string(expected):
    count = ir.Count(body=region())
    expensive = (ir.Comparison(count, '>', ir.Literal(0)) if type(expected) is bool else
                 ir.Case(ir.Comparison(count, '>', ir.Literal(0)), ir.Literal('high'), ir.Literal('empty'))
                 if type(expected) is str else count)
    fallback = True if type(expected) is bool else 'low' if type(expected) is str else 9
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(expected), ir.Literal(fallback)))
    plan, populated = solve(expression, 'S7', expected=expected, replacement=expensive)
    assert len(evaluate_query(region(), populated.spec)) >= 7
    assert any(fact.relation == 'nonnull' for fact in plan.obligations)
    assert plan.evidence['work']['semantic_mask']['kind'] == 'nonnull_coalesce_prefix'


@pytest.mark.parametrize('condition', [False, None])
def test_case_dual_expands_only_a_semantically_unselected_branch(condition):
    expression = ir.Case(ir.Literal(condition), ir.Count(body=region()), ir.Literal(11))
    plan, populated = solve(expression, 'P13', expected=11)
    assert len(evaluate_query(region(), populated.spec)) >= 7
    assert plan.evidence['work']['semantic_mask']['kind'] == 'unselected_value_branch'


@pytest.mark.parametrize('pattern,operator,expected', [('P4', 'AND', False), ('P5', 'OR', True)])
def test_boolean_exchange_requires_a_decisive_context_and_failed_exists_candidates(pattern, operator, expected):
    expensive = ir.Exists(body=region(filtered=True))
    expression = ir.Boolean(operator, (ir.Comparison(ir.Property('u', 'guard'), '=', ir.Literal(True)), expensive))
    plan, populated = solve(expression, pattern, expected=expected)
    assert evaluate_query(region(filtered=True), populated.spec) == []
    traversable = [edge for edge in populated.spec.iter_edges() if edge['type'] == 'R_cost']
    assert len(traversable) >= 7
    assert plan.evidence['work']['axis_kind'] == 'masked_scalar_existence_candidates'
    assert any(demand.exact_matches == 0 and demand.candidate_matches == 7 and demand.candidate_kind == 'right'
               for demand in plan.demands)


@pytest.mark.parametrize('expected', [0, False, '', None])
def test_case_value_requirement_propagates_to_the_selected_typed_literal(expected):
    fallback = 3 if type(expected) in (int, type(None)) else True if type(expected) is bool else 'fallback'
    expression = ir.Case(ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True)),
                         ir.Literal(expected), ir.Literal(fallback))
    count = ir.Count(body=region())
    high = count if type(fallback) is int else ir.Comparison(count, '>', ir.Literal(0)) if type(fallback) is bool else ir.Case(
        ir.Comparison(count, '>', ir.Literal(0)), ir.Literal('high'), ir.Literal('empty'))
    _, populated = solve(expression, 'S4', expected=expected, replacement=high)
    assert len(evaluate_query(region(), populated.spec)) >= 7


def test_comparison_pushes_a_string_value_through_case_and_with_alias():
    produced = ir.Case(ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True)),
                       ir.Property('u', 'text'), ir.Literal('other'))
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Variable('named'), ir.Literal('fallback')))
    query = ir.Query(None, None, (ir.Property('u', 'uid'), ir.Alias(expression, 'value')), pipeline=(
        ir.Match(ANCHOR), ir.With((ir.Variable('u'), ir.Alias(produced, 'named')),
                                 ir.Comparison(ir.Variable('named'), '=', ir.Literal('chosen')))))
    high = ir.Case(ir.Comparison(ir.Count(body=region()), '>', ir.Literal(0)),
                   ir.Literal('high'), ir.Literal('empty'))
    plan, _ = solve(expression, 'S7', query=query, expected='chosen', replacement=high)
    assert any(fact.value == 'chosen' and fact.address == ('query', 'pipeline', 1, 'projections', 1, 'expression', 'then')
               for fact in plan.obligations)


def test_exact_scalar_and_nullability_requirements_keep_boolean_integer_domains_separate():
    query = source(ir.Property('u', 'value'))
    problem = _Lowering(query, query)
    address = ('returns', 1, 'expression')
    problem.require_value(('query',), address, 0)
    with pytest.raises(PopulationError, match='typed value'):
        problem.require_value(('query',), address, False)
    with pytest.raises(PopulationError, match='typed value|nonnull'):
        problem.null(('query',), address)


@pytest.mark.parametrize('expected', [float('nan'), float('inf'), 2**63])
def test_value_requirements_use_the_same_literal_domain_as_the_query_ir(expected):
    query = source(ir.Property('u', 'value'))
    with pytest.raises(PopulationError, match='finite|64-bit'):
        _Lowering(query, query).require_value(('query',), ('returns', 1, 'expression'), expected)


def test_scalar_leaf_requirements_share_origins_across_transported_queries():
    query = source(ir.Property('u', 'value'))
    links = (BindingRelation(('query', 'pattern'), ('alternatives', 0, 'pattern')),)
    problem = _Lowering(query, query, links)
    address = ('returns', 1, 'expression')
    problem.require_value(('query',), address, None)
    with pytest.raises(PopulationError, match='transported scalar'):
        problem.nonnull(('alternatives', 0), address)
    problem = _Lowering(query, query, links)
    problem.require_value(('query',), address, 0)
    with pytest.raises(PopulationError, match='transported scalar'):
        problem.require_value(('alternatives', 0), address, False)


@pytest.mark.parametrize('pattern,operator,expected', [('S1', 'OR', True), ('S2', 'AND', False)])
def test_absorbing_boolean_scalar_replacement_gets_its_work_from_count(pattern, operator, expected):
    expression = ir.Boolean(operator, (ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True)),
                                       ir.Literal(not expected)))
    high = ir.Comparison(ir.Count(body=region()), '>', ir.Literal(0))
    plan, populated = solve(expression, pattern, expected=expected, replacement=high)
    assert len(evaluate_query(region(), populated.spec)) >= 7
    assert plan.evidence['work']['semantic_mask']['kind'] == 'absorbing_boolean_sibling'


def test_unprotected_scalar_read_has_no_automatic_work_certificate():
    expression = ir.Case(ir.Comparison(ir.Count(body=region()), '>', ir.Literal(0)),
                         ir.Literal(1), ir.Literal(2))
    query = source(expression)
    site, = find_mutation_sites(query, 'P13', 'case_dual')
    target, correspondence = plan_mutation_with_correspondence(query, site)
    with pytest.raises(PopulationError, match='semantic mask'):
        next(derive_requirement_plans(query, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))


def test_later_exists_failure_axis_has_a_nonempty_actual_input_prefix():
    middle = ir.Node('v', ('Mid',))
    end = ir.Node('w', ('End',))
    first = ir.Edge(ANCHOR, ir.Relationship(type='First'), middle)
    second = ir.Edge(middle, ir.Relationship(type='Second'), end)
    body = ir.Query(first, None, (ir.Alias(ir.Literal(1), 'c'),), matches=(ir.Match(second),))
    expression = ir.Boolean('OR', (ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True)),
                                   ir.Exists(body=body)))
    query = source(expression)
    site, = find_mutation_sites(query, 'P5', 'or')
    target, correspondence = plan_mutation_with_correspondence(query, site)
    goals = ({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'},
             {'kind': 'value', 'address': ('query', 'returns', 1, 'expression'), 'value': True})
    for plan in derive_requirement_plans(query, target, site, goals=goals, scale=7, correspondence=correspondence):
        if plan.evidence['work']['address'][-3:] != ['matches', 0, 'pattern']:
            continue
        populated = synthesize_relational_state(query, alternatives=(target,), scale=7, data_seed=0,
            demands=plan.demands, obligations=plan.obligations, correspondence=correspondence, min_result_rows=1)
        prefix = ir.Query(first, None, (ir.Property('v', 'uid'),))
        reached = {row['v.uid'] for row in evaluate_query(prefix, populated.spec)}
        assert reached
        assert len([edge for edge in populated.spec.iter_edges() if edge['type'] == 'Second' and edge['source'] in reached]) >= 7
        assert plan.evidence['work']['input_rows_lower_bound'] >= 1
        assert evaluate_query(body, populated.spec) == []
        break
    else:
        pytest.fail('The later edge must have a feasible failed-candidate axis')


def test_masked_count_cannot_describe_identity_filtered_node_copies_as_valid_matches():
    node = ir.Node('n', ('IdentityPopulation',))
    body = ir.Query(node, ir.Comparison(ir.Property('n', 'uid'), '=', ir.Literal(1)), (ir.Alias(ir.Literal(1), 'c'),))
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    query = source(expression)
    site, = find_mutation_sites(query, 'S7', 'coalesce')
    target, correspondence = plan_mutation_with_correspondence(query, site, replacement=ir.Count(body=body))
    with pytest.raises(PopulationError, match='uniform matching-domain filter'):
        next(derive_requirement_plans(query, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))


def test_a_replacement_cannot_claim_work_from_its_own_unreachable_branch():
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    query = source(expression)
    site, = find_mutation_sites(query, 'S7', 'coalesce')
    high = ir.Case(ir.Literal(False), ir.Count(body=region()), ir.Literal(0))
    target, correspondence = plan_mutation_with_correspondence(query, site, replacement=high)
    with pytest.raises(PopulationError, match='branch|candidate|semantic|truth'):
        next(derive_requirement_plans(query, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'}),
            scale=7, correspondence=correspondence))


def nested_read(position, *, passed=True, optional=False):
    middle, end = ir.Node('v', ('Imported',)), ir.Node('w', ('NestedEnd',))
    inner = ir.Count(body=ir.Query(ir.Edge(middle, ir.Relationship(type='Nested'), end), None,
                                   (ir.Alias(ir.Literal(1), 'c'),)))
    first = ir.Match(ir.Edge(ANCHOR, ir.Relationship(type='Prefix'), middle), ir.Literal(passed), optional)
    if position == 'projection':
        return ir.Query(None, None, (ir.Alias(inner, 'nested'),), pipeline=(first,))
    extra = ir.Node('x', ('Extra',))
    second = ir.Match(ir.Edge(middle, ir.Relationship(type='BeforeFilter'), extra),
                      ir.Comparison(inner, '>', ir.Literal(0)))
    return ir.Query(None, None, (ir.Alias(ir.Literal(1), 'c'),), pipeline=(first, second))


@pytest.mark.parametrize('position,optional', [('projection', False), ('where', False), ('projection', True)])
def test_nested_count_work_requires_a_live_outer_query_and_nonnull_imports(position, optional):
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    query = source(expression)
    site, = find_mutation_sites(query, 'S7', 'coalesce')
    high = ir.Count(body=nested_read(position, passed=False, optional=optional))
    target, correspondence = plan_mutation_with_correspondence(query, site, replacement=high)
    with pytest.raises(PopulationError, match='Optional|literal|input|truth|semantic'):
        next(derive_requirement_plans(query, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto', 'seed': 1}),
            scale=7, correspondence=correspondence))


@pytest.mark.parametrize('position', ['projection', 'where'])
def test_nested_count_work_is_reached_from_real_outer_query_bindings(position):
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    outer = nested_read(position)
    plan, populated = solve(expression, 'S7', expected=0, replacement=ir.Count(body=outer), seed=1)
    outer_rows = evaluate_query(outer, populated.spec)
    assert outer_rows
    reached = {edge['target'] for edge in populated.spec.iter_edges() if edge['type'] == 'Prefix'}
    assert len([edge for edge in populated.spec.iter_edges() if edge['type'] == 'Nested' and edge['source'] in reached]) >= 7
    assert any(step.get('requirement') == 'scalar_observer_input' for step in plan.evidence['inward_steps'])


def test_the_mark_itself_cannot_hide_in_a_dead_outer_projection_domain():
    expression = ir.Call('coalesce', (ir.Literal(None), ir.Literal(0), ir.Literal(9)))
    dead = ir.Query(ANCHOR, ir.Literal(False), (ir.Alias(expression, 'inner'),))
    query = source(ir.Count(body=dead))
    site, = find_mutation_sites(query, 'S7', 'coalesce')
    target, correspondence = plan_mutation_with_correspondence(query, site, replacement=ir.Count(body=region()))
    with pytest.raises(PopulationError, match='literal|input|truth|semantic'):
        next(derive_requirement_plans(query, target, site,
            goals=({'kind': 'output', 'min_rows': 1}, {'kind': 'work', 'mode': 'auto'},
                   {'kind': 'value', 'address': ('query', 'returns', 1, 'expression'), 'value': 0}),
            scale=7, correspondence=correspondence))
