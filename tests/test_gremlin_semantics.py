"""Opt-in result semantics on a dedicated JanusGraph and optional Neo4j.

Set GRAPHSYMPERF_GREMLIN_TEST_CONFIG_DIR to a directory containing
janusgraph.yaml. An optional GRAPHSYMPERF_GREMLIN_NEO4J_CONFIG enables a real
Cypher baseline. GRAPHSYMPERF_GREMLIN_TEST_OUTPUT preserves native GraphSON
and normalized rows outside pytest's temporary directory. These are semantic
tests, not performance measurements or optimization-bug detection.
"""

import json
import os
from pathlib import Path

import pytest

from executor import _gremlin_data, make_database, typed_bag
from gremlin import generate_gremlin
from main import read_config
from query import parse_query


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_GREMLIN_TEST_CONFIG_DIR"),
    reason="no dedicated Gremlin semantic-test configuration supplied")]

_NODES = [
    {"uid": 1, "gsp_sem_flag": True, "gsp_sem_n": 1},
    {"uid": 2, "gsp_sem_flag": False, "gsp_sem_n": 1},
    {"uid": 3, "gsp_sem_n": 1},
    {"uid": 4, "gsp_sem_flag": True, "gsp_sem_n": 1},
]
_EDGES = [(1, 1, 101), (1, 2, 102), (1, 2, 103), (2, 1, 104)]

