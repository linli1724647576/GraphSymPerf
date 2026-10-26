"""P3's native HugeGraph parent/sub label mapping is loading-only, not an IR rewrite."""

from copy import deepcopy
import pytest

from executor import ApacheHugeGraph, CaseFailure, _gremlin_state_checks, supported_patterns
from state import GraphSpec


def adapter():
    db = ApacheHugeGraph.__new__(ApacheHugeGraph)
    db.settings = {"timeout_seconds": 2.0, "max_rows": 1000}
    return db


def schema_response(spec, parents):
    rows = []
    for logical, children in parents.items():
        common = {"sort_keys": ["_gsp_edge_ordinal"], "properties": ["_gsp_edge_ordinal"], "nullable_keys": [],
                  "multiple": True, "status": "CREATED"}
        links = [{key: item[key] for key in ("source_label", "target_label")} for item in children]
        rows.append({**common, "name": logical, "type": "PARENT", "parent": None, "links": links})
        rows.extend({**common, "name": child["label"], "type": "SUB", "parent": logical,
                     "links": [link]} for child, link in zip(children, links))
    total = spec.counts()["edges"]
    return [{"supported": True, "labels": rows}], [{
        "total": total, "distinct_ordinals": total, "invalid": 0,
        "min_ordinal": 0, "max_ordinal": total - 1,
    }]


@pytest.mark.parametrize("variant,children", [("original", 2), ("pruning_path", 4)])
def test_p3_mapping_keeps_logical_schema_and_every_endpoint_pair(variant, children):
    db = adapter()
    spec = GraphSpec("P3", 3, 17, state_variant=variant)
    original = deepcopy(spec.schema())
    assert db._recipe({"graph_spec": spec.to_dict()}) == spec
    mapping = db._parent_edge_labels(spec)
    assert set(mapping) == {"GSP_LINK"}
    assert len(mapping["GSP_LINK"]) == children
    assert len({item["label"] for item in mapping["GSP_LINK"]}) == children
    assert all(item["label"].startswith("GSP_LINK__gsp_") for item in mapping["GSP_LINK"])
    assert {(item["source_label"], item["target_label"]) for item in mapping["GSP_LINK"]} == {
        (item["source_label"], item["target_label"]) for item in original["relationships"][0]["endpoints"]
    }
    assert spec.schema() == original
    assert db._parent_edge_labels(GraphSpec("P3", 30, 5, state_variant=variant)) == mapping


def test_other_multi_endpoint_patterns_remain_rejected_before_reset():
    db = adapter()
    db._reset = lambda: pytest.fail("unsupported recipe must not reset data")
    with pytest.raises(CaseFailure, match="hugegraph_multi_endpoint_relationship_type"):
        db.restore({"graph_spec": GraphSpec("P12", 3, 0).to_dict()})
    with pytest.raises(CaseFailure, match="hugegraph_multi_endpoint_relationship_type"):
        db._parent_edge_labels(GraphSpec("P12", 3, 0))


def test_schema_uses_native_parent_children_and_cleans_children_before_parent():
    db, calls = adapter(), []
    db._script = lambda text: (calls.append(text) or ([True], 1.0))
    db._prepare_schema(GraphSpec("P3", 3, 0))
    assert len(calls) == 2
    reset, create = calls
    assert reset.index("findAll{ !it.isFather() }") < reset.index("findAll{ it.isFather() }")
    assert "unexpected edge schema" in reset and "waitUntilTaskCompleted" in reset
    assert 's.edgeLabel("GSP_LINK").asBase().multiTimes()' in create
    assert create.count('.withBase("GSP_LINK")') == 2
    assert create.count("sortKeys('_gsp_edge_ordinal')") == 3
    assert "supportsFatherAndSubEdgeLabel()" in create
    load = db._edge_script()
    assert "label=mapping[3]" in load and "a.addEdge(label,b,props.toArray())" in load
    assert "r.ordinal.longValue()" in load
    assert "missing physical edge label" in load
    assert "r.type=" not in load


