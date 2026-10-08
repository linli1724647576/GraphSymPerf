from itertools import product

import pytest

from mutator import find_patterns, mutate
from query import Boolean, Literal, Node, Property, Query, QueryError, parse_query, render_query


def truth(operator, a, b):
    if operator == 'AND':
        return False if a is False or b is False else None if a is None or b is None else True
    return True if a is True or b is True else None if a is None or b is None else False


@pytest.mark.parametrize('operator,pattern', [('AND', 'P4'), ('OR', 'P5')])
def test_reordering_preserves_all_three_valued_inputs(operator, pattern):
    for a, b in product((True, False, None), repeat=2):
        query = Query(Node('n'), Boolean(operator, (Literal(a), Literal(b))), (Property('n', 'id'),))
        if a is b:
            with pytest.raises(QueryError):
                mutate(query, pattern)
        else:
            changed = mutate(query, pattern)
            x, y = changed.where.terms
            assert truth(operator, a, b) == truth(operator, x.value, y.value)


def test_endpoint_mirror_keeps_constraints_and_projection():
    original = parse_query('MATCH (u:A {id: 1})-[r:R {ok: true}]-(v:B) RETURN u.id, v.id')
    changed = mutate(original, 'P1')
    assert changed.pattern.left == original.pattern.right
    assert changed.pattern.right == original.pattern.left
    assert changed.pattern.relationship == original.pattern.relationship
    assert changed.returns == original.returns
    assert mutate(changed, 'P1') == original
    assert parse_query(render_query(changed)) == changed


def test_p1_does_not_silently_mean_directed_arrow_reversal():
    query = parse_query('MATCH (u)-[:R]->(v) RETURN u.id, v.id')
    assert 'P1' not in find_patterns(query)
    with pytest.raises(QueryError):
        mutate(query, 'P1')


def test_wrong_pattern_or_partial_boolean_order_is_rejected():
    query = parse_query('MATCH (u) WHERE u.x = 1 AND u.y = 2 AND u.z = 3 RETURN u.id')
    assert not find_patterns(query)
    with pytest.raises(QueryError):
        mutate(query, 'P5')


INVERSE = {'inverse_relationship': {'forward_type': 'F', 'reverse_type': 'B', 'pair_key': 'pair'}}


@pytest.mark.parametrize('pattern,source,expected', [
    ('P2', 'MATCH (u:A)-[:F]->(v:B) RETURN u.id, v.id',
     'MATCH (u:A)<-[:B]-(v:B) RETURN u.id, v.id'),
    ('P3', 'MATCH p = (u:A {id: 1})-[:R*1..3]-(v:B) RETURN u.id, v.id, length(p) AS hops',
     'MATCH p = (v:B)-[:R*1..3]-(u:A {id: 1}) RETURN u.id, v.id, length(p) AS hops'),
    ('P6', 'MATCH (u:A)-[:R]->(v) WHERE u.id = 1 RETURN u.id, v.id',
     'MATCH (u:A)-[:R]->(v) WHERE 1 = u.id RETURN u.id, v.id'),
    ('P7', 'MATCH (u:A:B)-[:R]->(v) RETURN u.id, v.id',
     'MATCH (u:B:A)-[:R]->(v) RETURN u.id, v.id'),
    ('P8', 'MATCH (u)-[r:R|S]->(v) RETURN u.id, v.id',
     'MATCH (u)-[r:S|R]->(v) RETURN u.id, v.id'),
    ('P9', 'MATCH (u:A) WHERE u.id = 1 MATCH (u)-[:R]->(v) RETURN u.id, v.id',
     'MATCH (u:A) MATCH (u)-[:R]->(v) WHERE u.id = 1 RETURN u.id, v.id'),
    ('P10', 'MATCH (a:A) MATCH (b:B)-[:R]->(c) RETURN a.id, b.id, c.id',
     'MATCH (b:B)-[:R]->(c) MATCH (a:A) RETURN a.id, b.id, c.id'),
    ('P11', 'MATCH (u:A) MATCH (u)-[:R]->(v) MATCH (u)-[:S]->(w) RETURN u.id, v.id, w.id',
     'MATCH (u:A) MATCH (u)-[:S]->(w) MATCH (u)-[:R]->(v) RETURN u.id, v.id, w.id'),
    ('P12', 'MATCH (u:A)-[:R]->(v) MATCH (v)-[:S]->(w:B) RETURN u.id, w.id',
     'MATCH (v)-[:S]->(w:B) MATCH (u:A)-[:R]->(v) RETURN u.id, w.id'),
    ('P13', 'MATCH (u:A) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (u)-[:S]->(w) RETURN u.id, v.id, w.id',
     'MATCH (u:A) OPTIONAL MATCH (u)-[:S]->(w) OPTIONAL MATCH (u)-[:R]->(v) RETURN u.id, v.id, w.id'),
    ('P14', 'RETURN 1 AS value UNION ALL RETURN 2 AS value',
     'RETURN 2 AS value UNION ALL RETURN 1 AS value'),
    ('P15', 'RETURN CASE WHEN null THEN 11 ELSE 29 END AS value',
     'RETURN CASE WHEN NOT coalesce(null, false) THEN 29 ELSE 11 END AS value'),
    ('P16', 'RETURN all(x IN ([1, 1] + [2, 3]) WHERE x = 1) AS ok',
     'RETURN all(x IN ([2, 3] + [1, 1]) WHERE x = 1) AS ok'),
])
def test_each_extended_pattern_performs_its_named_change(pattern, source, expected):
    query = parse_query(source)
    changed = mutate(query, pattern, bindings=INVERSE if pattern == 'P2' else None)
    assert changed == parse_query(expected)
    assert parse_query(render_query(changed)) == changed


