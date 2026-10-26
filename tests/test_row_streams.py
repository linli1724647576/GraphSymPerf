"""A global row demand is distinct from a last-branch LIMIT."""

from dataclasses import replace
import json

import pytest

import ir
from languages import UnsupportedFeature, generate
from query import parse_query, render_query


def constant_branch(variable='u'):
    return ir.Query(ir.Node(variable, ('Root',)), None, (ir.Alias(ir.Literal(1), 'c'),))


def test_global_limit_keeps_recursive_union_shape_and_archive():
    from ir import RowLimit
    first, second = constant_branch('a'), constant_branch('b')
    stream = RowLimit(ir.UnionAll((first, ir.UnionAll((second, first)))), 2)
    text = render_query(stream)
    assert text.startswith('CALL () {')
    assert text.endswith('RETURN c LIMIT 2')
    assert parse_query(text) == stream
    assert ir.from_data(json.loads(json.dumps(ir.to_data(stream)))) == stream
    assert ir.projection_names(stream) == ('c',)
    assert ir.row_stream_schema(stream) == (('c', 'integer'),)
    assert {'row_limit', 'recursive_row_stream', 'union_all', 'call_scope'} <= ir.requirements(stream)


def test_scalar_count_and_exists_accept_correlated_row_streams():
    from ir import RowLimit
    stream = ir.UnionAll((constant_branch(), constant_branch()))
    query = ir.Query(ir.Node('u', ('Root',)), None,
                     (ir.Alias(ir.Count(body=RowLimit(stream, 2)), 'n'),
                      ir.Alias(ir.Exists(body=stream), 'ok')))
    assert ir.row_stream_imports(stream, {'u': 'node'}) == frozenset({'u'})
    assert 'CALL (u) {' in render_query(query)
    assert parse_query(render_query(query)) == query
    assert ir.row_stream_schema(stream, {'u': 'node'}) == (('c', 'integer'),)
    assert ir.query_variables(stream) == frozenset({'u'})


def test_anonymous_count_column_uses_the_same_import_scope_as_the_query():
    from ir import RowLimit
    from query import projection_names
    stream = RowLimit(ir.UnionAll((constant_branch(), constant_branch())), 2)
    tree = ir.Query(ir.Node('u'), None, (ir.Count(body=stream),))
    assert 'CALL (u)' in projection_names(tree)[0]
    assert generate(tree).columns == ((projection_names(tree)[0], projection_names(tree)[0]),)


def test_branch_bindings_are_isolated_and_parent_bindings_remain_correlated():
    from ir import RowLimit
    bad = ir.UnionAll((constant_branch('local'), ir.Query(None, None, (ir.Alias(ir.Property('local', 'uid'), 'c'),))))
    with pytest.raises(ir.QueryError, match='Unbound'):
        ir.validate_query(RowLimit(bad, 1))
    shadow = ir.Query(None, None, (ir.Variable('u'),), pipeline=(ir.With((ir.Alias(ir.Literal(1), 'u'),)),))
    assert ir.row_stream_imports(shadow, {'u': 'node'}) == frozenset()
    nested = ir.Query(None, None, (ir.Alias(ir.Count(body=constant_branch()), 'n'),))
    assert ir.row_stream_imports(nested, {'u': 'node'}) == frozenset({'u'})


def test_global_limit_is_not_last_branch_limit_and_old_encoding_stays_identical():
    from ir import RowLimit
    first, second = constant_branch('a'), constant_branch('b')
    legacy = ir.UnionAll((first, replace(second, limit=2)))
    global_limit = RowLimit(ir.UnionAll((first, second)), 2)
    assert parse_query(render_query(legacy)) == legacy
    assert parse_query(render_query(global_limit)) == global_limit
    assert render_query(global_limit) != render_query(legacy)
    assert set(ir.to_data(first)['query']) == {'kind', 'pattern', 'where', 'returns', 'matches', 'limit', 'distinct'}
    assert ir.to_data(legacy)['query']['kind'] == 'UnionAll'