# The self-loop quantity below is an explicit fixture expectation; agreement
# with Cypher is claimed only when the optional Neo4j baseline actually runs.
_CASES = [
    ("undirected_self_loop_parallel", "MATCH (u:GspSem {uid:1})-[:GSP_SEM_LINK]-(v:GspSem) RETURN u.uid, v.uid",
     [{"u.uid": 1, "v.uid": 1}] + [{"u.uid": 1, "v.uid": 2}] * 3),
    ("reused_relationship_variable", "MATCH (u:GspSem {uid:1})-[r:GSP_SEM_LINK]->(v:GspSem) MATCH (u)-[r:GSP_SEM_LINK]->(v) RETURN u.uid, v.uid, r.gsp_sem_eid",
     [{"u.uid": 1, "v.uid": 1, "r.gsp_sem_eid": 101},
      {"u.uid": 1, "v.uid": 2, "r.gsp_sem_eid": 102},
      {"u.uid": 1, "v.uid": 2, "r.gsp_sem_eid": 103}]),
    ("different_match_relationship_reuse", "MATCH (u:GspSem {uid:1})-[r:GSP_SEM_LINK]->(v:GspSem) MATCH (u)-[s:GSP_SEM_LINK]->(v) RETURN u.uid, v.uid",
     [{"u.uid": 1, "v.uid": 1}] + [{"u.uid": 1, "v.uid": 2}] * 4),
    ("not_missing", "MATCH (u:GspSem) WHERE NOT (u.gsp_sem_flag = true) RETURN u.uid", [{"u.uid": 2}]),
    ("not_false_missing", "MATCH (u:GspSem) WHERE NOT (u.gsp_sem_flag = false) RETURN u.uid", [{"u.uid": 1}, {"u.uid": 4}]),
    ("count_empty", "MATCH (u:GspSem) RETURN u.uid, COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } AS n",
     [{"u.uid": 1, "n": 3}, {"u.uid": 2, "n": 1}, {"u.uid": 3, "n": 0}, {"u.uid": 4, "n": 0}]),
    ("count_zero_filter", "MATCH (u:GspSem) WHERE COUNT { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } = 0 RETURN u.uid", [{"u.uid": 3}, {"u.uid": 4}]),
    ("exists_empty", "MATCH (u:GspSem) RETURN u.uid, EXISTS { MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) } AS yes",
     [{"u.uid": 1, "yes": True}, {"u.uid": 2, "yes": True}, {"u.uid": 3, "yes": False}, {"u.uid": 4, "yes": False}]),
    ("union_duplicates", "RETURN 1 AS x UNION ALL RETURN 2 AS x UNION ALL RETURN 1 AS x", [{"x": 1}, {"x": 2}, {"x": 1}]),
    ("is_null", "MATCH (u:GspSem) WHERE u.gsp_sem_flag IS NULL RETURN u.uid", [{"u.uid": 3}]),
    ("null_property_map_not_is_null", "MATCH (u:GspSem {gsp_sem_flag:null}) RETURN u.uid", []),
    ("is_not_null", "MATCH (u:GspSem) WHERE u.gsp_sem_flag IS NOT NULL RETURN u.uid", [{"u.uid": 1}, {"u.uid": 2}, {"u.uid": 4}]),
    ("nullable_projection", "MATCH (u:GspSem) RETURN u.uid, u.gsp_sem_flag",
     [{"u.uid": 1, "u.gsp_sem_flag": True}, {"u.uid": 2, "u.gsp_sem_flag": False},
      {"u.uid": 3, "u.gsp_sem_flag": None}, {"u.uid": 4, "u.gsp_sem_flag": True}]),
    ("case_unknown_else", "MATCH (u:GspSem) RETURN u.uid, CASE WHEN u.gsp_sem_flag = true THEN 7 ELSE 3 END AS value",
     [{"u.uid": 1, "value": 7}, {"u.uid": 2, "value": 3}, {"u.uid": 3, "value": 3}, {"u.uid": 4, "value": 7}]),
    ("case_literal_null", "RETURN CASE WHEN null THEN 7 ELSE 3 END AS value", [{"value": 3}]),
    ("boolean_unknown_projection", "MATCH (u:GspSem) RETURN u.uid, (u.gsp_sem_flag = true) AS value",
     [{"u.uid": 1, "value": True}, {"u.uid": 2, "value": False}, {"u.uid": 3, "value": None}, {"u.uid": 4, "value": True}]),
    ("coalesce_missing", "MATCH (u:GspSem) WHERE coalesce(u.gsp_sem_flag, false) = true RETURN u.uid", [{"u.uid": 1}, {"u.uid": 4}]),
    ("coalesce_preserves_first_scalar_type", "RETURN coalesce(null, true, 1) AS value", [{"value": True}]),
    ("or_unknown_true", "MATCH (u:GspSem) WHERE u.gsp_sem_flag = true OR u.uid = 3 RETURN u.uid", [{"u.uid": 1}, {"u.uid": 3}, {"u.uid": 4}]),
    ("not_or_unknown", "MATCH (u:GspSem) WHERE NOT (u.gsp_sem_flag = true OR u.uid = 2) RETURN u.uid", []),
    ("integer_not_boolean", "MATCH (u:GspSem) WHERE u.gsp_sem_n = true RETURN u.uid", []),
    ("boolean_not_integer", "MATCH (u:GspSem) WHERE u.gsp_sem_flag = 1 RETURN u.uid", []),
    ("literal_types", "RETURN true AS flag, 1 AS n, 1.5 AS real", [{"flag": True, "n": 1, "real": 1.5}]),
    ("literal_comparison_types", "RETURN (true = 1) AS value", [{"value": False}]),
    ("numeric_value_equality", "RETURN (1 = 1.0) AS value", [{"value": True}]),
    ("signed_integer_limits", "RETURN -9223372036854775808 AS minimum, 9223372036854775807 AS maximum",
     [{"minimum": -(2**63), "maximum": 2**63 - 1}]),
    ("null_comparison", "RETURN (null = false) AS value", [{"value": None}]),
    ("property_property_missing", "MATCH (u:GspSem) WHERE u.gsp_sem_flag = u.gsp_sem_flag RETURN u.uid", [{"u.uid": 1}, {"u.uid": 2}, {"u.uid": 4}]),
    ("internal_label_names_do_not_capture_variables", "MATCH (_gsp_v0:GspSem {uid:1})-[:GSP_SEM_LINK]->(_gsp_tmp0:GspSem) RETURN _gsp_v0.uid, _gsp_tmp0.uid",
     [{"_gsp_v0.uid": 1, "_gsp_tmp0.uid": 1}] + [{"_gsp_v0.uid": 1, "_gsp_tmp0.uid": 2}] * 2),
]


