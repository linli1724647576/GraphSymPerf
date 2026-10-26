"""Composition is driven by operator types, never by a nesting-case registry."""

import json

import pytest

from ir import (All, Alias, Call, Comparison, Count, Edge, Exists, Literal,
                Node, Property, Query, Relationship, Variable, With, from_data, to_data)
from query import parse_query, render_query


def kernel():
    path = Edge(Node("u", ("Root",), (("id", Literal(1)),)),
                Relationship(type="R", min_hops=1, max_hops=3),
                Node("v", ("Target",)), "undirected", "p")
    predicate = All("x", Call("nodes", (Variable("p"),)),
                    Comparison(Property("x", "keep"), "=", Literal(True)))
    return Query(path, predicate, (Property("u", "uid"), Property("v", "uid")))


@pytest.mark.parametrize("contexts", [
    ("exists",), ("count",), ("with",), ("count", "with"),
    ("exists", "with"), ("count", "exists", "count", "with"),
])
def test_recursive_contexts_round_trip_without_combination_builders(contexts):
    from composition import compose_query

    query = compose_query(kernel(), kernel().pattern.left, contexts, threshold=0)
    assert parse_query(render_query(query)) == query
    assert from_data(json.loads(json.dumps(to_data(query)))) == query
    assert render_query(query).count("all(") == 1


def test_with_consumes_the_existing_scalar_and_exports_its_dependency():
    from composition import compose_query

    query = compose_query(kernel(), kernel().pattern.left, ("count", "with"), threshold=2)
    stage = query.pipeline[-1]
    assert isinstance(stage, With)
    value = next(item for item in stage.projections if isinstance(item, Alias))
    assert isinstance(value.expression, Count)
    assert value.expression.body == kernel()
    assert stage.where == Comparison(Variable(value.name), ">", Literal(2))
    assert Variable(value.name) in query.returns


def test_context_aliases_do_not_capture_existing_variables():
    from composition import compose_query

    tree = kernel()
    tree = Query(tree.pattern, tree.where, (*tree.returns, Alias(Literal(7), "gsp_value_0")))
    query = compose_query(tree, tree.pattern.left, ("with",))
    exported = next(item.name for item in query.pipeline[-1].projections if isinstance(item, Alias))
    assert exported != "gsp_value_0"


@pytest.mark.parametrize("contexts,threshold", [(("unknown",), 0), (("count",), True), (("count",), -1)])
def test_invalid_composition_is_rejected(contexts, threshold):
    from composition import compose_query

    with pytest.raises(ValueError):
        compose_query(kernel(), kernel().pattern.left, contexts, threshold=threshold)