def test_new_single_endpoint_schema_clears_previous_mapping():
    db = adapter()
    db._script = lambda text: ([True], 1.0)
    db._prepare_schema(GraphSpec("P3", 3, 0))
    assert "mapping" in db._edge_script()
    db._prepare_schema(GraphSpec("P1", 3, 0))
    assert db._hugegraph_parent_edges == {}
    assert "mapping" not in db._edge_script()


def test_parent_population_checks_scan_physical_labels_and_keep_expected_bag():
    spec = GraphSpec("P3", 3, 0, state_variant="pruning_path")
    groups = {logical: [child["label"] for child in children]
              for logical, children in adapter()._parent_edge_labels(spec).items()}
    old = _gremlin_state_checks(spec.state_checks(), scan_edges=True)
    new = _gremlin_state_checks(spec.state_checks(), scan_edges=True, edge_label_groups=groups)
    assert [row["expected_rows"] for row in old] == [row["expected_rows"] for row in new]
    edge_queries = [row["query"] for row in new if row["query"].startswith("g.E()")]
    assert edge_queries
    assert all(".contains(t.get().label())" in query for query in edge_queries)
    assert all('label() == "GSP_LINK"' not in query for query in edge_queries)
    assert any('outV().hasLabel("GspBad")' in query for query in edge_queries)


def test_parent_schema_and_identity_are_recorded_with_raw_label_boundary():
    db, spec = adapter(), GraphSpec("P3", 3, 0)
    parents = db._parent_edge_labels(spec)
    responses = iter(schema_response(spec, parents))
    calls = []
    db._script = lambda text: (calls.append(text) or (next(responses), 1.0))
    evidence = db._check_parent_edge_schema(spec, parents)
    assert evidence["mapping"] == parents
    assert evidence["identity"]["total"] == 8
    assert "raw edge.label()" in evidence["scope"]
    assert "g.E().each" in calls[1] and "p.value() instanceof Long" in calls[1]
    assert "seen.add(n)" in calls[1]


@pytest.mark.parametrize("field,value", [
    ("parent", "Wrong"), ("type", "NORMAL"),
    ("links", [{"source_label": "GspHigh", "target_label": "GspLow"}]),
    ("sort_keys", []), ("multiple", False), ("status", "CREATING"),
    ("properties", []),
    ("nullable_keys", ["_gsp_edge_ordinal"]),
])
def test_wrong_physical_schema_cannot_pass_gate(field, value):
    db, spec = adapter(), GraphSpec("P3", 3, 0)
    parents = db._parent_edge_labels(spec)
    schemas, counts = schema_response(spec, parents)
    schemas[0]["labels"][1][field] = value
    responses = iter([schemas, counts])
    db._script = lambda text: (next(responses), 1.0)
    with pytest.raises(CaseFailure, match="hugegraph_parent_edge_schema_mismatch"):
        db._check_parent_edge_schema(spec, parents)


@pytest.mark.parametrize("field,value", [
    ("total", 7), ("distinct_ordinals", 7), ("invalid", 1),
    ("min_ordinal", 1), ("max_ordinal", 8),
])
def test_missing_duplicate_unmapped_or_shifted_edge_identity_fails(field, value):
    db, spec = adapter(), GraphSpec("P3", 3, 0)
    parents = db._parent_edge_labels(spec)
    schemas, counts = schema_response(spec, parents)
    counts[0][field] = value
    responses = iter([schemas, counts])
    db._script = lambda text: (next(responses), 1.0)
    with pytest.raises(CaseFailure, match="hugegraph_parent_edge_identity_mismatch"):
        db._check_parent_edge_schema(spec, parents)


def test_support_declaration_is_provider_scoped():
    for provider in ("janusgraph", "hugegraph"):
        available = supported_patterns({"dbms": provider, "language": "gremlin"})
        assert {"P3", "P13", "P16"} <= available
        assert "P7" not in available
    assert "P12" not in supported_patterns({"dbms": "hugegraph", "language": "gremlin"})
    arcade = supported_patterns({"dbms": "arcadedb", "language": "gremlin"})
    assert not {"P3", "P7", "P13", "P16"} & arcade
    assert {f"S{i}" for i in range(1, 11)} <= arcade
