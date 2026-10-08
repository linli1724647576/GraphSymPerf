import pytest

from query import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count, Edge, ListExpr, Literal,
    Match, Node, Property, Query, QueryError, UnionAll, Variable, clauses,
    expression_variables, extract_seed_boolean, extract_seed_pattern, parse_query,
    projection_names, render_query, split_statements, with_clauses,
)


@pytest.mark.parametrize("text", [
    'MATCH (u:A {id: 1})-[r:R {ok: true}]-(v:B:C) RETURN u.id, v.id',
    'MATCH (u:A)<-[:R]-(v:B) WHERE u.id = 1 OR v.x IS NULL RETURN u.id',
    'MATCH (u:A) WHERE (u.keep = true) AND EXISTS { MATCH (u)-[:R]->(v:B) WHERE v.mark = true } RETURN u.id',
    'MATCH (u:A) WHERE NOT (u.x IS NOT NULL) RETURN u.id',
    'MATCH (u:A {text: "a; MATCH b", value: 1.5}) RETURN u.id',
    'MATCH (u:A) RETURN u.id LIMIT 1',
    'MATCH (u:A) RETURN DISTINCT u.id LIMIT 0',
    'MATCH (u:A)-[:R|S]->(v) RETURN u.id',
    'MATCH (u:A)-[r:R|:S {flag: true}]->(v) RETURN u.id, r.flag',
    'MATCH p = (u:A)-[:R*0..3]-(v:B) RETURN u.id, v.id, length(p) AS hops',
    'MATCH p = (u)-[:R*2..2]-(v) RETURN length(p) AS hops',
    'MATCH (u:A) WHERE u.id = 1 MATCH (u)-[:R]->(v:B) WHERE v.id <> u.id RETURN u.id,v.id',
    'MATCH (u:A) OPTIONAL MATCH (u)-[:R]->(v:B) OPTIONAL MATCH (u)-[:S]->(w:C) WHERE w.id = v.id RETURN u.id,v.id,w.id',
    'MATCH (a:A) RETURN a.uid AS id UNION ALL MATCH (b:B) RETURN b.uid AS id',
    'RETURN CASE WHEN null THEN 11 ELSE 29 END AS value',
    'RETURN CASE WHEN NOT coalesce(null, false) THEN 29 ELSE 11 END AS value',
    'RETURN all(x IN ([0] + range(1, 3)) WHERE x > 0) AS ok',
    'RETURN all(x IN ([] + [true, null, false]) WHERE x = true) AS ok',
    'RETURN all(x IN [] WHERE x > 0) AS ok',
    'RETURN all(x IN (null + [true]) WHERE x = true) AS ok',
    'RETURN size([1, 2]) AS count, coalesce(null, "value") AS text',
    'MATCH (u) RETURN CASE WHEN u.flag = true THEN [1, 2] ELSE [] END AS values',
    'RETURN COUNT { MATCH (u:Empty) } AS value',
    'RETURN COUNT { MATCH (u:Root)-[:HIGH]->(v:Target) WHERE v.mark = true } AS value',
    'MATCH (u:Root) WHERE true OR COUNT { MATCH (u)-[:HIGH]->(v) WHERE v.mark = true } > 0 RETURN u.uid',
    'MATCH (u:Root) WHERE false AND COUNT { MATCH (u)-[:HIGH]->(v) } > 0 RETURN u.uid',
    'RETURN coalesce(null, 0, COUNT { MATCH (u:Noise) }) AS value',
    'RETURN CASE WHEN true THEN 7 ELSE COUNT { MATCH (u:Noise) } END AS value',
    'CYPHER 25 WHEN true THEN { RETURN 7 AS value } ELSE { RETURN COUNT { MATCH (u:Noise) } AS value }',
    'CYPHER 25 WHEN null THEN { MATCH (u:A) RETURN DISTINCT u.uid AS value LIMIT 2 } ELSE { RETURN 7 AS value LIMIT 0 }',
    'CYPHER 25 WHEN COUNT { MATCH (u:Flag) } > 0 THEN { RETURN 1 AS value } ELSE { RETURN 0 AS value }',
    'RETURN 1 AS value LIMIT 2 UNION ALL RETURN 2 AS value LIMIT 0',
])
def test_query_roundtrip(text):
    ast = parse_query(text)
    assert parse_query(render_query(ast)) == ast


