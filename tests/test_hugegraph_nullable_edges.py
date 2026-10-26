"""HugeGraph user edge properties remain optional; physical identity does not."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from executor import ApacheHugeGraph, CaseFailure
from state import GraphSpec


def fixture(parent=False, user_properties=True):
    endpoints = [{"source_label": "A", "target_label": "B"}]
    if parent:
        endpoints.append({"source_label": "B", "target_label": "A"})
    schema = {"nodes": [{"label": label, "properties": {"uid": "integer"}} for label in ("A", "B")],
              "relationships": [{"type": "GSP_NULLABLE", "properties": {"key": "integer", "flag": "boolean"} if user_properties else {},
                                 "endpoints": endpoints}]}
    return SimpleNamespace(pattern="P3" if parent else "S1", schema=lambda: schema, indexes=[], counts=lambda: {"edges": 3})


@pytest.mark.parametrize("parent", [False, True])
def test_schema_declares_only_user_properties_nullable_on_every_physical_label(parent):
    spec, calls = fixture(parent), []
    before = deepcopy(spec.schema())
    db = ApacheHugeGraph.__new__(ApacheHugeGraph)
    db.settings = {"timeout_seconds": 2}
    db._script = lambda text: (calls.append(text) or [True], 1.0)
    db._prepare_schema(spec)
    creates = [statement for statement in calls[1].split("; ") if statement.startswith("s.edgeLabel(")]
    assert len(creates) == (3 if parent else 1)
    for text in creates:
        assert '.properties("key", "flag", "_gsp_edge_ordinal")' in text
        assert '.nullableKeys("key", "flag")' in text
        assert ".sortKeys('_gsp_edge_ordinal')" in text
    assert spec.schema() == before


@pytest.mark.parametrize("spec", [GraphSpec("S1", 3, 0), GraphSpec("P3", 3, 0)])
def test_existing_property_free_edge_schema_keeps_the_original_create_chain(spec):
    calls = []
    db = ApacheHugeGraph.__new__(ApacheHugeGraph)
    db.settings = {"timeout_seconds": 2}
    db._script = lambda text: (calls.append(text) or [True], 1.0)
    db._prepare_schema(spec)
    creates = [statement for statement in calls[1].split("; ") if statement.startswith("s.edgeLabel(")]
    assert creates
    for text in creates:
        assert '.properties("_gsp_edge_ordinal").sortKeys' in text
        assert ".nullableKeys(" not in text


@pytest.mark.parametrize("nullable,passed", [(["flag", "key"], True), (["key"], False),
                                            (["flag", "key", "_gsp_edge_ordinal"], False)])
def test_parent_and_child_schema_check_validates_the_exact_nullable_user_set(nullable, passed):
    db, spec = ApacheHugeGraph.__new__(ApacheHugeGraph), fixture(parent=True)
    parents = db._parent_edge_labels(spec)
    logical, children = next(iter(parents.items()))
    common = {"sort_keys": ["_gsp_edge_ordinal"], "properties": ["_gsp_edge_ordinal", "key", "flag"],
              "nullable_keys": ["key", "flag"], "multiple": True, "status": "CREATED"}
    links = [{key: child[key] for key in ("source_label", "target_label")} for child in children]
    rows = [{**common, "name": logical, "type": "PARENT", "parent": None, "links": links}]
    rows += [{**common, "name": child["label"], "type": "SUB", "parent": logical, "links": [link]}
             for child, link in zip(children, links)]
    rows[1]["nullable_keys"] = nullable
    responses = iter([[{"supported": True, "labels": rows}], [{"total": 3, "distinct_ordinals": 3,
        "invalid": 0, "min_ordinal": 0, "max_ordinal": 2}]])
    calls = []
    db._script = lambda text: (calls.append(text) or next(responses), 1.0)
    if passed:
        evidence = db._check_parent_edge_schema(spec, parents)
        assert all(row["nullable_keys"] == ["flag", "key"] for row in evidence["labels"])
        assert "e.nullableKeys()" in calls[0]
    else:
        with pytest.raises(CaseFailure, match="hugegraph_parent_edge_schema_mismatch"):
            db._check_parent_edge_schema(spec, parents)
