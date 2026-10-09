import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import query
from generator import instantiate, load_seeds
from ir import (Alias, Boolean, Comparison, Literal, Node, Property, Query,
                QueryError, Relationship, from_data, projection_names,
                requirements, to_data, validate_query)
from mutator import mutate


def test_validation_and_logical_identity_do_not_invoke_cypher_renderer(monkeypatch):
    expression = Boolean("AND", (Comparison(Property("u", "keep"), "=", Literal(True)), Literal(True)))
    tree = Query(Node("u", ("B", "A")), expression, (Alias(expression, "value"),))

    def forbidden(*args, **kwargs):
        raise AssertionError("IR validation invoked a language renderer")

    for name in ("render_query", "render_pattern", "render_expression"):
        monkeypatch.setattr(query, name, forbidden)
    validate_query(tree)
    assert projection_names(tree) == ("value",)
    assert from_data(to_data(tree)) == tree


def test_archive_preserves_types_labels_boolean_and_branch_order():
    tree = query.parse_query('MATCH (u:B:A {x: 1.0, y: true}) WHERE false OR u.keep = true RETURN u.uid AS id UNION ALL MATCH (v:C) RETURN v.uid AS id')
    data = to_data(tree)
    restored = from_data(json.loads(json.dumps(data)))
    assert restored == tree
    first = restored.branches[0]
    assert first.pattern.labels == ("B", "A")
    assert type(first.pattern.properties[0][1].value) is float
    assert type(first.pattern.properties[1][1].value) is bool
    assert first.where.terms[0] == Literal(False)
    assert to_data(restored) == data


@pytest.mark.parametrize("pattern", [f"P{i}" for i in range(1, 17)] + [f"S{i}" for i in range(1, 11)])
def test_pattern_and_transformed_ir_archive_roundtrip(pattern):
    path = Path(__file__).parent / "fixtures/gdsmith/neo4j_seeds.log"
    seeds = load_seeds({"log_files": [str(path)]})
    case = instantiate(seeds[1 if pattern == "P5" else 0], pattern, 4, 17)
    tree = case["query_a"]
    changed = mutate(tree, pattern, bindings=case["bindings"])
    for value in (tree, changed):
        assert from_data(json.loads(json.dumps(to_data(value)))) == value
        assert query.parse_query(value.render()) == value


def test_path_mode_is_explicit_in_old_constructor_and_archive():
    tree = query.parse_query('MATCH p = (u)-[:R*0..3]-(v) RETURN length(p) AS hops')
    assert tree.pattern.relationship.path_mode == "trail"
    assert Relationship().path_mode == "trail"
    assert {"bounded_path", "path_mode_trail", "function_length"} <= requirements(tree)
    changed = replace(tree, pattern=replace(tree.pattern, relationship=replace(tree.pattern.relationship, path_mode="walk")))
    validate_query(changed)
    assert from_data(to_data(changed)) == changed
    with pytest.raises(QueryError, match="trail"):
        changed.render()


@pytest.mark.parametrize("corruption", ["version", "unknown_node", "unexpected_field", "unbound", "nonfinite"])
def test_invalid_archive_is_rejected(corruption):
    data = deepcopy(to_data(query.parse_query('MATCH (u:A) RETURN u.uid')))
    if corruption == "version":
        data["version"] = 2
    elif corruption == "unknown_node":
        data["query"]["kind"] = "ArbitraryPythonObject"
    elif corruption == "unexpected_field":
        data["query"]["extra"] = 1
    elif corruption == "unbound":
        data["query"]["returns"][0]["variable"] = "absent"
    else:
        data["query"]["returns"] = [{"kind": "Literal", "value": float("nan")}]
    with pytest.raises(QueryError):
        from_data(data)


def test_features_describe_the_actual_tree_not_a_pattern_label():
    tree = query.parse_query('MATCH p=(u:A:B)-[:R|S*1..3]-(v) WHERE all(x IN nodes(p) WHERE x.keep = true) OPTIONAL MATCH (u)-[:T]->(w) RETURN DISTINCT u.uid LIMIT 2')
    assert {"multi_label_conjunction", "relationship_alternatives", "bounded_path",
            "path_mode_trail", "node_local_path_filter", "optional_match",
            "all", "function_nodes", "distinct", "limit"} <= requirements(tree)