@pytest.mark.parametrize("text", [
    'MATCH (u:A) RETURN absent.id',
    'MATCH (u:A) WHERE rand() = 1 RETURN u.id',
    'MATCH (u:A) SET u.x = 1 RETURN u.id',
    'MATCH (u)-[:R]->(v)-[:S]->(w) RETURN u.id',
    'MATCH (u:A) RETURN u.id, u.id',
    'MATCH (u:A) WHERE 3 AND true RETURN u.id',
    'MATCH (u:A) WHERE EXISTS { MATCH (v:B) } RETURN u.id',
    'MATCH (u:A)-[:R|R]->(v) RETURN u.id',
    'MATCH (u)-[:R*3..1]-(v) RETURN u.id',
    'MATCH (u)-[:R*1..]-(v) RETURN u.id',
    'MATCH (u)-[:R*1..3]->(v) RETURN u.id',
    'MATCH (u)-[r:R*1..3]-(v) RETURN u.id',
    'MATCH p = (u)-[:R*1..3]-(v) RETURN p',
    'MATCH p = (u)-[:R*1..3]-(v) RETURN nodes(p)',
    'MATCH u = (u)-[:R*1..3]-(v) RETURN u.id',
    'MATCH (u) MATCH (u)-[u:R]->(v) RETURN v.id',
    'MATCH (u) WHERE v.id = 1 MATCH (v) RETURN u.id',
    'MATCH (u) WHERE EXISTS { MATCH (u)-[:R]->(v) } RETURN v.id',
    'MATCH (u) WHERE EXISTS { MATCH (v)-[u:R]->(w) } RETURN u.id',
    'OPTIONAL MATCH (u) RETURN u.id',
    'MATCH (u) RETURN CASE WHEN u.flag THEN 1 ELSE 2 END AS x',
    'RETURN CASE WHEN 1 THEN 11 ELSE 29 END AS value',
    'RETURN all(x IN [1, 2] WHERE x) AS ok',
    'RETURN all(x IN [1, 2] WHERE x > 0) AS ok, x',
    'MATCH (x) RETURN all(x IN [1] WHERE x = 1) AS ok',
    'RETURN all(x IN 3 WHERE x = true) AS ok',
    'RETURN all(x IN ([1] + true) WHERE x = 1) AS ok',
    'RETURN range(1, 4, 0) AS bad',
    'RETURN range(1.5, 4) AS bad',
    'RETURN length(1) AS bad',
    'RETURN coalesce() AS bad',
    'RETURN 9223372036854775808 AS bad',
    'MATCH (u) RETURN CASE WHEN true THEN u ELSE 1 END AS bad',
    'MATCH (u) RETURN size(u.unknown) AS bad',
    'MATCH (u) WHERE u.unknown > 0 RETURN u.id',
    'RETURN all(x IN [true, 1] WHERE x > 0) AS bad',
    'RETURN 1 AS x UNION ALL RETURN 2 AS y',
    'RETURN 1 AS x, 2 AS x',
    'RETURN 1 AS x UNION RETURN 2 AS x',
    'MATCH (u:A) RETURN u.id LIMIT -1',
    'MATCH (u:A) RETURN u.id LIMIT 1.5',
    'MATCH (u:A) RETURN u.id LIMIT true',
    'MATCH (u:A) RETURN u.id LIMIT $n',
    'RETURN COUNT { MATCH (u:Noise) WHERE u.flag } AS value',
    'RETURN COUNT { MATCH (u:Noise) } AS value, u.uid',
    'MATCH (u) RETURN COUNT { MATCH (v)-[u:R]->(w) } AS value',
    'MATCH (u) RETURN count(u) AS value',
    'CYPHER 25 WHEN 1 THEN { RETURN 1 AS value } ELSE { RETURN 2 AS value }',
    'CYPHER 25 WHEN u.flag = true THEN { MATCH (u) RETURN 1 AS value } ELSE { RETURN 2 AS value }',
    'CYPHER 25 WHEN true THEN { RETURN 1 AS x } ELSE { RETURN 2 AS y }',
    'CYPHER 25 WHEN true THEN { CYPHER 25 WHEN true THEN { RETURN 1 AS x } ELSE { RETURN 2 AS x } } ELSE { RETURN 2 AS x }',
    'CYPHER 25 WHEN true THEN { RETURN 1 AS x UNION ALL RETURN 2 AS x } ELSE { RETURN 2 AS x }',
    'CYPHER 25 WHEN true THEN { RETURN 1 AS x }',
    'CYPHER 5 WHEN true THEN { RETURN 1 AS x } ELSE { RETURN 2 AS x }',
])
def test_unsupported_or_unbound_queries_fail(text):
    with pytest.raises(QueryError):
        parse_query(text)


