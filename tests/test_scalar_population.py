"""Scalar goals constrain actual lexical property domains and checked facts."""

import pytest

import ir
from dependencies import (MutationSite, find_mutation_sites, premise_obligations,
                          propagate_equivalence, verify_rule_premises)
from population import (MatchDemand, PopulationError, ValueDemand, evaluate_query,
                        synthesize_relational_state)
from query import render_query


def anchored(expression, *, where=None):
    return ir.Query(ir.Node('u', ('Root',), (('id', ir.Literal(1)),)), where,
                    (ir.Alias(expression, 'value'),))


def solve(source, value=None, *, relation='equal', address=('returns', 0, 'expression'), **options):
    return synthesize_relational_state(source, scale=7, data_seed=0, min_result_rows=1,
        obligations=(ValueDemand(('query', *address), value, relation),), **options)


@pytest.mark.parametrize('value', [0, 17, -5, 2.5, 'a "quoted" value', True, False, None])
def test_exact_scalar_goals_seed_the_actual_property_and_runtime_fact(value):
    source = anchored(ir.Property('u', 'score'))
    populated = solve(source, value)
    assert populated.expected_rows == [{'value': value}]
    assert type(populated.expected_rows[0]['value']) is type(value)
    assert list(populated.spec.iter_nodes())[0]['properties']['score'] == value
    site = MutationSite(('returns', 0, 'expression'), 'root:query', 'S3', 'conditional', {
        'premises': [{'address': ['returns', 0, 'expression'], 'relation': 'equal', 'value': value}]})
    fact, = premise_obligations(source, site)
    assert fact['expression'].operator == ('IS NULL' if value is None else '=')
    checked = ir.Query(source.pattern, None, (ir.Alias(ir.Property('u', 'uid'), 'anchor_uid'),
                                            ir.Alias(fact['expression'], 'premise')))
    check = {'query': render_query(checked), 'expected_rows': [{'anchor_uid': 1, 'premise': True}]}
    assert evaluate_query(checked, populated.spec) == check['expected_rows']
    assert verify_rule_premises(source, site, {'premise_checks': [check], 'required_state_checks': [check],
        'validation_graph_spec': populated.evidence['validation_graph_spec']})['proof_model_checked']


def test_exact_scalar_goal_follows_an_alias_before_same_name_node_rebinding():
    source = ir.Query(None, None, (ir.Alias(ir.Variable('saved'), 'value'),), pipeline=(
        ir.Match(ir.Node('u', ('First',), (('id', ir.Literal(1)),))),
        ir.With((ir.Alias(ir.Property('u', 'score'), 'saved'),)),
        ir.Match(ir.Node('u', ('Second',), (('id', ir.Literal(2)),)))))
    populated = solve(source, 'kept')
    first = next(node for node in populated.spec.iter_nodes() if 'First' in node['labels'])
    second = next(node for node in populated.spec.iter_nodes() if 'Second' in node['labels'])
    assert first['properties']['score'] == 'kept'
    assert 'score' not in second['properties']
    assert populated.expected_rows == [{'value': 'kept'}]


def test_scalar_goal_seeds_case_value_branches_without_overwriting_the_guard_type():
    expression = ir.Case(ir.Comparison(ir.Property('u', 'guard'), '=', ir.Literal(True)),
                         ir.Property('u', 'score'), ir.Literal(-1))
    source = anchored(expression)
    populated = solve(source, 42)
    props = list(populated.spec.iter_nodes())[0]['properties']
    assert props['guard'] is True and props['score'] == 42
    assert populated.expected_rows == [{'value': 42}]


@pytest.mark.parametrize('operator,value', [('<>', 1), ('=', 0)])
def test_comparison_literals_reach_the_actual_alias_producer_domain(operator, value):
    source = ir.Query(None, None, (ir.Alias(ir.Variable('score'), 'value'),), pipeline=(
        ir.Match(ir.Node('u', ('Root',), (('id', ir.Literal(1)),))),
        ir.With((ir.Variable('u'), ir.Alias(ir.Property('u', 'score'), 'score')),
                ir.Comparison(ir.Variable('score'), operator, ir.Literal(0)))))
    populated = solve(source, value)
    assert populated.expected_rows == [{'value': value}]


@pytest.mark.parametrize('value', [False, 0, '', 2.5, 'typed'])
def test_nonnull_is_a_domain_requirement_and_does_not_force_true(value):
    source = anchored(ir.Property('u', 'score'),
                      where=ir.Comparison(ir.Property('u', 'score'), '=', ir.Literal(value)))
    populated = solve(source, relation='nonnull')
    assert populated.expected_rows == [{'value': value}]
    assert type(populated.expected_rows[0]['value']) is type(value)