def _write(directory, name, value):
    (directory / name).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def semantic_graph(tmp_path_factory):
    directory = Path(os.environ["GRAPHSYMPERF_GREMLIN_TEST_CONFIG_DIR"])
    config = read_config(directory / "janusgraph.yaml")
    assert config["database"]["dbms"] == "janusgraph"
    db = make_database(config["database"], config["measurement"])
    output = Path(os.getenv("GRAPHSYMPERF_GREMLIN_TEST_OUTPUT") or tmp_path_factory.mktemp("gremlin-semantics"))
    output.mkdir(parents=True, exist_ok=True)
    neo = None
    try:
        schema = ("_gsp_graph.tx().rollback(); def m=_gsp_graph.openManagement(); "
                  "if(m.getVertexLabel('GspSem')==null) m.makeVertexLabel('GspSem').make(); "
                  "if(m.getEdgeLabel('GSP_SEM_LINK')==null) m.makeEdgeLabel('GSP_SEM_LINK').multiplicity(org.janusgraph.core.Multiplicity.MULTI).make(); "
                  "if(m.getPropertyKey('uid')==null) m.makePropertyKey('uid').dataType(Long.class).cardinality(org.janusgraph.core.Cardinality.SINGLE).make(); "
                  "if(m.getPropertyKey('gsp_sem_flag')==null) m.makePropertyKey('gsp_sem_flag').dataType(Boolean.class).cardinality(org.janusgraph.core.Cardinality.SINGLE).make(); "
                  "['gsp_sem_n','gsp_sem_eid'].each{ key -> if(m.getPropertyKey(key)==null) m.makePropertyKey(key).dataType(Long.class).cardinality(org.janusgraph.core.Cardinality.SINGLE).make() }; "
                  "m.commit(); true")
        db._script(schema)
        setup = ("g.V().hasLabel('GspSem').drop().iterate(); "
                 "gsp_nodes.each{ row -> def v=g.addV('GspSem').next(); row.each{ key,value -> v.property(key, value instanceof Number ? value.longValue() : value) } }; "
                 "gsp_edges.each{ row -> def a=g.V().hasLabel('GspSem').has('uid',row[0].longValue()).next(); "
                 "def b=g.V().hasLabel('GspSem').has('uid',row[1].longValue()).next(); a.addEdge('GSP_SEM_LINK',b,'gsp_sem_eid',row[2].longValue()) }; "
                 "g.V().hasLabel('GspSem').count().next()")
        envelope, _ = db._request(setup, {"gsp_nodes": _NODES, "gsp_edges": _EDGES}, config["measurement"]["timeout_seconds"])
        assert _gremlin_data(envelope, require_list=False) == [4]
        _write(output, "fixture-graphson.json", envelope)
        baseline_path = os.getenv("GRAPHSYMPERF_GREMLIN_NEO4J_CONFIG")
        if baseline_path:
            neo_config = read_config(Path(baseline_path))
            assert neo_config["database"]["dbms"] == "neo4j"
            neo = make_database(neo_config["database"], neo_config["measurement"])
            neo.execute("MATCH (u:GspSem) DETACH DELETE u", {}, 30)
            neo.execute("UNWIND $rows AS row CREATE (u:GspSem) SET u = row", {"rows": _NODES}, 30)
            neo.execute("UNWIND $rows AS row MATCH (u:GspSem {uid: row[0]}), (v:GspSem {uid: row[1]}) CREATE (u)-[:GSP_SEM_LINK {gsp_sem_eid: row[2]}]->(v)", {"rows": _EDGES}, 30)
        _write(output, "environment.json", {"gremlin_dbms": config["database"]["dbms"], "gremlin_version": db.version,
                                             "cypher_baseline_version": neo.version if neo else None,
                                             "nodes": _NODES, "edges": _EDGES})
        yield db, neo, output
    finally:
        db._script("g.V().hasLabel('GspSem').drop().iterate(); true")
        db.close()
        if neo is not None:
            neo.execute("MATCH (u:GspSem) DETACH DELETE u", {}, 30)
            neo.close()


@pytest.mark.parametrize("name,text,expected", _CASES, ids=[case[0] for case in _CASES])
def test_actual_typed_result_bag(semantic_graph, name, text, expected):
    db, neo, output = semantic_graph
    compiled = generate_gremlin(parse_query(text), "janusgraph", verified_features=("gremlin_null_projection",))
    envelope, _ = db._request(f"({compiled.text}).limit(1001).toList()", compiled.bindings, 30)
    physical = _gremlin_data(envelope, require_list=True, max_items=1001)
    actual = compiled.normalize_rows(physical)
    evidence = {"source_cypher": text, "gremlin": compiled.text, "bindings": compiled.bindings,
                "native_graphson": envelope, "physical_rows": physical, "logical_rows": actual,
                "expected_rows": expected, "cypher_baseline_rows": None}
    _write(output, name + ".json", evidence)
    if neo is not None:
        baseline = neo.execute(text, {}, 30)
        assert baseline["complete"]
        evidence["cypher_baseline_rows"] = baseline["rows"]
        _write(output, name + ".json", evidence)
    assert typed_bag(actual) == typed_bag(expected), evidence
    if neo is not None:
        assert typed_bag(actual) == typed_bag(evidence["cypher_baseline_rows"]), evidence
