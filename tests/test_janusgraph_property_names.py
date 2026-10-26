"""Lossless JanusGraph property slots; the logical query/state stay unchanged."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from executor import CaseFailure, JanusGraph, _gremlin_state_checks
from languages import (JANUSGRAPH_PROPERTY_PREFIX, gremlin_property_key,
                       logical_gremlin_property_key, generate)
from main import prepare_legacy_case as prepare_case
from query import parse_query
from state import GraphSpec


VERIFIED = ("gremlin_null_projection",)


@pytest.mark.parametrize("key", ["key", "Key", "KEY", "vertex", "edge", "element", "property", "label",
                                 "_gsp_property_key", "_gsp_property__gsp_property_key", "uid", "id"])
def test_native_property_names_round_trip_without_collision(key):
    physical = gremlin_property_key(key, "janusgraph")
    assert logical_gremlin_property_key(physical, "janusgraph") == key
    if key.lower() in {"key", "vertex", "edge", "element", "property", "label"} or key.startswith(JANUSGRAPH_PROPERTY_PREFIX):
        assert physical == JANUSGRAPH_PROPERTY_PREFIX + key
    else:
        assert physical == key
    for target in ("hugegraph", "arcadedb", "tinkergraph"):
        assert gremlin_property_key(key, target) == logical_gremlin_property_key(key, target) == key


def test_property_encoding_is_injective_even_when_the_escape_prefix_is_a_logical_name():
    names = {"key", "Key", "label", "ordinary"}
    for _ in range(10):
        names |= {JANUSGRAPH_PROPERTY_PREFIX + key for key in names}
    physical = {gremlin_property_key(key, "janusgraph") for key in names}
    assert len(physical) == len(names)
    assert {logical_gremlin_property_key(key, "janusgraph") for key in physical} == names


def test_only_property_slots_change_in_maps_filters_null_checks_and_scalar_reads():
    source = ("MATCH (u:key {key:1})-[r:R {key:2}]->(v:vertex) "
              "WHERE (u.key = v.key AND r.key IS NOT NULL) OR v.key IS NULL "
              "RETURN u.key AS key, r.key AS rel_key, 'key' AS literal")
    tree = parse_query(source)
    before = deepcopy(tree)
    janus = generate(tree, "janusgraph", "gremlin", verified_features=VERIFIED)
    huge = generate(tree, "hugegraph", "gremlin", verified_features=VERIFIED)
    assert tree == before and janus.columns == huge.columns
    assert janus.bindings == huge.bindings and "key" in janus.bindings.values()
    assert ".hasLabel('key')" in janus.text and ".hasLabel('vertex')" in janus.text
    assert ".has('_gsp_property_key',P.eq(" in janus.text
    assert ".has('_gsp_property_key')" in janus.text
    assert ".hasNot('_gsp_property_key')" in janus.text
    assert ".values('_gsp_property_key')" in janus.text
    assert "_gsp_property_" not in huge.text


def test_nullable_and_nested_correlated_property_reads_use_the_same_slot():
    tree = parse_query("MATCH (u:A {key:1}) OPTIONAL MATCH (u)-[:R]->(v:B) "
                       "RETURN coalesce(v.key, 0) AS key, "
                       "COUNT { MATCH (u)-[:R]->(w:B) WHERE w.key = u.key } AS n, "
                       "EXISTS { MATCH (u)-[:R]->(w:B) WHERE w.key IS NULL } AS yes")
    compiled = generate(tree, "janusgraph", "gremlin", verified_features=VERIFIED)
    assert "values('key')" not in compiled.text
    assert "values('_gsp_property_key')" in compiled.text
    assert "hasNot('_gsp_property_key')" in compiled.text
    assert compiled.columns == (("c0", "key"), ("c1", "n"), ("c2", "yes"))


def test_schema_and_indexes_use_encoded_native_keys_without_mutating_logical_schema():
    schema = {"nodes": [{"label": "A", "properties": {"uid": "integer", "key": "integer",
                                                       "_gsp_property_key": "integer"}}],
              "relationships": [{"type": "GSP_R", "properties": {"Key": "boolean"}}]}
    spec = SimpleNamespace(schema=lambda: schema, indexes=[{"name": "gsp_cost_lookup", "label": "A", "property": "key"}])
    before, calls = deepcopy(schema), []
    db = JanusGraph.__new__(JanusGraph)
    db.settings = {"timeout_seconds": 2}
    db._script = lambda text: (calls.append(text) or [True], 1.0)
    db._prepare_schema(spec)
    assert schema == before
    assert db.property_name_mapping == {"key": "_gsp_property_key", "_gsp_property_key": "_gsp_property__gsp_property_key", "Key": "_gsp_property_Key"}
    assert 'm.makePropertyKey("_gsp_property_key")' in calls[0]
    assert 'm.makePropertyKey("_gsp_property__gsp_property_key")' in calls[0]
    assert 'm.getPropertyKey("key")' not in calls[0]
    assert '.addKey(m.getPropertyKey("_gsp_property_key"))' in calls[0]
    for text in (db._vertex_script(), db._edge_script()):
        assert "gsp_property_key(k)" in text
        assert "toLowerCase(java.util.Locale.ROOT)" in text
        assert 'k.startsWith("_gsp_property_")' in text


@pytest.mark.parametrize("physical,passed", [("_gsp_property_key", True), ("key", False)])
def test_encoded_index_evidence_retains_native_and_logical_properties(physical, passed):
    db = JanusGraph.__new__(JanusGraph)
    db._script = lambda text: ([{"name": "gsp_cost_lookup_A_key", "keys": [physical], "state": ["ENABLED"], "label": "A", "composite": True}], 1.0)
    spec = SimpleNamespace(indexes=[{"name": "gsp_cost_lookup", "label": "A", "property": "key"}])
    if not passed:
        with pytest.raises(CaseFailure, match="janusgraph_cost_index_not_enabled"):
            db._check_indexes(spec)
    else:
        evidence, = db._check_indexes(spec)
        assert evidence["keys"] == [physical]
        assert evidence["logical_property"] == "key" and evidence["physical_property"] == physical


def test_ordinary_index_evidence_keeps_its_existing_shape():
    db = JanusGraph.__new__(JanusGraph)
    saved = {"name": "gsp_cost_lookup_A_id", "keys": ["id"], "state": ["ENABLED"], "label": "A", "composite": True}
    db._script = lambda text: ([saved], 1.0)
    spec = SimpleNamespace(indexes=[{"name": "gsp_cost_lookup", "label": "A", "property": "id"}])
    assert db._check_indexes(spec) == [{"kind": "label_scoped_composite", **saved}]


def test_state_check_queries_map_native_slots_but_keep_logical_projection_and_expected_rows():
    checks = [
        {"kind": "node_count", "labels": ["A"], "properties": {"key": 1}, "expected": 1},
        {"kind": "edge_count", "type": "GSP_R", "source_properties": {"key": 1}, "target_properties": {"key": None}, "expected": 1},
        {"kind": "node_properties", "label": "A", "uid": 1, "properties": {"key": None, "_gsp_property_key": False}},
        {"kind": "distinct_values", "label": "A", "property": "key", "expected": 1},
        {"kind": "inverse_edges", "forward": "GSP_R", "reverse": "GSP_REVERSE", "pair_key": "key", "expected": 0},
        {"kind": "reference_edges", "reference": "GSP_REF", "original": "GSP_R", "source_label": "A", "target_label": "B", "guard_property": "key", "guard_value": False, "expected": 0},
    ]
    before = deepcopy(checks)
    native = _gremlin_state_checks(checks, target="janusgraph", lambda_free=True)
    ordinary = _gremlin_state_checks(checks, lambda_free=True)
    assert checks == before
    assert [item["expected_rows"] for item in native] == [item["expected_rows"] for item in ordinary]
    assert '.project("key", "_gsp_property_key")' in native[2]["query"]
    assert 'values("_gsp_property__gsp_property_key")' in native[2]["query"]
    assert all('"_gsp_property_key"' in item["query"] for item in (*native[:5], native[-1]))
    assert native[5] == ordinary[5]  # The unguarded reference direction reads no property.


def test_janus_state_check_dispatch_maps_property_slots_and_exports_mapping_evidence():
    spec = GraphSpec("S1", 3, 0)
    case = {"graph_spec": spec.to_dict()}
    expected = _gremlin_state_checks(spec.state_checks(), target="janusgraph")
    db = JanusGraph.__new__(JanusGraph)
    db.settings = {"timeout_seconds": 2}
    db.property_name_mapping = {"key": "_gsp_property_key"}
    pending = iter(expected)
    def execute(text, parameters, timeout):
        check = next(pending)
        assert text == check["query"]
        return {"rows": check["expected_rows"], "complete": True, "elapsed_ms": 1.0}
    db.execute = execute
    db._check_indexes = lambda supplied: []
    evidence = db.check_state(case)
    assert evidence["property_name_mapping"] == db.property_name_mapping
    assert evidence["checks"] == [{"query": item["query"], "rows": item["expected_rows"]} for item in expected]


@pytest.mark.parametrize("pattern,digests", [
    ("P3", ("6d4ae95b4d6a927807ea518e195a0217427f9b8246e4aac76051b92d92ddd3c7", "e1021b2d33effbd5a4cc9b67e43aff74e10d2d3f444cdb9009e273ddf20a24b3")),
    ("P13", ("ab05c404ba6a7627ad618e87682cff949571b93dd335d33851d80c9c090eadfc", "7b416503377cd45a10285f6982a06a63af54d8e7c021b1d088a351cf40ab1a91")),
])
def test_existing_archived_p3_p13_compiled_objects_without_reserved_keys_are_unchanged(pattern, digests):
    # Captured from the 20261018T090344Z / 20261018T091814Z JanusGraph
    # archives (N=1000, seed=0), before this property-slot mapping change.
    case = prepare_case(None, pattern, 1000, 0, 0, database={"dbms": "janusgraph", "language": "gremlin", "verified_features": VERIFIED})
    for side, expected in zip(("a", "b"), digests):
        obj = {"text": case[f"query_{side}"], "columns": case["output_columns"][side],
               "required_features": sorted(case["required_features"][side]), "bindings": case["query_bindings"][side]}
        actual = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert actual == expected