def test_extracts_incoming_edge_from_real_gdsmith_shape():
    text = 'MATCH (n1)<-[r1 :T1]-(n2 :L2 :L0) WHERE ((n2.k6) AND true) UNWIND [(r1.k51)] AS a0 RETURN max("c") AS a1;'
    edge = extract_seed_pattern(text)
    assert isinstance(edge, Edge)
    assert (edge.left.variable, edge.right.variable, edge.direction) == ('n1', 'n2', 'in')
    assert extract_seed_boolean(text, 'AND') is not None
    assert extract_seed_boolean(text, 'OR') is None


def test_keywords_in_quoted_values_are_not_boolean_sites():
    text = 'MATCH (n:A) WHERE n.x = "WHERE true OR false" RETURN n.id'
    assert extract_seed_boolean(text, 'OR') is None


def test_statement_split_respects_literals_and_comments():
    script = '-- seed value: 1\nCREATE (n {x: "a;b"}); // comment;\nRETURN ";"; /* ; */ RETURN 2;'
    assert split_statements(script) == ('CREATE (n {x: "a;b"})', 'RETURN ";"', 'RETURN 2')
    with pytest.raises(QueryError):
        split_statements("RETURN 'unterminated")


def test_clause_helpers_keep_where_attached_and_legacy_constructor():
    first = Node('u', ('A',))
    q = Query(first, None, (Property('u', 'uid'),))
    assert clauses(q) == (Match(first),)
    expanded = with_clauses(q, (Match(first, Comparison(Property('u', 'uid'), '=', Literal(1))), Match(Node('v'), optional=True)))
    assert expanded.pattern == first and expanded.matches[0].optional
    assert parse_query(render_query(expanded)) == expanded
    with pytest.raises(QueryError, match='first MATCH'):
        with_clauses(q, (Match(first, optional=True),))


def test_optional_variables_remain_available_and_nullable_in_later_clauses():
    q = parse_query('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) MATCH (w) WHERE v.id IS NULL RETURN u.id,v.id,w.id')
    assert clauses(q)[1].optional
    assert isinstance(clauses(q)[2].where, Comparison)
    assert parse_query(render_query(q)) == q


def test_union_uses_alias_names_not_expression_spelling():
    q = parse_query('MATCH (a) RETURN a.id AS id UNION ALL MATCH (b) RETURN b.id AS id')
    assert isinstance(q, UnionAll)
    assert projection_names(q.branches[0]) == ('id',)
    assert projection_names(q.branches[1]) == ('id',)


def test_all_binds_only_its_predicate_and_exists_references_are_conservative():
    q = parse_query('MATCH (u) RETURN all(x IN [1] WHERE x = u.id) AS ok')
    assert expression_variables(q.returns[0].expression) == frozenset({'u'})
    q = parse_query('MATCH (u) WHERE EXISTS { MATCH (u)-[:R]->(v) WHERE v.id = u.id } RETURN u.id')
    assert expression_variables(q.where) == frozenset({'u', 'v'})


