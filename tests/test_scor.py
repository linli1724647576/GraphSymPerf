"""SCor reference-replacement contracts, distinct from P1--P16 symmetries.

SCor Table 1 and sections 3.1/3.3 in 3802061.pdf define the ten source
patterns. Its local bytecode replaces large0 by empty1 indiscriminately;
our LIMIT-n and first-match references must instead retain enough matches.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from generator import instantiate, load_seeds
from mutator import find_patterns, mutate
from query import (Alias, Boolean, Call, Case, Comparison, ConditionalQuery,
                   Count, Exists, Literal, Negation, Node, Property, QueryError, parse_query, render_query)


PAPER_PATTERNS = dict(zip(
    (f'S{i}' for i in range(1, 11)),
    ('1.1', '1.2', '2.1', '2.2', '3.1', '3.2', '4.1', '4.2', '5.1', '5.2'),
))
ROOT = 'MATCH (u:ScorRoot {id: 1})'
EDGE = '(u:ScorRoot {id: 1})-[:GSP_SC_HIGH]->(v:ScorTarget)'
HIGH = f'COUNT {{ MATCH {EDGE} WHERE v.mark = true }}'
EXISTS = 'EXISTS { MATCH (u)-[:GSP_SC_HIGH]->(v:ScorTarget) WHERE v.key = u.key }'
SOURCES = {
    'S1': f'{ROOT} RETURN true OR ({HIGH} > 0) AS value',
    'S2': f'{ROOT} RETURN false AND ({HIGH} > 0) AS value',
    'S3': f'CYPHER 25 WHEN true THEN {{ RETURN 7 AS value }} ELSE {{ RETURN {HIGH} AS value }}',
    'S4': f'{ROOT} RETURN CASE WHEN true THEN 7 ELSE {HIGH} END AS value',
    'S5': f'MATCH {EDGE} RETURN 1 AS value LIMIT 0',
    'S6': f'MATCH {EDGE} RETURN 1 AS value LIMIT 3',
    'S7': f'{ROOT} RETURN coalesce(null, 0, {HIGH}) AS value',
    'S8': f'{ROOT} WHERE {EXISTS} RETURN u.uid',
    'S9': f'MATCH (e:ScorRoot) WHERE false MATCH {EDGE} RETURN 1 AS value',
    'S10': f'{ROOT} WHERE false AND EXISTS {{ MATCH (u)-[:GSP_SC_HIGH]->(v:ScorTarget) WHERE (v.key = u.key) OR (v.key IS NULL AND u.key IS NULL) }} RETURN DISTINCT u.key AS key',
}


def bindings(pattern, **updates):
    facts = {'pattern': pattern, 'paper_pattern': PAPER_PATTERNS[pattern],
             'high_type': 'GSP_SC_HIGH', 'reference_type': 'GSP_SC_REF',
             'high_matches': 40, 'reference_matches': 3,
             'assumptions': []}
    if pattern == 'S8':
        facts['all_candidates_match'] = True
    facts.update(updates)
    return {'scor': facts}


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


@pytest.mark.parametrize('pattern', PAPER_PATTERNS)
def test_ten_patterns_replace_only_the_high_cost_reference(pattern):
    original = parse_query(SOURCES[pattern])
    contract = bindings(pattern, limit=3) if pattern == 'S6' else bindings(pattern)
    assert pattern in find_patterns(original, bindings=contract)
    changed = mutate(original, pattern, bindings=contract)
    expected = parse_query(SOURCES[pattern].replace(':GSP_SC_HIGH', ':GSP_SC_REF'))
    assert changed == expected
    assert parse_query(render_query(changed)) == changed
    # Every scalar/projection/condition node survives; only relationship labels change.
    before = [item for item in walk(original) if isinstance(item, (Literal, Alias))]
    after = [item for item in walk(changed) if isinstance(item, (Literal, Alias))]
    assert [item.value for item in before if isinstance(item, Literal)] == [item.value for item in after if isinstance(item, Literal)]


@pytest.mark.parametrize('pattern', PAPER_PATTERNS)
def test_scor_does_not_match_without_its_explicit_contract(pattern):
    query = parse_query(SOURCES[pattern])
    assert pattern not in find_patterns(query)
    with pytest.raises(QueryError):
        mutate(query, pattern)
    wrong = bindings(pattern)
    wrong['scor']['pattern'] = 'S2' if pattern == 'S1' else 'S1'
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=wrong)


@pytest.mark.parametrize('unknown', ['S0', 'S11', 'S01', 'S1_extra'])
def test_scor_identifiers_are_exact_not_prefix_matches(unknown):
    with pytest.raises(QueryError):
        mutate(parse_query(SOURCES['S1']), unknown, bindings=bindings('S1'))


@pytest.mark.parametrize('pattern,source', [
    ('P1', 'MATCH (u)-[:R]-(v) RETURN u.id, v.id LIMIT 1'),
    ('P4', 'MATCH (u) WHERE u.flag = true AND EXISTS { MATCH (u)-[:R]->(v) } RETURN u.id LIMIT 1'),
])
def test_adding_limit_does_not_silently_enable_old_symmetry_on_arbitrary_rows(pattern, source):
    # Equal full bags do not guarantee identical unspecified LIMIT subsets.
    with pytest.raises(QueryError):
        mutate(parse_query(source), pattern)


@pytest.mark.parametrize('pattern,replacement', [('S1', 'false'), ('S1', 'null'), ('S2', 'true'), ('S2', 'null')])
def test_boolean_reference_requires_the_actual_decisive_truth_value(pattern, replacement):
    needle = 'RETURN true OR' if pattern == 'S1' else 'RETURN false AND'
    operator = 'OR' if pattern == 'S1' else 'AND'
    query = parse_query(SOURCES[pattern].replace(needle, f'RETURN {replacement} {operator}'))
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings(pattern))


@pytest.mark.parametrize('pattern', ['S3', 'S4'])
@pytest.mark.parametrize('condition', ['false', 'null'])
def test_selected_high_cost_branch_is_not_a_short_circuit_case(pattern, condition):
    query = parse_query(SOURCES[pattern].replace('WHEN true', f'WHEN {condition}'))
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings(pattern))


@pytest.mark.parametrize('pattern', ['S3', 'S4'])
@pytest.mark.parametrize('condition', ['false', 'null'])
def test_false_and_null_conditions_allow_replacing_an_unselected_then_branch(pattern, condition):
    if pattern == 'S3':
        source = f'CYPHER 25 WHEN {condition} THEN {{ RETURN {HIGH} AS value }} ELSE {{ RETURN 7 AS value }}'
    else:
        source = f'{ROOT} RETURN CASE WHEN {condition} THEN {HIGH} ELSE 7 END AS value'
    query = parse_query(source)
    changed = mutate(query, pattern, bindings=bindings(pattern))
    assert changed == parse_query(source.replace(':GSP_SC_HIGH', ':GSP_SC_REF'))


def test_selected_low_count_is_preserved_while_unselected_high_count_is_replaced():
    low = HIGH.replace('GSP_SC_HIGH', 'GSP_SC_LOW')
    original = parse_query(f'{ROOT} RETURN CASE WHEN true THEN {low} ELSE {HIGH} END AS value')
    changed = mutate(original, 'S4', bindings=bindings('S4'))
    expected = parse_query(f'{ROOT} RETURN CASE WHEN true THEN {low} ELSE {HIGH.replace("GSP_SC_HIGH", "GSP_SC_REF")} END AS value')
    assert changed == expected
    assert sum(isinstance(item, Count) and item.pattern.relationship.type == 'GSP_SC_LOW' for item in walk(changed)) == 1


@pytest.mark.parametrize('pattern', ['S1', 'S2', 'S4', 'S7', 'S8'])
def test_high_reference_outside_the_short_circuited_context_cannot_be_rewritten(pattern):
    # The protected expression remains constant, but this extra output exposes
    # HIGH=40 versus REF=3. A global label replacement would be unsound.
    query = parse_query(SOURCES[pattern] + f', {HIGH} AS exposed_count')
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings(pattern))


def test_decisive_or_projection_does_not_protect_an_expensive_where_filter():
    source = f'{ROOT} WHERE {HIGH} > 10 RETURN true OR ({HIGH} > 0) AS value'
    with pytest.raises(QueryError):
        mutate(parse_query(source), 'S1', bindings=bindings('S1'))


@pytest.mark.parametrize('prefix', ['0', 'false', '""'])
def test_coalesce_falsey_values_are_non_null_and_decisive(prefix):
    query = parse_query(f'{ROOT} RETURN coalesce(null, {prefix}, {HIGH}) AS value')
    changed = mutate(query, 'S7', bindings=bindings('S7'))
    assert changed == parse_query(f'{ROOT} RETURN coalesce(null, {prefix}, {HIGH.replace("GSP_SC_HIGH", "GSP_SC_REF")}) AS value')


@pytest.mark.parametrize('expression', [f'coalesce(null, {HIGH}, 0)', f'coalesce({HIGH}, 0)'])
def test_coalesce_cannot_skip_an_earlier_selected_high_cost_argument(expression):
    query = parse_query(f'{ROOT} RETURN {expression} AS value')
    with pytest.raises(QueryError):
        mutate(query, 'S7', bindings=bindings('S7'))


def test_limit_reference_needs_enough_rows_and_invariant_projection():
    query = parse_query(SOURCES['S6'])
    for count in (0, 1, 2):
        with pytest.raises(QueryError):
            mutate(query, 'S6', bindings=bindings('S6', reference_matches=count, limit=3))
    varying = parse_query(SOURCES['S6'].replace('RETURN 1 AS value', 'RETURN v.uid AS value'))
    with pytest.raises(QueryError):
        mutate(varying, 'S6', bindings=bindings('S6', limit=3))
    for source in (
        SOURCES['S6'].replace('(v:ScorTarget)', '(v:ScorTarget {mark: false})'),
        SOURCES['S6'].replace('[:GSP_SC_HIGH]', '[:GSP_SC_HIGH {flag: false}]'),
    ):
        with pytest.raises(QueryError):
            mutate(parse_query(source), 'S6', bindings=bindings('S6', limit=3))
    # A constant projection makes nondeterministic traversal order irrelevant,
    # while preserving multiplicity. The published empty-table replacement fails.
    high_rows, reference_rows, empty_rows = [1] * min(40, 3), [1] * min(3, 3), []
    assert Counter(high_rows) == Counter(reference_rows) == Counter({1: 3})
    assert Counter(high_rows) != Counter(empty_rows)


def test_first_match_reference_cannot_be_empty_or_predicate_always_false():
    query = parse_query(SOURCES['S8'])
    with pytest.raises(QueryError):
        mutate(query, 'S8', bindings=bindings('S8', reference_matches=0))
    with pytest.raises(QueryError):
        mutate(query, 'S8', bindings=bindings('S8', all_candidates_match=False))
    no_witness = parse_query(SOURCES['S8'].replace('v.key = u.key', 'false'))
    with pytest.raises(QueryError):
        mutate(no_witness, 'S8', bindings=bindings('S8'))
    for source in (
        SOURCES['S8'].replace('(v:ScorTarget)', '(v:ScorTarget {mark: false})'),
        SOURCES['S8'].replace('(v:ScorTarget)', '(v:OtherTarget)'),
        SOURCES['S8'].replace('(u)-[:GSP_SC_HIGH]->', '(u)<-[:GSP_SC_HIGH]-'),
        SOURCES['S8'].replace('(v:ScorTarget)', '(u:ScorTarget)').replace('v.key', 'u.key'),
    ):
        with pytest.raises(QueryError):
            mutate(parse_query(source), 'S8', bindings=bindings('S8'))


@pytest.mark.parametrize('pattern,source', [
    ('S5', SOURCES['S5'].replace('LIMIT 0', 'LIMIT 1')),
    ('S9', SOURCES['S9'].replace('WHERE false', 'WHERE true')),
    ('S9', f'{ROOT} OPTIONAL MATCH (u)-[:GSP_SC_HIGH]->(v) WHERE false RETURN 1 AS value'),
    ('S10', SOURCES['S10'].replace('false AND ', '')),
    ('S10', SOURCES['S10'].replace('RETURN DISTINCT', 'RETURN')),
])
def test_empty_input_rules_require_their_actual_empty_flow_contract(pattern, source):
    query = parse_query(source)
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings(pattern))


def test_global_aggregation_revival_is_outside_the_empty_stream_fragment():
    # An ungrouped count over an empty stream returns one row; it is not an
    # empty relation. The restricted parser must not silently discard WITH.
    source = 'MATCH (e:ScorRoot) WHERE false WITH count(e) AS n MATCH (u)-[:GSP_SC_HIGH]->(v) RETURN n'
    with pytest.raises(QueryError):
        parse_query(source)
    zero_input = []
    aggregate_output = [{'n': len(zero_input)}]
    assert aggregate_output == [{'n': 0}] and aggregate_output != zero_input


def _truth(operator, left, right):
    if operator == 'AND':
        return False if left is False or right is False else None if left is None or right is None else True
    return True if left is True or right is True else None if left is None or right is None else False


def _value(expression, sizes):
    if isinstance(expression, Literal):
        return expression.value
    if isinstance(expression, Alias):
        return _value(expression.expression, sizes)
    if isinstance(expression, Count):
        if isinstance(expression.pattern, Node):
            return int(expression.where is None or _value(expression.where, sizes) is True)
        return sizes[expression.pattern.relationship.type]
    if isinstance(expression, Property):
        return sizes[f"{expression.variable}.{expression.key}"]
    if isinstance(expression, Exists):
        return sizes[expression.pattern.relationship.type] > 0
    if isinstance(expression, Comparison):
        left, right = _value(expression.left, sizes), _value(expression.right, sizes)
        if left is None or right is None:
            return None
        return {'=': lambda: left == right, '>': lambda: left > right,
                '<': lambda: left < right}[expression.operator]()
    if isinstance(expression, Boolean):
        values = [_value(term, sizes) for term in expression.terms]
        return _truth(expression.operator, *values)
    if isinstance(expression, Negation):
        item = _value(expression.expression, sizes)
        return None if item is None else not item
    if isinstance(expression, Call) and expression.name == 'coalesce':
        return next((value for arg in expression.args if (value := _value(arg, sizes)) is not None), None)
    if isinstance(expression, Case):
        branch = expression.then if _value(expression.condition, sizes) is True else expression.otherwise
        return _value(branch, sizes)
    raise AssertionError(f'Unsupported independent test expression: {expression!r}')


@pytest.mark.parametrize('pattern', ['S1', 'S2', 'S3', 'S4', 'S7'])
@pytest.mark.parametrize('high_size,reference_size', [(0, 0), (1, 0), (1000, 3)])
def test_scalar_reference_replacement_has_an_independent_value_oracle(pattern, high_size, reference_size):
    original = parse_query(SOURCES[pattern])
    changed = mutate(original, pattern, bindings=bindings(pattern))
    sizes = {'GSP_SC_HIGH': high_size, 'GSP_SC_REF': reference_size}
    def projection(query):
        if isinstance(query, ConditionalQuery):
            query = query.then if _value(query.condition, sizes) is True else query.otherwise
        return tuple(_value(item, sizes) for item in query.returns)
    assert projection(original) == projection(changed)


@pytest.mark.parametrize('pattern', PAPER_PATTERNS)
def test_generated_case_preserves_paper_identity_and_graph_reference_facts(pattern):
    fixture = Path(__file__).parent / 'fixtures/gdsmith/neo4j_seeds.log'
    seed = load_seeds({'log_files': [str(fixture)]})[0]
    case = instantiate(seed, pattern, 4, 17)
    facts = case['bindings']['scor']
    assert facts['pattern'] == pattern
    assert facts['paper_pattern'] == PAPER_PATTERNS[pattern]
    assert facts['high_type'] == 'GSP_SC_HIGH' and facts['reference_type'] == 'GSP_SC_REF'
    assert facts['high_matches'] > facts['reference_matches'] > 0
    assert case['checks'] and 'expected_rows' in case
    assert case['source']['raw_query'] == seed.text
    assert case['source']['derivation_preserves_original_result'] is False
    assert case['source']['seed_syntax_enriched'] is True
    assert case['source']['paper_doi'] == '10.1145/3802061'
    assert case['source']['paper_table'] == 'Table 1'
    assert case['source']['paper_pattern'] == PAPER_PATTERNS[pattern]
    assert case['source']['oracle_mode'] == 'high_cost_reference_replacement'
    assert case['source']['graph_adaptation']
    changed = mutate(case['query_a'], pattern, bindings=case['bindings'])
    assert changed != case['query_a']
    assert parse_query(render_query(changed)) == changed
    # The source and reference share setup; mutation is not a graph rewrite.
    original_setup = deepcopy(case['setup'])
    mutate(case['query_a'], pattern, bindings=case['bindings'])
    assert case['setup'] == original_setup
    if pattern == 'S6':
        assert facts['reference_matches'] >= case['query_a'].limit
    if pattern == 'S3':
        assert case['required_cypher'] == facts['required_cypher'] == 25
    if pattern == 'S10':
        assert facts['native_set_operator'] is False
        assert facts['null_safe_membership'] is True


@pytest.mark.parametrize('pattern', PAPER_PATTERNS)
def test_scale_changes_high_region_without_changing_the_constructed_answer(pattern):
    source = 'MATCH (u)-[:R]->(v) RETURN u.id, v.id'
    small = instantiate(source, pattern, 1, 17)
    large = instantiate(source, pattern, 10, 17)
    assert small['expected_rows'] == large['expected_rows']
    assert small['query_a'] == large['query_a']
    assert small['setup'] != large['setup']
    assert small['checks'] != large['checks']
    assert small['bindings']['scor']['reference_matches'] == large['bindings']['scor']['reference_matches']
    assert small['bindings']['scor']['high_matches'] < large['bindings']['scor']['high_matches']


@pytest.mark.parametrize('pattern', ['S1', 'S2', 'S3', 'S4', 'S7'])
@pytest.mark.parametrize('complexity', [0, 1, 2])
def test_generated_scalar_role_pools_preserve_the_reference_answer(pattern, complexity):
    sizes = {'GSP_SC_HIGH': 12, 'GSP_SC_REF': 3, 'u.expected_count': 12,
             'u.id': 1, 'u.uid': 1, 'u.key': 1, 'guard.key': 1,
             'v.mark': True, 'v.key': 1}
    for seed in range(108):
        case = instantiate(None, pattern, 3, seed, data_seed=0, complexity=complexity)
        reference = mutate(case['query_a'], pattern, bindings=case['bindings'])
        def projection(query):
            if isinstance(query, ConditionalQuery):
                query = query.then if _value(query.condition, sizes) is True else query.otherwise
            return tuple(_value(item, sizes) for item in query.returns)
        assert projection(case['query_a']) == projection(reference)
        assert projection(reference) == tuple(case['expected_rows'][0].values())
        assert [type(value) for value in projection(reference)] == [type(value) for value in case['expected_rows'][0].values()]


@pytest.mark.parametrize('guard', ['u.key = 1', 'coalesce(u.key, 1) = 1'])
def test_unknown_runtime_guards_cannot_certify_a_scor_replacement(guard):
    source = SOURCES['S1'].replace('RETURN true OR', f'RETURN {guard} OR')
    with pytest.raises(QueryError):
        mutate(parse_query(source), 'S1', bindings=bindings('S1'))


@pytest.mark.parametrize('pattern', ['S1', 'S2', 'S3', 'S4', 'S7'])
@pytest.mark.parametrize('missing', ['guard_facts', 'guard_check', 'guard_cardinality_check'])
def test_dynamic_guards_require_the_full_explicit_checked_binding(pattern, missing):
    case = instantiate(None, pattern, 3, 101, data_seed=0, complexity=2)
    bad = deepcopy(case['bindings'])
    bad['scor'].pop(missing)
    with pytest.raises(QueryError):
        mutate(case['query_a'], pattern, bindings=bad)


@pytest.mark.parametrize('alteration', ['wrong_value', 'wrong_check', 'wrong_scope', 'boolean_as_integer'])
def test_declared_guard_facts_cannot_be_transferred_to_an_unchecked_property_scope(alteration):
    case = instantiate(None, 'S1', 3, 101, data_seed=0, complexity=2)
    bad = deepcopy(case['bindings'])
    query = case['query_a']
    if alteration == 'wrong_value':
        bad['scor']['guard_facts']['key'] = 0
    elif alteration == 'wrong_check':
        bad['scor']['guard_check']['expected_rows'] = [{'id': 1, 'uid': 1, 'key': 0}]
    elif alteration == 'boolean_as_integer':
        bad['scor']['guard_facts']['key'] = True
    else:
        from dataclasses import replace
        query = replace(query, pattern=replace(query.pattern, labels=('UncheckedRoot',)))
    with pytest.raises(QueryError):
        mutate(query, 'S1', bindings=bad)


def test_singleton_root_count_cannot_rebind_an_unchecked_outer_variable():
    case = instantiate(None, 'S1', 3, 101, data_seed=0, complexity=2)
    source = ('MATCH (u:UncheckedRoot) RETURN '
              '(COUNT { MATCH (u:ScorRoot {id: 1}) WHERE u.key = 1 } > 0) OR '
              '(COUNT { MATCH (u)-[:GSP_SC_HIGH]->(v:ScorTarget) } > 0) AS value')
    with pytest.raises(QueryError):
        mutate(parse_query(source), 'S1', bindings=case['bindings'])