@pytest.mark.parametrize('limit', [True, -1, 1.5, None])
def test_global_limit_rejects_invalid_demands(limit):
    from ir import RowLimit
    with pytest.raises(ir.QueryError):
        ir.validate_query(RowLimit(constant_branch(), limit))


def test_row_stream_features_are_gated_for_legacy_backends():
    from ir import RowLimit
    stream = RowLimit(ir.UnionAll((constant_branch('a'), constant_branch('b'))), 2)
    assert generate(stream, 'neo4j').text == render_query(stream)
    for backend in ('memgraph', 'falkordb', 'arcadedb', 'age', 'ladybug', 'janusgraph', 'hugegraph'):
        with pytest.raises(UnsupportedFeature):
            generate(stream, backend)


def test_union_schema_joins_nullability_and_checks_column_identity():
    first = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'c'),))
    null = ir.Query(None, None, (ir.Alias(ir.Literal(None), 'c'),))
    assert ir.row_stream_schema(ir.UnionAll((first, null))) == (('c', 'integer?'),)
    wrong = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'other'),))
    with pytest.raises(ir.QueryError, match='identical column'):
        ir.row_stream_schema(ir.UnionAll((first, wrong)))


def test_call_boundary_rejects_output_collision_and_import_rebinding():
    from ir import RowLimit
    collision = ir.Query(ir.Node('u'), None, (ir.Alias(ir.Literal(1), 'u'),))
    query = ir.Query(ir.Node('u'), None, (ir.Alias(ir.Count(body=RowLimit(collision, 1)), 'n'),))
    ir.validate_query(query)
    with pytest.raises(ir.QueryError, match='collide'):
        render_query(query)
    rebound = ir.Query(None, None, (ir.Alias(ir.Literal(1), 'c'),), pipeline=(
        ir.With((ir.Variable('u'),)), ir.With((ir.Alias(ir.Literal(1), 'n'),)), ir.Match(ir.Node('u')),
    ))
    query = replace(query, returns=(ir.Alias(ir.Count(body=RowLimit(rebound, 1)), 'n'),))
    ir.validate_query(query)
    assert ir.row_stream_imports(rebound, {'u': 'node'}) == frozenset({'u'})
    with pytest.raises(ir.QueryError, match='rebind'):
        render_query(query)


@pytest.mark.parametrize('text', [
    'CALL () { RETURN 1 AS c UNION ALL RETURN 1 AS c } RETURN c LIMIT 2',
    'RETURN COUNT { RETURN 1 AS c UNION ALL RETURN 1 AS c } AS n',
    'MATCH (u:Root) RETURN COUNT { CALL (u) { MATCH (u) RETURN 1 AS c UNION ALL MATCH (u) RETURN 1 AS c } RETURN c LIMIT 2 } AS n',
])
def test_only_generated_call_shape_roundtrips(text):
    tree = parse_query(text)
    assert parse_query(render_query(tree)) == tree


@pytest.mark.parametrize('text', [
    'CALL () { RETURN 1 AS c } RETURN 9 AS c LIMIT 2',
    'MATCH (u) RETURN COUNT { CALL () { MATCH (u) RETURN 1 AS c UNION ALL RETURN 1 AS c } RETURN c LIMIT 2 } AS n',
    'CALL (missing) { RETURN 1 AS c } RETURN c LIMIT 2',
    'CALL () { RETURN 1 AS c } RETURN c',
    'CALL () { RETURN 1 AS c UNION ALL RETURN 1 AS c } RETURN c',
    'CALL db.labels() YIELD label RETURN label',
])
def test_arbitrary_calls_or_incorrect_imports_are_rejected(text):
    with pytest.raises(ir.QueryError):
        parse_query(text)


def conditional_stream(condition=None):
    return ir.ConditionalQuery(ir.Literal(True) if condition is None else condition,
                               ir.Query(None, None, (ir.Alias(ir.Literal(7), 'c'),)),
                               ir.Query(None, None, (ir.Alias(ir.Literal(9), 'c'),)))