def test_scalar_goal_and_fixed_map_do_not_coerce_integer_zero_to_false():
    source = ir.Query(ir.Node('u', ('Root',), (('id', ir.Literal(1)), ('score', ir.Literal(0)))),
                      None, (ir.Alias(ir.Property('u', 'score'), 'value'),))
    with pytest.raises(PopulationError, match='obligations'):
        solve(source, False)


@pytest.mark.parametrize('value', [[], {}, float('inf'), 2**63])
def test_scalar_goal_rejects_unsupported_literal_domains(value):
    with pytest.raises(PopulationError, match='literal|scalar|64-bit|finite'):
        solve(anchored(ir.Property('u', 'score')), value)


def test_coalesce_sites_describe_the_actual_null_prefix_and_decisive_position():
    source = anchored(ir.Call('coalesce', (ir.Property('u', 'a'), ir.Property('u', 'b'),
                                          ir.Property('u', 'c'), ir.Literal(7))))
    sites = find_mutation_sites(source, 'S7', 'coalesce')
    assert [site.address[-1] for site in sites] == [1, 2, 3]
    assert sites[-1].guards['premises'] == [
        {'address': ['returns', 0, 'expression', 'args', 0], 'relation': 'null'},
        {'address': ['returns', 0, 'expression', 'args', 1], 'relation': 'null'},
        {'address': ['returns', 0, 'expression', 'args', 2], 'relation': 'nonnull'},
    ]
    original = anchored(ir.Call('coalesce', (ir.Literal(None), ir.Literal(7), ir.Literal(1))))
    site, = find_mutation_sites(original, 'S7')
    assert site.address == ('returns', 0, 'expression', 'args', 2)
    assert not find_mutation_sites(anchored(ir.Call('coalesce', (ir.Literal(None), ir.Literal(1)))), 'S7')


def test_coalesce_nullable_prefix_and_nonnull_alias_use_their_real_producers():
    source = ir.Query(None, None, (ir.Alias(ir.Call('coalesce', (
        ir.Property('u', 'prefix'), ir.Variable('saved'), ir.Literal(1))), 'value'),), pipeline=(
        ir.Match(ir.Node('u', ('Root',), (('id', ir.Literal(1)),))),
        ir.With((ir.Variable('u'), ir.Alias(ir.Property('u', 'payload'), 'saved')))))
    site = next(site for site in find_mutation_sites(source, 'S7') if site.address[-1] == 2)
    populated = synthesize_relational_state(source, scale=7, data_seed=0, min_result_rows=1, obligations=(
        ValueDemand(('query', 'returns', 0, 'expression', 'args', 0), None),
        ValueDemand(('query', 'returns', 0, 'expression', 'args', 1), relation='nonnull'),
        ValueDemand(('query', 'returns', 0, 'expression'), 'payload'),
    ))
    assert populated.expected_rows == [{'value': 'payload'}]
    facts = premise_obligations(source, site)
    assert facts[1]['expression'].left == ir.Property('u', 'payload')
    assert list(populated.spec.iter_nodes())[0]['properties']['prefix'] is None


@pytest.mark.parametrize('pattern,operator', [('P4', 'AND'), ('P5', 'OR')])
def test_commutativity_evidence_names_the_actual_boolean_operator(pattern, operator):
    source = anchored(ir.Boolean(operator, (ir.Literal(True), ir.Literal(False))))
    site, = find_mutation_sites(source, pattern)
    proof = propagate_equivalence(source, site)
    assert proof[0]['premise'] == f'pure_three_valued_{operator}_commutativity'


def counted(kind, label):
    return ir.Count(body=ir.Query(ir.Edge(ir.Node('u', ('Root',), (('id', ir.Literal(1)),)),
                                           ir.Relationship(type=kind), ir.Node('item', (label,))),
                                  None, (ir.Alias(ir.Literal(1), 'c'),)))


@pytest.mark.parametrize('value', [0, False, '', 2.5, 'stable'])
def test_nonnull_scalar_prefix_masks_large_graph_work_without_truthiness(value):
    source = anchored(ir.Call('coalesce', (ir.Property('u', 'prefix'), counted('H', 'High'))))
    populated = synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'returns', 0, 'expression', 'args', 1, 'body', 'pattern'),
                             'work', scale_matches=True),),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression', 'args', 0), value),))
    assert populated.expected_rows == [{'value': value}]
    assert type(populated.expected_rows[0]['value']) is type(value)
    assert populated.spec.counts()['nodes'] > 5000
    assert any(read['mask'] and read['mask']['kind'] == 'nonnull_coalesce_prefix'
               for fact in populated.evidence['growth_certificates'] for read in fact['observations'])


