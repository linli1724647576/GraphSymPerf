"""Recursive scope, query body and archive contracts without a live database."""

from copy import deepcopy

import pytest

from ir import (
    Alias, Count, Edge, Exists, Literal, Match, Node, Property, Query,
    QueryError, Relationship, Variable, With, clauses, expression_variables,
    from_data, requirements, to_data, validate_query, with_clauses,
)
from query import parse_query, render_query


@pytest.mark.parametrize("text", [
    "MATCH (u:Root) WITH u, 2 AS n WHERE n > 0 MATCH (u)-[:R]->(v) WITH v, n AS m RETURN v.uid, m",
    "WITH 2 AS n WITH DISTINCT n WHERE n > 0 RETURN n",
    "MATCH p=(u)-[:R*1..3]-(v) WITH p, u RETURN u.uid, length(p) AS hops",
    "MATCH (u) WHERE EXISTS { MATCH (u)-[:R]->(v) RETURN v.uid } RETURN u.uid",
    "MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) WITH v MATCH (v)-[:S]->(w) RETURN w.uid LIMIT 2 } AS n",
    "RETURN COUNT { WITH 1 AS x WHERE x > 0 RETURN x } AS n",
    "MATCH (u) WHERE EXISTS { MATCH (u)-[:R]->(v) WHERE EXISTS { MATCH (v)-[:S]->(w) WITH w RETURN w.uid } WITH v RETURN v.uid } RETURN u.uid",
    "MATCH (u) RETURN COUNT { RETURN u.uid AS id } AS n",
    "MATCH (u) RETURN CASE WHEN EXISTS { WITH u RETURN u.uid } THEN COUNT { MATCH (u)-[:R]->(v) RETURN v.uid } ELSE 0 END AS n",
    "MATCH (u) WHERE EXISTS { RETURN u.uid UNION ALL RETURN u.uid } RETURN u.uid",
])
def test_recursive_query_roundtrips_preserve_scope_and_archive(text):
    tree = parse_query(text)
    assert parse_query(render_query(tree)) == tree
    assert from_data(to_data(tree)) == tree


@pytest.mark.parametrize("text", [
    "MATCH (u) WITH 1 AS n RETURN u.uid",
    "MATCH (u) WITH u.uid RETURN u.uid",
    "MATCH (u) WITH u AS x RETURN u.uid",
    "MATCH (u) WITH u, 1 AS u RETURN u.uid",
    "WITH 1 AS a, a AS b RETURN b",
    "MATCH (u) WITH 1 AS n WHERE u.flag=true RETURN n",
    "MATCH (u) WITH 1 AS u MATCH (u)-[:R]->(v) RETURN v.uid",
    "MATCH (u) WHERE EXISTS { WITH 1 AS n MATCH (u) RETURN u.uid } RETURN u.uid",
    "MATCH (u) WHERE EXISTS { WITH 1 AS u RETURN u } RETURN u.uid",
    "MATCH (u) WHERE EXISTS { MATCH (u)-[:R]->(v) RETURN v.uid } RETURN v.uid",
    "MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) WITH u RETURN v.uid } AS n",
    "MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) RETURN v } AS n",
    "MATCH p=(u)-[:R*1..3]-(v) WITH p RETURN p",
])
def test_recursive_queries_reject_scope_leaks_ambiguous_exports_and_unsupported_bodies(text):
    with pytest.raises(QueryError):
        parse_query(text)


@pytest.mark.parametrize("operator", ["COUNT", "EXISTS"])
@pytest.mark.parametrize("body", ["MATCH (v:Independent) RETURN v.uid", "RETURN 1 AS value"])
def test_complete_subqueries_allow_genuinely_independent_row_streams(operator, body):
    from ir import row_stream_imports
    from languages import UnsupportedFeature, generate

    tree = parse_query(f"MATCH (u:Outer) RETURN {operator} {{ {body} }} AS value")
    expression = tree.returns[0].expression
    assert row_stream_imports(expression.body, {"u": "node"}) == frozenset()
    assert parse_query(render_query(tree)) == tree
    assert from_data(to_data(tree)) == tree
    assert "query_subquery" in requirements(tree)
    with pytest.raises(UnsupportedFeature):
        generate(tree, "memgraph")


def test_legacy_pattern_only_exists_still_requires_an_outer_binding():
    from ir import Exists, validate_query

    with pytest.raises(QueryError, match="outer variable"):
        validate_query(Query(Node("u"), None, (Alias(Exists(Node("v")), "value"),)))


def test_body_and_short_subquery_are_exclusive():
    body = Query(None, None, (Alias(Literal(1), "x"),))
    for expression in (
        Count(Node("v"), body=body),
        Count(where=Literal(True), body=body),
        Exists(Node("u"), body=body),
        Count(),
    ):
        with pytest.raises(QueryError):
            validate_query(Query(Node("u"), None, (Alias(expression, "n"),)))


def test_pipeline_and_legacy_clauses_are_exclusive_and_ordered():
    stage = With((Alias(Literal(1), "n"),))
    invalid = (
        Query(Node("u"), None, (Literal(1),), pipeline=(stage,)),
        Query(None, None, (Literal(1),), pipeline=[stage]),
        Query(None, None, (Literal(1),), pipeline=(With(()),)),
        Query(None, None, (Literal(1),), pipeline=(With((Literal(1),)),)),
        Query(None, None, (Literal(1),), pipeline=(With((Alias(Literal(1), "n"),), distinct=1),)),
    )
    for tree in invalid:
        with pytest.raises(QueryError):
            validate_query(tree)


def test_clause_helper_preserves_pipelines_and_can_reset_them():
    tree = parse_query("MATCH (u) WITH u RETURN u.uid")
    changed = with_clauses(tree, (*clauses(tree), Match(Edge(Node("u"), Relationship(type="R"), Node("v")))))
    assert changed.pattern is None and not changed.matches
    assert isinstance(clauses(changed)[1], With)
    assert parse_query(render_query(changed)) == changed
    reset = with_clauses(tree, ())
    assert not reset.pipeline and not clauses(reset)


def test_legacy_archive_omits_new_empty_fields_and_keeps_strict_old_fields():
    tree = parse_query("MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) } AS n")
    archive = to_data(tree)
    assert set(archive["query"]) == {"kind", "pattern", "where", "returns", "matches", "limit", "distinct"}
    assert set(archive["query"]["returns"][0]["expression"]) == {"kind", "pattern", "where"}
    assert from_data(archive) == tree
    for field in ("where", "matches", "limit", "distinct"):
        corrupt = deepcopy(archive)
        del corrupt["query"][field]
        with pytest.raises(QueryError):
            from_data(corrupt)
    corrupt = deepcopy(archive)
    corrupt["query"]["unexpected"] = None
    with pytest.raises(QueryError):
        from_data(corrupt)


def test_recursive_requirements_and_references_come_from_structure():
    tree = parse_query("MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) WITH DISTINCT v RETURN v.uid } AS n")
    assert {"with_clause", "query_subquery", "count_subquery", "distinct"} <= requirements(tree)
    assert "return_only" not in requirements(tree)
    count = tree.returns[0].expression
    assert count.pattern is None
    assert expression_variables(count) == frozenset({"u", "v"})
    plain = parse_query("MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) } AS n")
    assert not {"with_clause", "query_subquery"}.intersection(requirements(plain))