@pytest.mark.parametrize('consumer', ['count', 'exists', 'union', 'limit', 'conditional'])
def test_query_level_when_is_a_recursive_row_stream_with_one_version_prefix(consumer):
    branch = conditional_stream()
    if consumer == 'count':
        tree = ir.Query(None, None, (ir.Alias(ir.Count(body=branch), 'n'),))
    elif consumer == 'exists':
        branch = conditional_stream(ir.Comparison(ir.Property('u', 'flag'), '=', ir.Literal(True)))
        tree = ir.Query(ir.Node('u'), None, (ir.Alias(ir.Exists(body=branch), 'ok'),))
    elif consumer == 'union':
        tree = ir.UnionAll((branch, conditional_stream(ir.Literal(False))))
    elif consumer == 'limit':
        tree = ir.RowLimit(branch, 1)
    else:
        tree = ir.ConditionalQuery(ir.Literal(True), branch, conditional_stream(ir.Literal(False)))
    text = render_query(tree)
    assert text.startswith('CYPHER 25 ')
    assert text.count('CYPHER 25') == 1
    assert 'WHEN ' in text and 'CASE ' not in text
    assert parse_query(text) == tree
    assert ir.from_data(ir.to_data(tree)) == tree
    assert 'conditional_query' in ir.requirements(tree)
    assert generate(tree, 'neo4j').text == text
    with pytest.raises(UnsupportedFeature):
        generate(tree, 'memgraph')


def test_conditional_stream_imports_and_branch_scopes_are_precise():
    condition = ir.Comparison(ir.Property('u', 'flag'), '=', ir.Literal(True))
    branch = conditional_stream(condition)
    assert ir.row_stream_imports(branch, {'u': 'node', 'unrelated': 'node'}) == frozenset({'u'})
    assert ir.row_stream_schema(branch, {'u': 'node'}) == (('c', 'integer'),)
    first = ir.Query(ir.Node('local'), None, (ir.Alias(ir.Literal(1), 'c'),))
    second = ir.Query(None, None, (ir.Alias(ir.Property('local', 'uid'), 'c'),))
    with pytest.raises(ir.QueryError, match='Unbound'):
        ir.validate_query(ir.ConditionalQuery(ir.Literal(True), first, second))


def test_conditional_branches_compose_union_and_global_limit_without_extra_calls():
    branch = conditional_stream()
    tree = ir.ConditionalQuery(ir.Literal(True),
                               ir.UnionAll((branch.then, branch.otherwise)),
                               ir.RowLimit(branch, 1))
    text = render_query(tree)
    assert parse_query(text) == tree
    assert text.count('CYPHER 25') == 1
    # The RowLimit already provides its own single-query CALL boundary.
    assert 'ELSE { CALL () { WHEN ' in text
    assert 'RETURN c LIMIT 1 }' in text


def test_optional_entity_and_property_nullability_crosses_with_boundary():
    tree = parse_query('MATCH (u) OPTIONAL MATCH (u)-[:R]->(v) WITH v, v.uid AS value RETURN value')
    assert ir.row_stream_schema(tree) == (('value', 'unknown?'),)


def test_fragment_archive_preserves_contextual_terms_without_query_validation():
    fragment = ir.Comparison(ir.Property('outer', 'flag'), '=', ir.Literal(True))
    encoded = ir.encode_fragment(fragment)
    assert set(encoded) == {'version', 'fragment'}
    assert ir.decode_fragment(json.loads(json.dumps(encoded))) == fragment
    match = ir.Match(ir.Node('u'), fragment)
    assert ir.decode_fragment(ir.encode_fragment(match)) == match
    stream = conditional_stream()
    assert ir.decode_fragment(ir.encode_fragment(stream)) == stream
    corrupt = json.loads(json.dumps(encoded))
    corrupt['fragment']['unexpected'] = 1
    with pytest.raises(ir.QueryError):
        ir.decode_fragment(corrupt)
    for wrong in ({'version': True, 'fragment': encoded['fragment']}, {'version': 1, 'query': encoded['fragment']}):
        with pytest.raises(ir.QueryError):
            ir.decode_fragment(wrong)