def test_null_coalesce_prefix_cannot_hide_a_count_that_changes_after_the_proof_graph():
    source = anchored(ir.Call('coalesce', (ir.Property('u', 'prefix'), counted('H', 'High'))))
    with pytest.raises(PopulationError, match='visible.*graph read'):
        synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
            demands=(MatchDemand(('query', 'returns', 0, 'expression', 'args', 1, 'body', 'pattern'),
                                 'work', scale_matches=True),),
            obligations=(ValueDemand(('query', 'returns', 0, 'expression', 'args', 0), None),))


def test_graph_count_zero_is_a_nonnull_mask_only_after_its_own_reads_close():
    source = anchored(ir.Call('coalesce', (counted('G', 'Guard'), counted('H', 'High'))))
    populated = synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'returns', 0, 'expression', 'args', 0, 'body', 'pattern'),
                             'guard', exact_matches=0),
                 MatchDemand(('query', 'returns', 0, 'expression', 'args', 1, 'body', 'pattern'),
                             'work', scale_matches=True)),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression'), 0),))
    assert populated.expected_rows == [{'value': 0}]
    assert type(populated.expected_rows[0]['value']) is int
    assert populated.evidence['growth_certificates']


def test_a_masked_query_does_not_prove_an_independent_count_fact_at_the_small_scale():
    source = anchored(ir.Call('coalesce', (ir.Literal(0), counted('H', 'High'))))
    count_address = ('query', 'returns', 0, 'expression', 'args', 1)
    with pytest.raises(PopulationError, match='scalar.*growth|scalar.*stable'):
        synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
            demands=(MatchDemand((*count_address, 'body', 'pattern'), 'work', scale_matches=True),),
            obligations=(ValueDemand(count_address, 3),))


def test_a_nonnull_count_fact_follows_count_semantics_even_when_its_exact_value_grows():
    source = anchored(ir.Call('coalesce', (ir.Literal(0), counted('H', 'High'))))
    count_address = ('query', 'returns', 0, 'expression', 'args', 1)
    populated = synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand((*count_address, 'body', 'pattern'), 'work', scale_matches=True),),
        obligations=(ValueDemand(count_address, relation='nonnull'),))
    assert populated.expected_rows == [{'value': 0}]


@pytest.mark.parametrize('operator,decisive', [('AND', False), ('OR', True)])
def test_exact_boolean_facts_can_ignore_unknown_graph_values_after_a_decisive_operand(operator, decisive):
    high = counted('H', 'High')
    expression = ir.Boolean(operator, (ir.Literal(decisive), ir.Exists(body=high.body)))
    source = anchored(expression)
    populated = synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'returns', 0, 'expression', 'terms', 1, 'body', 'pattern'),
                             'work', scale_matches=True),),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression'), decisive),))
    assert populated.expected_rows == [{'value': decisive}]
    assert populated.evidence['scalar_value_certificates'][0]['proof'] == 'constant_scalar_under_growth'


def test_known_null_comparison_preserves_a_null_fact_when_the_other_operand_grows():
    source = anchored(ir.Comparison(counted('H', 'High'), '=', ir.Literal(None)))
    populated = synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
        demands=(MatchDemand(('query', 'returns', 0, 'expression', 'left', 'body', 'pattern'),
                             'work', scale_matches=True),),
        obligations=(ValueDemand(('query', 'returns', 0, 'expression'), None),))
    assert populated.expected_rows == [{'value': None}]
    assert populated.evidence['scalar_value_certificates']


def test_unknown_case_condition_cannot_be_treated_as_else_for_an_independent_fact():
    expression = ir.Case(ir.Comparison(counted('H', 'High'), '>', ir.Literal(5)), ir.Literal(7), ir.Literal(0))
    source = anchored(ir.Call('coalesce', (ir.Literal(0), expression)))
    case_address = ('query', 'returns', 0, 'expression', 'args', 1)
    with pytest.raises(PopulationError, match='scalar.*stable'):
        synthesize_relational_state(source, scale=5001, data_seed=0, min_result_rows=1,
            demands=(MatchDemand((*case_address, 'condition', 'left', 'body', 'pattern'),
                                 'work', scale_matches=True),),
            obligations=(ValueDemand(case_address, 0),))
