"""Three-valued construction facts use exact values and Boolean observations."""

import pytest

import ir
from dependencies import MutationSite, find_mutation_sites, premise_obligations, verify_rule_premises
from population import MatchDemand, PopulationError, ValueDemand, evaluate_query, synthesize_relational_state
from query import render_query


def fact_site(address, relation):
    return MutationSite(tuple(address), 'root:query', 'S3', 'conditional', {
        'premises': [{'address': list(address), 'relation': relation}],
    })


def checked_fact(source, site, spec, anchor):
    fact, = premise_obligations(source, site)
    checked = ir.Query(anchor, None, (ir.Alias(ir.Property(anchor.variable, 'uid'), 'anchor_uid'),
                                      ir.Alias(fact['expression'], 'premise')))
    check = {'query': render_query(checked), 'expected_rows': evaluate_query(checked, spec)}
    return fact, {'premise_checks': [check], 'required_state_checks': [check],
                  'validation_graph_spec': spec.to_dict()}


@pytest.mark.parametrize('value,relation', [(True, 'true'), (False, 'false'), (None, 'null')])
def test_truth_fact_is_constructed_and_observed_as_a_boolean(value, relation):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    source = ir.Query(root, None, (ir.Alias(ir.Property('u', 'flag'), 'value'),))
    address = ('returns', 0, 'expression')
    solved = synthesize_relational_state(source, scale=5, data_seed=0,
        obligations=(ValueDemand(('query', *address), value),), min_result_rows=1)
    assert solved.expected_rows == [{'value': value}]
    fact, bindings = checked_fact(source, fact_site(address, relation), solved.spec, root)
    if value is None:
        assert fact['expression'].operator == 'IS NULL'
    assert bindings['premise_checks'][0]['expected_rows'] == [{'anchor_uid': 1, 'premise': True}]
    assert verify_rule_premises(source, fact_site(address, relation), bindings)['proof_model_checked']


def test_null_is_not_a_false_fact_or_a_null_equality_fact():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    source = ir.Query(root, None, (ir.Alias(ir.Property('u', 'flag'), 'value'),))
    address = ('returns', 0, 'expression')
    solved = synthesize_relational_state(source, scale=5, data_seed=0,
        obligations=(ValueDemand(('query', *address), None),))
    _, bindings = checked_fact(source, fact_site(address, 'false'), solved.spec, root)
    assert bindings['premise_checks'][0]['expected_rows'] == [{'anchor_uid': 1, 'premise': None}]
    with pytest.raises(ir.QueryError, match='exact premise'):
        verify_rule_premises(source, fact_site(address, 'false'), bindings)
    null_equality = ir.Query(root, None, (ir.Alias(ir.Comparison(ir.Property('u', 'flag'), '=', ir.Literal(None)), 'value'),))
    assert evaluate_query(null_equality, solved.spec) == [{'value': None}]


@pytest.mark.parametrize('value', [False, None])
def test_zero_is_not_a_false_or_null_truth_value(value):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)), ('flag', ir.Literal(0))))
    source = ir.Query(root, None, (ir.Alias(ir.Property('u', 'flag'), 'value'),))
    with pytest.raises(PopulationError, match='obligations'):
        synthesize_relational_state(source, scale=4, data_seed=0,
            obligations=(ValueDemand(('query', 'returns', 0, 'expression'), value),))
    solved = synthesize_relational_state(source, scale=4, data_seed=0,
        obligations=(ValueDemand(('query', 'returns', 0, 'expression'), relation='nonnull'),))
    assert solved.expected_rows == [{'value': 0}]


def test_unknown_fact_relation_does_not_silently_mean_false():
    source = ir.Query(ir.Node('u', ('Root',)), None, (ir.Literal(False),))
    with pytest.raises(ir.QueryError, match='relation'):
        premise_obligations(source, fact_site(('returns', 0), 'truthy'))


@pytest.mark.parametrize('value', [False, None])
def test_rejected_matching_candidates_can_preserve_false_or_null_filter_facts(value):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    rows = ir.Query(ir.Edge(root, ir.Relationship(type='R'), ir.Node('v', ('Item',))),
                    ir.Comparison(ir.Property('v', 'flag'), '=', ir.Literal(True)), (ir.Literal(1),))
    source = ir.Query(root, None, (ir.Alias(ir.Count(body=rows), 'count'),))
    atom = ('query', 'returns', 0, 'expression', 'body', 'pattern')
    condition = ('query', 'returns', 0, 'expression', 'body', 'where')
    solved = synthesize_relational_state(source, scale=7, data_seed=0,
        demands=(MatchDemand(atom, 'rejected', exact_matches=0, candidate_matches=7),),
        obligations=(ValueDemand(condition, value),), min_result_rows=1)
    assert solved.expected_rows == [{'count': 0}]
    states = {node['properties'].get('flag') for node in solved.spec.iter_nodes() if 'Item' in node['labels']}
    assert states == {value}