def test_inverse_relationship_replacement_requires_explicit_distinct_type_mapping():
    query = parse_query('MATCH (u)-[:F]->(v) RETURN u.id, v.id')
    for bindings in (None, {}, {'inverse_relationship': {'forward_type': 'F', 'reverse_type': 'F', 'pair_key': 'pair'}},
                     {'inverse_relationship': {'forward_type': 'X', 'reverse_type': 'B', 'pair_key': 'pair'}}):
        with pytest.raises(QueryError):
            mutate(query, 'P2', bindings=bindings)


@pytest.mark.parametrize('source,pattern', [
    ('MATCH (u)-[:R]->(v) RETURN u.id, v.id', 'P2'),
    ('MATCH (u)-[:F]-(v) RETURN u.id, v.id', 'P2'),
    ('MATCH (u)-[r:F]->(v) RETURN u.id, r.id', 'P2'),
    ('MATCH (u:A)-[:R]->(v) RETURN u.id, v.id', 'P7'),
    ('MATCH (u)-[:R]->(v) RETURN u.id, v.id', 'P8'),
    ('MATCH (u:A) MATCH (u)-[:R]->(v) WHERE v.id = 1 RETURN u.id, v.id', 'P9'),
    ('MATCH (u:A) OPTIONAL MATCH (u)-[:R]->(v) WHERE u.id = 1 RETURN u.id, v.id', 'P9'),
    ('MATCH (u) MATCH (u)-[:R]->(v) WHERE EXISTS { MATCH (v)-[:S]->(w) } RETURN u.id, v.id', 'P9'),
    ('MATCH (a)-[:R]->(b) MATCH (b)-[:S]->(c) RETURN a.id, c.id', 'P10'),
    ('MATCH (a) MATCH (b) WHERE EXISTS { MATCH (a)-[:R]->(c) } RETURN a.id, b.id', 'P10'),
    ('MATCH (u) MATCH (u)-[:R]->(v) MATCH (v)-[:S]->(w) RETURN u.id, v.id, w.id', 'P11'),
    ('MATCH (u)-[:R]->(v) MATCH (a)-[:S]->(w) RETURN u.id, w.id', 'P12'),
    ('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (v)-[:S]->(w) RETURN u.id, v.id, w.id', 'P13'),
    ('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (u)-[:S]->(w) WHERE w.id = v.id RETURN u.id, v.id, w.id', 'P13'),
    ('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (u)-[:S]->(w) WHERE EXISTS { MATCH (v)-[:T]->(x) } RETURN u.id, v.id, w.id', 'P13'),
    ('RETURN [1, 2] AS values', 'P16'),
])
def test_similar_syntax_is_rejected_when_the_named_pattern_precondition_is_absent(source, pattern):
    query = parse_query(source)
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=INVERSE if pattern == 'P2' else None)


def test_where_movement_is_reversible_and_preserves_the_two_match_scopes():
    before = parse_query('MATCH (u) WHERE u.id = 1 MATCH (u)-[:R]->(v) RETURN u.id, v.id')
    after = mutate(before, 'P9')
    assert mutate(after, 'P9') == before
    assert render_query(after).upper().count('MATCH') == 2


@pytest.mark.parametrize('source', [
    'MATCH p = (u)-[:R*1..3]-(v) RETURN p',
    'MATCH p = (u)-[:R*1..3]-(v) RETURN nodes(p) AS nodes',
    'MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) OPTIONAL MATCH (u)-[:S]->(w) RETURN missing.id',
    'RETURN all(x IN [1, 2] WHERE x = 1) AS ok, x AS escaped',
    'RETURN 1 AS x UNION ALL RETURN 1 AS y',
])
def test_unsupported_observations_and_scope_leaks_fail_before_mutation(source):
    with pytest.raises(QueryError):
        parse_query(source)
