"""Opt-in S1--S10 semantics/interfaces for dedicated JanusGraph/HugeGraph.

Set GRAPHSYMPERF_GREMLIN_SCOR_TEST_CONFIG to an explicitly resettable test
configuration and GRAPHSYMPERF_GREMLIN_SCOR_TEST_OUTPUT to retain evidence.
Run the providers sequentially. The hand-built graph below is a semantic
oracle only; performance inputs remain owned by the existing generator.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from executor import CaseFailure, make_database, supported_patterns, typed_bag
from generator import PatternNotApplicable
from languages import generate, gremlin_property_key, logical_gremlin_property_key
from main import prepare_legacy_case as prepare_case, read_case, read_config, save_case
from query import parse_query
from test_arcade_gremlin_integration import _inspect_case, _write
from test_arcade_gremlin_scor_integration import (
    _COALESCE_CASES, _CONDITIONAL_CASES, _MEMBERSHIP_CASES,
)
from test_gremlin_semantics import _EDGES, _NODES


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_GREMLIN_SCOR_TEST_CONFIG"),
    reason="no dedicated JanusGraph/HugeGraph SCor interface configuration supplied")]

PATTERNS = tuple(f"S{i}" for i in range(1, 11))
# These existing ArcadeDB gate seeds cover literal/data-backed guards, all
# three S4 truth values, S5 alternatives, and S6 LIMIT cardinalities.
SEEDS = (0, 1, 2, 3, 6, 9, 12, 18, 27, 36, 54, 81)
COMPLEXITIES = (0, 1, 2)

_CONDITIONAL_VALUE_CASES = [
    ("selected_null", "CYPHER 25 WHEN true THEN { RETURN null AS value } ELSE { RETURN 9 AS value }",
     [{"value": None}]),
    ("selected_false", "CYPHER 25 WHEN true THEN { RETURN false AS value } ELSE { RETURN 9 AS value }",
     [{"value": False}]),
    ("selected_zero", "CYPHER 25 WHEN false THEN { RETURN 9 AS value } ELSE { RETURN 0 AS value }",
     [{"value": 0}]),
    ("selected_empty_string", "CYPHER 25 WHEN false THEN { RETURN 9 AS value } ELSE { RETURN '' AS value }",
     [{"value": ""}]),
    ("ordered_columns", "CYPHER 25 WHEN true THEN { RETURN 7 AS first, false AS second } "
     "ELSE { RETURN 9 AS first, true AS second }", [{"first": 7, "second": False}]),
    ("null_guard_complete_else_row", "CYPHER 25 WHEN null THEN { RETURN 7 AS first, true AS second } "
     "ELSE { RETURN null AS first, false AS second }", [{"first": None, "second": False}]),
    ("nullable_property_rows", "CYPHER 25 WHEN true THEN { MATCH (u:GspSem) "
     "RETURN u.uid AS uid, u.gsp_sem_flag AS flag } ELSE { RETURN 9 AS uid, false AS flag }",
     [{"uid": 1, "flag": True}, {"uid": 2, "flag": False}, {"uid": 3, "flag": None}, {"uid": 4, "flag": True}]),
]

_VERTICES = [{"uid": row["uid"], "labels": ["GspSem"], "properties": row} for row in _NODES]
_RELATIONSHIPS = [
    {"source": source, "target": target, "source_label": "GspSem", "target_label": "GspSem",
     "type": "GSP_SEM_LINK", "properties": {"gsp_sem_eid": eid}, "ordinal": ordinal}
    for ordinal, (source, target, eid) in enumerate(_EDGES)
]


class _SemanticGraph:
    indexes = [{"name": "gsp_cost_lookup", "label": "GspSem", "property": "uid"}]

    @staticmethod
    def schema():
        return {"nodes": [{"label": "GspSem", "properties": {
            "uid": "integer", "gsp_sem_flag": "boolean", "gsp_sem_n": "integer"}}],
            "relationships": [{"type": "GSP_SEM_LINK", "properties": {"gsp_sem_eid": "integer"},
                               "endpoints": [{"source_label": "GspSem", "target_label": "GspSem"}]}]}


# The complete three-valued truth tables are independent of the compiler.
# Falsey values in COALESCE are separately covered by the existing cases.
_BOOLEAN_CASES = [
    (f"{operator.lower()}_{left}_{right}", f"RETURN {left} {operator} {right} AS value", [{"value": expected}])
    for operator, rows in (
        ("OR", (("true", "true", True), ("true", "false", True), ("true", "null", True),
                ("false", "true", True), ("false", "false", False), ("false", "null", None),
                ("null", "true", True), ("null", "false", None), ("null", "null", None))),
        ("AND", (("true", "true", True), ("true", "false", False), ("true", "null", None),
                 ("false", "true", False), ("false", "false", False), ("false", "null", False),
                 ("null", "true", None), ("null", "false", False), ("null", "null", None))),
    )
    for left, right, expected in rows
]

_CASE_CASES = [
    ("null_selected_value", "RETURN CASE WHEN true THEN null ELSE 7 END AS value", [{"value": None}]),
    ("false_selected_value", "RETURN CASE WHEN true THEN false ELSE 7 END AS value", [{"value": False}]),
    ("zero_selected_value", "RETURN CASE WHEN true THEN 0 ELSE 7 END AS value", [{"value": 0}]),
    ("empty_string_selected_value", "RETURN CASE WHEN true THEN '' ELSE 7 END AS value", [{"value": ""}]),
    ("null_guard_selects_else", "RETURN CASE WHEN null THEN 7 ELSE 9 END AS value", [{"value": 9}]),
    ("property_guard_full_values", "MATCH (u:GspSem) RETURN u.uid AS uid, "
     "CASE WHEN u.gsp_sem_flag = true THEN COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } "
     "ELSE -1 END AS value",
     [{"uid": 1, "value": 3}, {"uid": 2, "value": -1}, {"uid": 3, "value": -1}, {"uid": 4, "value": 0}]),
]

_FLOW_CASES = [
    ("limit_zero", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value LIMIT 0", []),
    ("false_filter", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) WHERE false RETURN 1 AS value", []),
    ("null_filter", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) WHERE null RETURN 1 AS value", []),
    # Constant projections avoid depending on provider iteration order.
    ("positive_limit_one", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value LIMIT 1", [{"value": 1}]),
    ("positive_limit_three", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value LIMIT 3", [{"value": 1}] * 3),
    ("positive_limit_more_than_input", "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value LIMIT 9", [{"value": 1}] * 4),
    ("positive_limit_empty_input", "MATCH (u:GspSem {uid:999})-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value LIMIT 3", []),
    ("empty_upstream_suppresses_independent_join", "MATCH (e:GspSem) WHERE false "
     "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value", []),
    ("missing_upstream_suppresses_independent_join", "MATCH (e:GspSem {uid:999}) "
     "MATCH (u:GspSem)-[:GSP_SEM_LINK]->(v:GspSem) RETURN 1 AS value", []),
    ("semi_join_preserves_root_multiplicity", "MATCH (u:GspSem) "
     "WHERE EXISTS { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } RETURN u.uid AS uid", [{"uid": 1}, {"uid": 2}]),
    ("semi_join_missing_witness", "MATCH (u:GspSem) "
     "WHERE EXISTS { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem {uid:999}) } RETURN u.uid AS uid", []),
    ("anti_semi_join_empty_witness", "MATCH (u:GspSem) "
     "WHERE NOT EXISTS { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } RETURN u.uid AS uid", [{"uid": 3}, {"uid": 4}]),
]

_SHORT_CIRCUIT_CASES = [
    ("or_true", "MATCH (u:GspSem {uid:1}) RETURN true OR "
     "COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } > 0 AS value", [{"value": True}]),
    ("and_false", "MATCH (u:GspSem {uid:1}) RETURN false AND "
     "COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } > 0 AS value", [{"value": False}]),
    ("case_unselected_graph", "MATCH (u:GspSem {uid:1}) RETURN CASE WHEN true THEN 7 "
     "ELSE COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } END AS value", [{"value": 7}]),
    ("coalesce_nonnull_prefix", "MATCH (u:GspSem {uid:1}) RETURN coalesce(null, 0, "
     "COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) }) AS value", [{"value": 0}]),
]


def _failure(record, exc):
    record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    if isinstance(exc, CaseFailure):
        record["failure"] = {"status": exc.status, "reason": exc.reason,
                             "stop_target": exc.stop_target, "details": exc.details}


@pytest.fixture(scope="module")
def scor_config():
    config = read_config(Path(os.environ["GRAPHSYMPERF_GREMLIN_SCOR_TEST_CONFIG"]))
    assert config["database"]["dbms"] in {"janusgraph", "hugegraph"}
    assert config["database"]["language"] == "gremlin"
    assert config["database"]["dedicated"] is True and config["database"]["allow_reset"] is True
    assert set(PATTERNS) <= supported_patterns(config["database"])
    assert config["measurement"]["max_rows"] >= 1000
    return config


@pytest.fixture(scope="module")
def evidence_directory(tmp_path_factory):
    supplied = os.getenv("GRAPHSYMPERF_GREMLIN_SCOR_TEST_OUTPUT")
    output = Path(supplied) if supplied else tmp_path_factory.mktemp("gremlin-scor-interface")
    output.mkdir(parents=True, exist_ok=True)
    yield output
    entries = []
    capabilities = []
    for path in sorted(output.glob("*.json")):
        if path.name in {"semantic-fixture.json", "validation-summary.json"}:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if "status" in record:
            entries.append({"file": path.name, "status": record["status"], "reason": record.get("reason")})
        if record.get("kind") == "existing_pattern_interface":
            capabilities.append({"file": path.name,
                **{key: record.get(key) for key in ("status", "pattern", "seed", "data_seed", "complexity",
                    "scale", "reason", "failed_stage", "compiled_query_hashes")}})
    _write(output / "validation-summary.json", {
        "kind": "interface_and_semantics_only", "performance_screening": False,
        "planned_pattern_inputs": len(PATTERNS) * len(SEEDS) * len(COMPLEXITIES),
        "planned_native_property_mapping_cases": 1,
        "planned_semantic_cases": sum(map(len, (_CONDITIONAL_CASES, _MEMBERSHIP_CASES,
            _CONDITIONAL_VALUE_CASES, _COALESCE_CASES, _BOOLEAN_CASES, _CASE_CASES,
            _FLOW_CASES, _SHORT_CIRCUIT_CASES))),
        "counts": dict(Counter(item["status"] for item in entries)), "entries": entries,
        "pattern_capabilities": {
            "passed": [item for item in capabilities if item["status"] == "passed"],
            "unsupported_feature": [item for item in capabilities if item["status"] == "unsupported_feature"],
            "other": [item for item in capabilities if item["status"] not in {"passed", "unsupported_feature"}],
            "scope": "Observed inputs only; matching compiled query text is not a general provider capability claim",
        },
    })


@pytest.fixture(scope="module")
def semantic_graph(scor_config, evidence_directory):
    db = make_database(scor_config["database"], scor_config["measurement"])
    evidence = {"kind": "interface_oracle_only", "performance_screening": False, "status": "started",
                "version": db.version, "nodes": _VERTICES, "edges": _RELATIONSHIPS}
    try:
        assert db.health()
        db._reset()
        spec = _SemanticGraph()
        db._prepare_schema(spec)
        for rows, script in ((_VERTICES, db._vertex_script()), (_RELATIONSHIPS, db._edge_script())):
            values, _ = db._script(script, {"gsp_rows": rows})
            assert values == [len(rows)], values
        evidence["indexes"] = db._check_indexes(spec)
        values, _ = db._script("g.V().hasLabel('GspSem').valueMap().toList()", require_list=True)
        expected = [{key: [value] for key, value in row["properties"].items()} for row in _VERTICES]
        actual = [{key: value for key, value in row.items() if key != "_gsp_identity"} for row in values]
        assert typed_bag(actual) == typed_bag(expected), (actual, expected)
        evidence["loaded_vertices"] = values
        values, _ = db._script("g.E().project('source','target','type','eid')"
                              ".by(__.outV().values('uid')).by(__.inV().values('uid'))"
                              ".by(__.label()).by(__.values('gsp_sem_eid')).toList()", require_list=True)
        expected = [{"source": source, "target": target, "type": "GSP_SEM_LINK", "eid": eid}
                    for source, target, eid in _EDGES]
        assert typed_bag(values) == typed_bag(expected), (values, expected)
        evidence.update(loaded_edges=values, status="fixture_ready")
        _write(evidence_directory / "semantic-fixture.json", evidence)
        yield db
    except Exception as exc:
        _failure(evidence, exc)
        _write(evidence_directory / "semantic-fixture.json", evidence)
        raise
    finally:
        db.close()


def _profile_value(value):
    """Read only native metric wrappers; preserve the raw plan separately."""
    if isinstance(value, list):
        return [_profile_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"@type", "@value"}:
        kind, raw = value["@type"], value["@value"]
        if kind == "g:Map":
            assert isinstance(raw, list) and len(raw) % 2 == 0, value
            return {_profile_value(key): _profile_value(item) for key, item in zip(raw[::2], raw[1::2])}
        return _profile_value(raw)
    return {key: _profile_value(item) for key, item in value.items()}


def _profile_steps(value):
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and isinstance(value.get("counts"), dict):
            yield value
        for child in value.values():
            yield from _profile_steps(child)
    elif isinstance(value, list):
        for child in value:
            yield from _profile_steps(child)


def _unselected_work(profile, marker):
    matches = [step for step in _profile_steps(_profile_value(profile["plan"]))
               if step["name"].startswith(marker)]
    checked = []
    for step in matches:
        counts = step["counts"]
        exposed = {key: counts[key] for key in ("elementCount", "traverserCount") if key in counts}
        # A missing native counter does not establish zero work. Providers can
        # also omit optimized-away branches from PROFILE entirely.
        assert all(type(count) is int and count == 0 for count in exposed.values()), step
        checked.append({"name": step["name"], "id": step.get("id"), "counts": counts,
                        "status": "checked" if len(exposed) == 2 else "counts_not_exposed"})
    return {"marker": marker, "status": "checked" if checked and all(
        item["status"] == "checked" for item in checked) else "not_exposed", "steps": checked}


def _check_semantic_case(db, config, output, kind, name, text, expected, *, unselected_marker=None):
    target = config["database"]["dbms"]
    record = {"kind": kind, "status": "started", "dbms": target, "version": db.version,
              "source_cypher": text, "expected_rows": expected, "performance_screening": False}
    try:
        compiled = generate(parse_query(text), target, "gremlin",
                            verified_features=config["database"].get("verified_features", ()))
        record.update(gremlin=compiled.text, bindings=compiled.bindings, columns=compiled.columns)
        actual = db.execute(compiled.text, compiled.bindings, config["measurement"]["timeout_seconds"])
        record["execution"] = actual
        assert actual["complete"], actual
        columns = {source for source, _ in compiled.columns}
        assert all(set(row) == columns for row in actual["rows"]), actual
        record["logical_rows"] = compiled.normalize_rows(actual["rows"])
        assert typed_bag(record["logical_rows"]) == typed_bag(expected), record
        record["explain"] = db.explain(compiled.text, compiled.bindings, analyze=False)
        assert record["explain"].get("plan"), record
        record["profile"] = db.explain(compiled.text, compiled.bindings, analyze=True)
        assert record["profile"].get("plan"), record
        if unselected_marker:
            record["unselected_branch_work"] = _unselected_work(record["profile"], unselected_marker)
        record["status"] = "passed"
    except Exception as exc:
        _failure(record, exc)
        raise
    finally:
        _write(output / f"{kind}-{name}.json", record)


# Hand-built semantic cases precede Pattern tests which rebuild the graph.
@pytest.mark.parametrize("name,text,expected,marker", _CONDITIONAL_CASES,
                         ids=[case[0] for case in _CONDITIONAL_CASES])
def test_s3_conditional_query_semantics(semantic_graph, scor_config, evidence_directory,
                                        name, text, expected, marker):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "conditional", name,
                         text, expected, unselected_marker=marker)


@pytest.mark.parametrize("name,text,expected", _CONDITIONAL_VALUE_CASES,
                         ids=[case[0] for case in _CONDITIONAL_VALUE_CASES])
def test_s3_complete_selected_rows(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "conditional-value", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _MEMBERSHIP_CASES, ids=[case[0] for case in _MEMBERSHIP_CASES])
def test_s10_nonempty_null_safe_membership(semantic_graph, scor_config, evidence_directory, name, text, expected):
    assert expected, "An empty-only S10 oracle cannot establish NULL-safe membership semantics"
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "membership", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _COALESCE_CASES, ids=[case[0] for case in _COALESCE_CASES])
def test_s7_falsey_values_are_not_null(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "coalesce", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _BOOLEAN_CASES, ids=[case[0] for case in _BOOLEAN_CASES])
def test_s1_s2_boolean_truth_tables(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "boolean", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _CASE_CASES, ids=[case[0] for case in _CASE_CASES])
def test_s4_selected_values(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "case", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _FLOW_CASES, ids=[case[0] for case in _FLOW_CASES])
def test_s5_s6_s8_s9_row_flow(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "flow", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _SHORT_CIRCUIT_CASES,
                         ids=[case[0] for case in _SHORT_CIRCUIT_CASES])
def test_scalar_short_circuit(semantic_graph, scor_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, scor_config, evidence_directory, "short-circuit", name, text,
                         expected, unselected_marker="VertexStep(OUT,[GSP_SEM_LINK]")


_MAPPING_KEYS = ("key", "Key", "vertex", "edge", "element", "property", "label",
                 "_gsp_property_key", "_gsp_property_Key", "_gsp_property__gsp_property_key")
_MAPPING_KINDS = {key: "boolean" if key == "Key" else "integer" for key in _MAPPING_KEYS}
_MAPPING_VERTICES = [
    {"uid": 1, "labels": ["GspPropMap"], "properties": {"uid": 1,
        **{key: True if key == "Key" else number for number, key in enumerate(_MAPPING_KEYS, 11)}}},
    {"uid": 2, "labels": ["GspPropMap"], "properties": {"uid": 2, "key": 31, "Key": False}},
    {"uid": 3, "labels": ["GspPropMap"], "properties": {"uid": 3}},
]
_MAPPING_RELATIONSHIPS = [
    {"source": 1, "target": 2, "source_label": "GspPropMap", "target_label": "GspPropMap",
     "type": "GSP_PROP_MAP", "ordinal": 0, "properties": {"eid": 101,
         **{key: False if key == "Key" else number for number, key in enumerate(_MAPPING_KEYS, 41)}}},
    {"source": 1, "target": 2, "source_label": "GspPropMap", "target_label": "GspPropMap",
     "type": "GSP_PROP_MAP", "ordinal": 1, "properties": {"eid": 102, "key": 61, "Key": True}},
    {"source": 2, "target": 3, "source_label": "GspPropMap", "target_label": "GspPropMap",
     "type": "GSP_PROP_MAP", "ordinal": 2, "properties": {"eid": 103}},
]


class _PropertyMappingGraph:
    """Test-only native encoding oracle, never a performance graph recipe."""

    indexes = [{"name": "gsp_cost_lookup", "label": "GspPropMap", "property": key}
               for key in ("uid", "key")]

    @staticmethod
    def schema():
        return {"nodes": [{"label": "GspPropMap", "properties": {"uid": "integer", **_MAPPING_KINDS}}],
                "relationships": [{"type": "GSP_PROP_MAP", "properties": {"eid": "integer", **_MAPPING_KINDS},
                    "endpoints": [{"source_label": "GspPropMap", "target_label": "GspPropMap"}]}]}


_MAPPING_QUERIES = [
    ("vertex_properties", "MATCH (u:GspPropMap) RETURN u.uid AS uid, "
     + ", ".join(f"u.{key} AS {key}" for key in _MAPPING_KEYS),
     [{"uid": row["uid"], **{key: row["properties"].get(key) for key in _MAPPING_KEYS}}
      for row in _MAPPING_VERTICES]),
    ("relationship_properties", "MATCH (u:GspPropMap)-[r:GSP_PROP_MAP]->(v:GspPropMap) "
     "RETURN u.uid AS source, v.uid AS target, r.eid AS eid, "
     + ", ".join(f"r.{key} AS {key}" for key in _MAPPING_KEYS),
     [{"source": row["source"], "target": row["target"], "eid": row["properties"]["eid"],
       **{key: row["properties"].get(key) for key in _MAPPING_KEYS}} for row in _MAPPING_RELATIONSHIPS]),
    ("indexed_logical_key", "MATCH (u:GspPropMap {key:11}) RETURN u.uid AS uid", [{"uid": 1}]),
    ("relationship_logical_key_filter", "MATCH (u:GspPropMap {uid:1})-[r:GSP_PROP_MAP {key:41}]->(v:GspPropMap) "
     "RETURN r.key AS key, r.Key AS Key", [{"key": 41, "Key": False}]),
    ("missing_property_is_null", "MATCH (u:GspPropMap) WHERE u.key IS NULL "
     "RETURN u.uid AS uid, u.key AS key, u.Key AS Key", [{"uid": 3, "key": None, "Key": None}]),
    ("scalar_values_and_columns_stay_logical", "RETURN 'key' AS key, 'Key' AS Key, "
     "'_gsp_property_key' AS _gsp_property_key, 'label' AS label",
     [{"key": "key", "Key": "Key", "_gsp_property_key": "_gsp_property_key", "label": "label"}]),
]


def test_native_property_mapping_is_injective_and_logically_transparent(scor_config, evidence_directory):
    target = scor_config["database"]["dbms"]
    record = {"kind": "native_property_mapping_interface_oracle_only", "performance_screening": False,
              "status": "started", "dbms": target, "logical_nodes": _MAPPING_VERTICES,
              "logical_edges": _MAPPING_RELATIONSHIPS, "queries": {}}
    db = None
    try:
        mapping = {key: gremlin_property_key(key, target) for key in _MAPPING_KEYS}
        assert len(mapping) == len(set(mapping.values()))
        assert {logical_gremlin_property_key(value, target) for value in mapping.values()} == set(mapping)
        if target == "janusgraph":
            assert mapping["key"] == "_gsp_property_key"
            assert mapping["Key"] == "_gsp_property_Key"
            assert mapping["_gsp_property_key"] == "_gsp_property__gsp_property_key"
        else:
            assert all(key == value for key, value in mapping.items())
        record["property_name_mapping"] = mapping
        db = make_database(scor_config["database"], scor_config["measurement"])
        record["version"] = db.version
        assert db.health()
        db._reset()
        spec = _PropertyMappingGraph()
        db._prepare_schema(spec)
        for rows, script in ((_MAPPING_VERTICES, db._vertex_script()), (_MAPPING_RELATIONSHIPS, db._edge_script())):
            original = deepcopy(rows)
            values, _ = db._script(script, {"gsp_rows": rows})
            assert values == [len(rows)], values
            assert rows == original, "Native property encoding must not mutate the logical fixture"
        indexes = db._check_indexes(spec)
        assert len(indexes) == len(spec.indexes)
        for index, observed in zip(spec.indexes, indexes):
            assert observed["keys"] == [gremlin_property_key(index["property"], target)], observed
        record["indexes"] = indexes

        def logical_properties(properties, internal):
            observed = {key: value for key, value in properties.items() if key not in internal}
            logical = {logical_gremlin_property_key(key, target): value for key, value in observed.items()}
            assert len(logical) == len(observed), observed
            assert {gremlin_property_key(key, target): value for key, value in logical.items()} == observed
            return logical

        vertices, _ = db._script("g.V().hasLabel('GspPropMap').valueMap().toList()", require_list=True)
        expected = [{key: [value] for key, value in row["properties"].items()} for row in _MAPPING_VERTICES]
        assert typed_bag([logical_properties(row, {"_gsp_identity"}) for row in vertices]) == typed_bag(expected)
        record["loaded_physical_vertices"] = vertices
        edges, _ = db._script("g.E().project('source','target','properties')"
                              ".by(__.outV().values('uid')).by(__.inV().values('uid'))"
                              ".by(__.valueMap()).toList()", require_list=True)
        expected = [{"source": row["source"], "target": row["target"], "properties": row["properties"]}
                    for row in _MAPPING_RELATIONSHIPS]
        actual = [{**row, "properties": logical_properties(row["properties"], {"_gsp_edge_ordinal"})}
                  for row in edges]
        assert typed_bag(actual) == typed_bag(expected), (actual, expected)
        record["loaded_physical_edges"] = edges

        for name, source, expected in _MAPPING_QUERIES:
            compiled = generate(parse_query(source), target, "gremlin",
                                verified_features=scor_config["database"].get("verified_features", ()))
            query_record = {"source_cypher": source, "gremlin": compiled.text, "bindings": compiled.bindings,
                            "columns": compiled.columns, "expected_rows": expected}
            record["queries"][name] = query_record
            actual = db.execute(compiled.text, compiled.bindings, scor_config["measurement"]["timeout_seconds"])
            query_record["execution"] = actual
            assert actual["complete"], actual
            assert all(set(row) == {physical for physical, _ in compiled.columns} for row in actual["rows"]), actual
            query_record["logical_rows"] = compiled.normalize_rows(actual["rows"])
            assert typed_bag(query_record["logical_rows"]) == typed_bag(expected), query_record
            query_record["explain"] = db.explain(compiled.text, compiled.bindings, analyze=False)
            query_record["profile"] = db.explain(compiled.text, compiled.bindings, analyze=True)
            assert query_record["explain"].get("plan") and query_record["profile"].get("plan"), query_record
        record["status"] = "passed"
    except Exception as exc:
        _failure(record, exc)
        raise
    finally:
        _write(evidence_directory / "native-property-mapping.json", record)
        if db is not None:
            db.close()


_HUGEGRAPH_UNINDEXED_ROOT_MESSAGE = (
    "Don't accept query based on properties [uid] that are not indexed in label 'ScorRoot', "
    "may not match secondary condition"
)


def _is_known_hugegraph_unindexed_root(target, exc):
    # This opt-out is narrower than the HTTP classification: only the exact
    # observed restriction on the unchanged ScorRoot.uid traversal is allowed.
    return (target == "hugegraph" and isinstance(exc, CaseFailure)
            and exc.status == "unsupported_feature" and exc.stop_target is False
            and exc.details.get("server_message") == _HUGEGRAPH_UNINDEXED_ROOT_MESSAGE)


def _compiled_query_hashes(case):
    result = {}
    for side in ("a", "b", "ref"):
        if not case.get(f"query_{side}"):
            continue
        query = case[f"query_{side}"]
        material = {"query": query,
                    "bindings": {**case.get("parameters", {}), **case.get("query_bindings", {}).get(side, {})},
                    "columns": case["output_columns"][side]}
        encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        result[side] = {"text_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                        "program_sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def _check_pattern_interface(config, output, pattern, seed, complexity):
    target = config["database"]["dbms"]
    name = f"pattern-{pattern}-s{seed}-c{complexity}"
    record = {"kind": "existing_pattern_interface", "performance_screening": False,
              "status": "started", "pattern": pattern, "dbms": target, "scale": 3,
              "seed": seed, "data_seed": 0, "complexity": complexity, "stages": {}}
    db = None
    stage = "prepare_case"
    try:
        case = prepare_case(None, pattern, 3, seed, 0, complexity, database=config["database"])
        record["case"] = case
        assert not case.get("compile_error"), case.get("compile_error")
        assert "expected_rows" in case, "The generated Pattern must retain its independent result oracle"
        record["compiled_query_hashes"] = _compiled_query_hashes(case)
        stage = "initial_connection"
        db = make_database(config["database"], config["measurement"])
        record["version"] = db.version
        assert db.health()
        stage = "initial_build.restore"
        db.restore(case)
        initial = record["stages"].setdefault("initial_build", {})
        stage = "initial_build.check_state_execute_profile"
        baseline = _inspect_case(db, case, config, initial, profile=True)
        initial["explain"] = {}
        for side in ("a", "b", "ref"):
            if case.get(f"query_{side}"):
                parameters = {**case.get("parameters", {}), **case.get("query_bindings", {}).get(side, {})}
                stage = f"initial_build.explain.{side}"
                plan = db.explain(case[f"query_{side}"], parameters, analyze=False)
                assert plan.get("plan"), plan
                initial["explain"][side] = plan
        db.close()
        stage = "new_connection.open"
        db = make_database(config["database"], config["measurement"])
        assert db.version == record["version"]
        stage = "new_connection.check_state_execute"
        reopened = _inspect_case(db, case, config, record["stages"].setdefault("new_connection", {}), profile=False)
        assert reopened == baseline
        stage = "second_build.restore"
        db.restore(case)
        stage = "second_build.check_state_execute_profile"
        rebuilt = _inspect_case(db, case, config, record["stages"].setdefault("second_build", {}), profile=True)
        assert rebuilt == baseline
        stage = "archive_roundtrip"
        archive = output / "archives" / name
        save_case(archive, case, config["measurement"], {
            "status": "interface_checked", "dbms": target, "language": "gremlin",
            "database_version": db.version, "performance_screening": False,
        })
        restored, settings = read_case(archive)
        assert restored == case and settings == config["measurement"]
        record.update(status="passed", archive_roundtrip=True)
    except PatternNotApplicable as exc:
        record.update(status="not_applicable", reason=str(exc))
        pytest.skip(str(exc))
    except Exception as exc:
        record["failed_stage"] = stage
        _failure(record, exc)
        if _is_known_hugegraph_unindexed_root(target, exc):
            record.update(status="unsupported_feature", reason=exc.reason)
            pytest.skip(f"unchanged HugeGraph traversal is unsupported: {exc.details['server_message']}")
        raise
    finally:
        _write(output / f"{name}.json", record)
        if db is not None:
            db.close()


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("complexity", COMPLEXITIES)
def test_existing_scor_pattern_interfaces(scor_config, evidence_directory, pattern, seed, complexity):
    _check_pattern_interface(scor_config, evidence_directory, pattern, seed, complexity)