@pytest.mark.parametrize('value', [False, None])
def test_positive_matching_witness_cannot_have_a_false_or_null_local_filter(value):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    source = ir.Query(ir.Edge(root, ir.Relationship(type='R'), ir.Node('v', ('Item',))),
                      ir.Comparison(ir.Property('v', 'flag'), '=', ir.Literal(True)), (ir.Literal(1),))
    with pytest.raises(PopulationError, match='obligations'):
        synthesize_relational_state(source, scale=4, data_seed=0,
            demands=(MatchDemand(('query', 'pattern'), 'witness', exact_matches=1),),
            obligations=(ValueDemand(('query', 'where'), value),))


def test_null_fact_keeps_alias_origin_after_same_name_node_rebinding():
    first = ir.Node('u', ('First',), (('id', ir.Literal(1)),))
    second = ir.Node('u', ('Second',), (('id', ir.Literal(2)),))
    source = ir.Query(None, None, (ir.Alias(ir.Variable('saved'), 'value'),), pipeline=(
        ir.Match(first), ir.With((ir.Alias(ir.Property('u', 'flag'), 'saved'),)), ir.Match(second)))
    address = ('returns', 0, 'expression')
    solved = synthesize_relational_state(source, scale=4, data_seed=0,
        obligations=(ValueDemand(('query', *address), None),), min_result_rows=1)
    _, wrong = checked_fact(source, fact_site(address, 'null'), solved.spec, second)
    assert wrong['premise_checks'][0]['expected_rows'][0]['premise'] is True
    with pytest.raises(ir.QueryError, match='imported anchor domain'):
        verify_rule_premises(source, fact_site(address, 'null'), wrong)


@pytest.mark.parametrize('value', [False, None])
def test_false_and_null_conditions_select_else_without_truthiness_conflation(value):
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    high = ir.Query(ir.Edge(root, ir.Relationship(type='H'), ir.Node('item', ('Work',))), None, (ir.Literal(1),))
    condition = ir.Comparison(ir.Property('u', 'flag'), '=', ir.Literal(True))
    source = ir.Query(root, None, (ir.Alias(ir.Case(condition, ir.Count(body=high), ir.Literal(7)), 'value'),))
    address = ('query', 'returns', 0, 'expression')
    solved = synthesize_relational_state(source, scale=5001, data_seed=0,
        demands=(MatchDemand((*address, 'then', 'body', 'pattern'), 'masked', scale_matches=True),),
        obligations=(ValueDemand((*address, 'condition'), value),), min_result_rows=1)
    assert solved.expected_rows == [{'value': 7}]
    assert any(observation['mask'] and observation['mask'].get('condition') is value
               for family in solved.evidence['growth_certificates'] for observation in family['observations'])


def test_premise_closes_free_aliases_and_preserves_subquery_local_with_bindings():
    root = ir.Node('u', ('Root',), (('id', ir.Literal(1)),))
    rows = ir.Query(None, None, (ir.Literal(1),), pipeline=(
        ir.Match(ir.Edge(root, ir.Relationship(type='R'), ir.Node('item', ('Item',)))),
        ir.With((ir.Alias(ir.Property('item', 'keep'), 'local_keep'),),
                ir.Comparison(ir.Variable('local_keep'), '=', ir.Literal(True)))))
    condition = ir.Boolean('AND', (ir.Variable('gate'), ir.Comparison(ir.Count(body=rows), '>', ir.Literal(0))))
    branch = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'c'),))
    when = ir.ConditionalQuery(condition, branch, branch)
    source = ir.Query(None, None, (ir.Alias(ir.Count(body=when), 'count'),), pipeline=(
        ir.Match(root), ir.With((ir.Variable('u'), ir.Alias(
            ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True)), 'gate')))))
    site, = find_mutation_sites(source, 'S3', 'conditional')
    predicate = ('query', 'returns', 0, 'expression', 'body', 'condition')
    solved = synthesize_relational_state(source, scale=4, data_seed=0,
        demands=(MatchDemand((*predicate, 'terms', 1, 'left', 'body', 'pipeline', 0, 'pattern'),
                             'guard', exact_matches=2),),
        obligations=(ValueDemand(predicate, True),), min_result_rows=1)
    fact, bindings = checked_fact(source, site, solved.spec, root)
    ir.validate_query(ir.Query(root, None, (ir.Alias(fact['expression'], 'premise'),)))
    closed = fact['expression'].left
    assert closed.terms[0] == ir.Comparison(ir.Property('u', 'enabled'), '=', ir.Literal(True))
    assert closed.terms[1].left.body.pipeline[-1].where.left == ir.Variable('local_keep')
    assert verify_rule_premises(source, site, bindings)['proof_model_checked']