def test_literal_identity_keeps_cypher_value_types_distinct():
    assert Literal(True) != Literal(1)
    assert Literal(False) != Literal(0)
    assert Literal(1) != Literal(1.0)
    assert len({Literal(True), Literal(1), Literal(1.0)}) == 3


def test_named_path_seed_extraction_preserves_first_edge_and_stops_before_later_clauses():
    p = extract_seed_pattern('MATCH p=(u:A)-[:R*2..2]-(v:B) WHERE true MATCH (w) RETURN length(p)')
    assert p.path_variable == 'p'
    assert p.relationship.min_hops == p.relationship.max_hops == 2
    edge = extract_seed_pattern('MATCH (u)-[:R]->(v)-[:S]->(w) WHERE true RETURN u.id')
    assert (edge.left.variable, edge.right.variable) == ('u', 'v')


def test_extract_boolean_still_uses_real_subtrees_with_new_keywords():
    source = 'MATCH (n) WHERE n.x = 1 OR n.y = 2 RETURN n.id UNION ALL MATCH (m) RETURN m.id'
    start, end = extract_seed_boolean(source, 'OR')
    assert source[start:end].strip() == 'n.x = 1 OR n.y = 2'


def test_split_statements_is_at_top_level_and_checks_delimiters():
    assert split_statements('CALL { RETURN 1; } RETURN 2; RETURN 3') == ('CALL { RETURN 1; } RETURN 2', 'RETURN 3')
    with pytest.raises(QueryError, match='Unbalanced'):
        split_statements('CREATE (u;')


def test_count_has_its_own_scope_and_numeric_type():
    q = parse_query('MATCH (u:Root) RETURN COUNT { MATCH (v:Noise) WHERE v.uid = u.uid } AS value')
    count = q.returns[0].expression
    assert isinstance(count, Count)
    assert expression_variables(count) == frozenset({'u', 'v'})
    assert parse_query('RETURN COUNT { MATCH (v:Empty) } > 0 AS present').pattern is None


def test_limit_belongs_to_each_union_branch():
    q = parse_query('RETURN 1 AS value LIMIT 2 UNION ALL RETURN 2 AS value LIMIT 0')
    assert [branch.limit for branch in q.branches] == [2, 0]
    assert all(not branch.distinct for branch in q.branches)


def test_distinct_and_limit_survive_clause_replacement():
    q = Query(Node('u'), None, (Property('u', 'uid'),), (), 0, True)
    changed = with_clauses(q, (Match(Node('v')),))
    assert (changed.limit, changed.distinct) == (0, True)


@pytest.mark.parametrize('limit,distinct', [(True, False), (-1, False), (1.5, False), (0, 1)])
def test_programmatic_limit_and_distinct_types_are_checked(limit, distinct):
    q = Query(None, None, (Alias(Literal(1), 'value'),), limit=limit, distinct=distinct)
    with pytest.raises(QueryError):
        render_query(q)


def test_conditional_has_an_explicit_version_and_branch_column_names():
    q = ConditionalQuery(Literal(True), Query(None, None, (Alias(Literal(7), 'value'),)), Query(None, None, (Alias(Count(Node('u', ('Noise',))), 'value'),)))
    rendered = render_query(q)
    assert rendered.startswith('CYPHER 25 WHEN true THEN {')
    assert isinstance(parse_query(rendered), ConditionalQuery)
    assert projection_names(q) == ('value',)
    assert not render_query(q.then).startswith('CYPHER')


def test_conditional_script_split_keeps_count_subquery_and_literals():
    source = 'CYPHER 25 WHEN true THEN { RETURN ";" AS value } ELSE { RETURN COUNT { MATCH (u) } AS value }; RETURN 1 AS value;'
    statements = split_statements(source)
    assert len(statements) == 2
    assert isinstance(parse_query(statements[0]), ConditionalQuery)
    assert parse_query(statements[1]).returns == (Alias(Literal(1), 'value'),)
