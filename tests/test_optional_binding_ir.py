"""WITH aliases retain entity type and NULLability without scalar coercion."""

import pytest

import ir
from query import parse_query, render_query


def aliased_query(*, optional):
    root, endpoint = ir.Node("u", ("Root",)), ir.Node("v", ("Leaf",))
    return ir.Query(None, None, (ir.Property("saved", "uid"),), pipeline=(
        ir.Match(root),
        ir.Match(ir.Edge(root, ir.Relationship(type="R"), endpoint), optional=optional),
        ir.With((ir.Alias(ir.Variable("v"), "saved"),)),
    ))


@pytest.mark.parametrize("optional", [False, True])
def test_with_entity_alias_keeps_its_nullable_or_mandatory_binding_and_roundtrips(optional):
    query = aliased_query(optional=optional)
    ir.validate_query(query)
    assert parse_query(render_query(query)) == query


def test_an_entity_alias_does_not_restore_the_dropped_variable():
    query = aliased_query(optional=True)
    query = ir.Query(None, None, (ir.Property("v", "uid"),), pipeline=query.pipeline)
    with pytest.raises(ir.QueryError, match="Unbound|bound"):
        ir.validate_query(query)


def test_with_entity_alias_does_not_make_entity_values_valid_scalar_outputs():
    query = aliased_query(optional=True)
    query = ir.Query(None, None, (ir.Alias(ir.Variable("saved"), "value"),), pipeline=query.pipeline)
    with pytest.raises(ir.QueryError, match="graph entit|graph properties"):
        ir.validate_query(query)
