"""Opt-in P3/P13/P16 semantics and interfaces on a dedicated Gremlin target.

GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_CONFIG selects one JanusGraph/HugeGraph
configuration; it must explicitly permit reset of a dedicated graph. Set
GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_OUTPUT to retain evidence. Run the targets
sequentially. The small hand-built graph is an interface oracle only: it is
never fed to the performance runner or counted as a new Pattern.
"""

from collections import Counter
import json
import os
from pathlib import Path

import pytest

from executor import CaseFailure, make_database, supported_patterns, typed_bag
from generator import PatternNotApplicable
from languages import UnsupportedFeature, generate
from main import prepare_case, read_case, read_config, save_case
from query import parse_query
from test_arcade_gremlin_integration import _inspect_case, _write


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_CONFIG"),
    reason="no dedicated extended Gremlin interface configuration supplied")]

PATTERNS = ("P3", "P13", "P16")
SEEDS = (0, 3, 17)
COMPLEXITIES = (0, 1, 2)
STATE_VARIANTS = (("P3", "pruning_path"), ("P13", "multi_low"))

_FLAGS = {1: True, 2: True, 3: None, 4: False, 5: True, 6: False, 7: None, 8: True}
_NODES = [{"uid": uid, "labels": ["GspExt"], "properties": {
    "uid": uid, "gsp_ext_anchor": uid <= 4,
    **({"gsp_ext_flag": flag} if flag is not None else {}),
}} for uid, flag in _FLAGS.items()]
_TRAIL_EDGES = ((1, 1, 101), (1, 2, 102), (1, 2, 103), (2, 1, 104),
                (2, 3, 105), (3, 1, 106), (4, 4, 107), (3, 4, 108))
_LOW_EDGES = ((1, 5, 201), (1, 5, 202), (2, 5, 203))
_HIGH_EDGES = ((1, 8, 301), (1, 8, 302), (1, 6, 303), (2, 6, 304), (3, 8, 305))
_EDGES = [{"source": source, "target": target, "source_label": "GspExt", "target_label": "GspExt",
           "type": kind, "properties": {"gsp_ext_eid": eid}, "ordinal": ordinal}
          for ordinal, (kind, source, target, eid) in enumerate(
              (kind, *edge) for kind, edges in (("GSP_EXT_TRAIL", _TRAIL_EDGES),
                                               ("GSP_EXT_LOW", _LOW_EDGES),
                                               ("GSP_EXT_HIGH", _HIGH_EDGES)) for edge in edges)]


class _SemanticGraph:
    """A test-only schema, reusing the providers' native schema and loaders."""

    indexes = [{"name": "gsp_cost_lookup", "label": "GspExt", "property": "uid"}]

    @staticmethod
    def schema():
        return {"nodes": [{"label": "GspExt", "properties": {
            "uid": "integer", "gsp_ext_anchor": "boolean", "gsp_ext_flag": "boolean"}}],
            "relationships": [{"type": kind, "properties": {"gsp_ext_eid": "integer"},
                               "endpoints": [{"source_label": "GspExt", "target_label": "GspExt"}]}
                              for kind in ("GSP_EXT_TRAIL", "GSP_EXT_LOW", "GSP_EXT_HIGH")]}


def _all_flag(vertices):
    values = [_FLAGS[uid] for uid in vertices]
    return False if False in values else None if None in values else True


def _trails(start, minimum, maximum):
    """Independent undirected TRAIL oracle: edges unique, vertices may recur.

    Each stored edge appears once in the incident-edge enumeration, including
    a self-loop. Parallel and reciprocal edges have different identities.
    This oracle never imports or evaluates the compiler's traversal steps.
    """
    def visit(vertices, used):
        if len(used) >= minimum:
            yield vertices
        if len(used) == maximum:
            return
        at = vertices[-1]
        for source, target, eid in _TRAIL_EDGES:
            if eid in used or at not in (source, target):
                continue
            neighbor = target if at == source else source
            yield from visit((*vertices, neighbor), used | {eid})
    yield from visit((start,), frozenset())


def _path_case(name, *, start=1, minimum=0, maximum=3, reverse=False,
               same_end=False, predicate=None, include_all=False):
    left = "(u:GspExt {uid:" + str(start) + "})"
    right = "(u)" if same_end else "(v:GspExt)"
    if reverse:
        left, right = right, left
    query = f"MATCH p = {left}-[:GSP_EXT_TRAIL*{minimum}..{maximum}]-{right}"
    if predicate is not None:
        query += " WHERE " + ("NOT " if predicate is False else "") + "all(x IN nodes(p) WHERE x.gsp_ext_flag = true)"
    query += " RETURN u.uid AS start, " + ("u.uid" if same_end else "v.uid") + " AS end, length(p) AS hops"
    if include_all:
        query += ", all(x IN nodes(p) WHERE x.gsp_ext_flag = true) AS ok"
    rows = []
    for vertices in _trails(start, minimum, maximum):
        if same_end and vertices[-1] != start:
            continue
        truth = _all_flag(vertices)
        if predicate is not None and truth is not predicate:
            continue
        row = {"start": start, "end": vertices[-1], "hops": len(vertices) - 1}
        if include_all:
            row["ok"] = truth
        rows.append(row)
    return name, query, rows


_PATH_CASES = [
    _path_case("zero_to_three_hops"),
    _path_case("endpoint_mirror", reverse=True),
    _path_case("zero_only", minimum=0, maximum=0),
    _path_case("one_hop_self_loop_once", minimum=1, maximum=1),
    _path_case("two_hops_parallel_and_revisited_vertex", minimum=2, maximum=2),
    _path_case("three_hops_cycle", minimum=3, maximum=3),
    _path_case("same_bound_endpoint", minimum=1, same_end=True),
    _path_case("isolated_start", start=7, minimum=1),
    _path_case("path_all_projection_truth_table", include_all=True),
    _path_case("path_all_true_filter", predicate=True),
    _path_case("path_all_unknown_not_false", predicate=False),
    _path_case("path_all_false_projection", start=4, minimum=0, maximum=1, include_all=True),
    _path_case("path_all_false_filter", start=4, minimum=0, maximum=1, predicate=False),
    _path_case("path_all_unknown_single_vertex", start=3, minimum=0, maximum=0, include_all=True),
    ("length_null", "RETURN length(null) AS hops", [{"hops": None}]),
]
_history_name, _history_query, _history_rows = _path_case("path_excludes_earlier_match_history", include_all=True)
_PATH_CASES.append((_history_name, "MATCH (z:GspExt {uid:4}) " + _history_query, _history_rows))
for _maximum in (0, 1):
    _PATH_CASES.append((
        f"zero_hop_length_list_distinct_{_maximum}",
        f"MATCH p=(u:GspExt)-[:GSP_EXT_TRAIL*0..{_maximum}]-(v:GspExt) "
        "WHERE length(p)=0 RETURN DISTINCT "
        "CASE WHEN u.uid=1 THEN [length(p)] ELSE range(0,0) END AS value",
        [{"value": [0]}],
    ))

_OPTIONAL_BASE = "MATCH (u:GspExt) WHERE u.gsp_ext_anchor = true "
_OPTIONAL_LOW = "OPTIONAL MATCH (u)-[r:GSP_EXT_LOW]->(v:GspExt) "
_OPTIONAL_HIGH = "OPTIONAL MATCH (u)-[s:GSP_EXT_HIGH]->(w:GspExt) WHERE w.gsp_ext_flag = true "
_OPTIONAL_ROWS = ([{"uid": 1, "low": 5, "high": 8}] * 4
                  + [{"uid": 2, "low": 5, "high": None}, {"uid": 3, "low": None, "high": 8},
                     {"uid": 4, "low": None, "high": None}])
_OPTIONAL_CASES = [
    ("independent_low_first", _OPTIONAL_BASE + _OPTIONAL_LOW + _OPTIONAL_HIGH
     + "RETURN u.uid AS uid, v.uid AS low, w.uid AS high", _OPTIONAL_ROWS),
    ("independent_high_first", _OPTIONAL_BASE + _OPTIONAL_HIGH + _OPTIONAL_LOW
     + "RETURN u.uid AS uid, v.uid AS low, w.uid AS high", _OPTIONAL_ROWS),
    ("null_relationship_projection", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "RETURN u.uid AS uid, r.gsp_ext_eid AS edge, v.gsp_ext_flag AS flag",
     [{"uid": 1, "edge": 201, "flag": True}, {"uid": 1, "edge": 202, "flag": True},
      {"uid": 2, "edge": 203, "flag": True}, {"uid": 3, "edge": None, "flag": None},
      {"uid": 4, "edge": None, "flag": None}]),
    ("where_false_preserves_outer_row", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "WHERE false RETURN u.uid AS uid, v.uid AS low",
     [{"uid": uid, "low": None} for uid in range(1, 5)]),
    ("where_null_preserves_outer_row", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "WHERE null RETURN u.uid AS uid, v.uid AS low",
     [{"uid": uid, "low": None} for uid in range(1, 5)]),
    ("optional_start_constraints_fail", "MATCH (u:GspExt {uid:1}) "
     "OPTIONAL MATCH (u {uid:999})-[:GSP_EXT_LOW]->(v:GspExt) RETURN u.uid AS uid, v.uid AS low",
     [{"uid": 1, "low": None}]),
    ("optional_end_constraints_fail", "MATCH (u:GspExt {uid:1}) "
     "OPTIONAL MATCH (u)-[:GSP_EXT_LOW]->(v:GspExt {uid:999}) RETURN u.uid AS uid, v.uid AS low",
     [{"uid": 1, "low": None}]),
    ("null_property_tests", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "RETURN u.uid AS uid, (v.uid IS NULL) AS missing, (v.uid IS NOT NULL) AS present",
     [{"uid": 1, "missing": False, "present": True}] * 2
     + [{"uid": 2, "missing": False, "present": True}, {"uid": 3, "missing": True, "present": False},
        {"uid": 4, "missing": True, "present": False}]),
    ("null_property_coalesce", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "RETURN u.uid AS uid, coalesce(v.uid, -1) AS low",
     [{"uid": 1, "low": 5}] * 2 + [{"uid": 2, "low": 5}, {"uid": 3, "low": -1}, {"uid": 4, "low": -1}]),
    ("null_bound_node_mandatory_match", "MATCH (u:GspExt {uid:4}) "
     "OPTIONAL MATCH (u)-[:GSP_EXT_LOW]->(v:GspExt) MATCH (v) RETURN u.uid AS uid, v.uid AS low", []),
    ("null_bound_node_optional_expansion", "MATCH (u:GspExt {uid:4}) "
     "OPTIONAL MATCH (u)-[:GSP_EXT_LOW]->(v:GspExt) "
     "OPTIONAL MATCH (v)-[:GSP_EXT_HIGH]->(w:GspExt) RETURN u.uid AS uid, v.uid AS low, w.uid AS high",
     [{"uid": 4, "low": None, "high": None}]),
    ("null_bound_node_exists_count", "MATCH (u:GspExt {uid:4}) "
     "OPTIONAL MATCH (u)-[:GSP_EXT_LOW]->(v:GspExt) "
     "RETURN u.uid AS uid, EXISTS { MATCH (v)-[:GSP_EXT_HIGH]->(w:GspExt) } AS has_edge, "
     "COUNT { MATCH (v)-[:GSP_EXT_HIGH]->(w:GspExt) } AS n",
     [{"uid": 4, "has_edge": False, "n": 0}]),
    ("outer_null_predicate_in_second_optional", _OPTIONAL_BASE + _OPTIONAL_LOW
     + "OPTIONAL MATCH (u)-[:GSP_EXT_HIGH]->(w:GspExt) WHERE v.gsp_ext_flag = true AND w.gsp_ext_flag = true "
     "RETURN u.uid AS uid, v.uid AS low, w.uid AS high",
     [{"uid": 1, "low": 5, "high": 8}] * 4
     + [{"uid": 2, "low": 5, "high": None}, {"uid": 3, "low": None, "high": None},
        {"uid": 4, "low": None, "high": None}]),
    ("duplicates_preserved_until_distinct", _OPTIONAL_BASE + _OPTIONAL_LOW + _OPTIONAL_HIGH
     + "RETURN DISTINCT u.uid AS uid, v.uid AS low, w.uid AS high", [_OPTIONAL_ROWS[0], *_OPTIONAL_ROWS[4:]]),
]
for _start, _suffix in ((1, "matched"), (7, "unmatched")):
    _name, _text, _expected = _path_case("bounded_path_" + _suffix, start=_start,
                                        minimum=1, maximum=2, include_all=True)
    _text = f"MATCH (u:GspExt {{uid:{_start}}}) OPTIONAL " + _text
    _OPTIONAL_CASES.append((_name, _text, _expected or [{"start": _start, "end": None,
                                                       "hops": None, "ok": None}]))


def _scalar_case(name, expression, expected):
    return name, f"RETURN {expression} AS value", [{"value": expected}]


_LIST_CASES = [
    _scalar_case("empty_list", "[]", []),
    _scalar_case("list_keeps_null_and_types", "[1, true, null, false, 2]", [1, True, None, False, 2]),
    _scalar_case("concat_keeps_order_and_duplicates", "[2, null, 2] + [1, 2]", [2, None, 2, 1, 2]),
    _scalar_case("concat_empty_left", "[] + [1, 2]", [1, 2]),
    _scalar_case("concat_empty_right", "[1, 2] + []", [1, 2]),
    _scalar_case("concat_null_left", "null + [1]", None),
    _scalar_case("concat_null_right", "[1] + null", None),
    _scalar_case("ascending_range", "range(1, 5)", [1, 2, 3, 4, 5]),
    _scalar_case("descending_default_empty", "range(5, 1)", []),
    _scalar_case("descending_range", "range(5, 1, -2)", [5, 3, 1]),
    _scalar_case("ascending_step", "range(1, 6, 2)", [1, 3, 5]),
    _scalar_case("wrong_direction_empty", "range(1, 5, -1)", []),
    _scalar_case("equal_endpoints", "range(2, 2, -2)", [2]),
    _scalar_case("negative_endpoints", "range(-3, 1, 2)", [-3, -1, 1]),
    _scalar_case("range_above_double_precision", "range(9007199254740992, 9007199254740994)",
                 [9007199254740992, 9007199254740993, 9007199254740994]),
    _scalar_case("range_near_signed_integer_minimum", "range(-9223372036854775808, -9223372036854775806)",
                 [-9223372036854775808, -9223372036854775807, -9223372036854775806]),
    _scalar_case("all_empty_true", "all(x IN [] WHERE x > 0)", True),
    _scalar_case("all_null_input", "all(x IN null WHERE x > 0)", None),
    _scalar_case("all_true", "all(x IN [1, 2, 3] WHERE x > 0)", True),
    _scalar_case("all_false_first", "all(x IN [0, 1, 2] WHERE x > 0)", False),
    _scalar_case("all_false_last", "all(x IN [1, 2, 0] WHERE x > 0)", False),
    _scalar_case("all_unknown", "all(x IN [1, null, 2] WHERE x > 0)", None),
    _scalar_case("all_false_before_unknown", "all(x IN [0, null, 2] WHERE x > 0)", False),
    _scalar_case("all_false_after_unknown", "all(x IN [null, 0, 2] WHERE x > 0)", False),
    _scalar_case("all_boolean_identity", "all(x IN [true, null] WHERE x)", None),
    _scalar_case("all_type_sensitive", "all(x IN [true, 1, null] WHERE x = true)", False),
    _scalar_case("all_concat_forward", "all(x IN ([0] + range(1, 5)) WHERE x > 0)", False),
    _scalar_case("all_concat_reverse", "all(x IN (range(1, 5) + [0]) WHERE x > 0)", False),
    _scalar_case("all_nested_scope", "all(x IN [1, 2] WHERE all(y IN [1, 2] WHERE y >= x))", False),
    _scalar_case("all_unknown_is_not_false", "NOT all(x IN [1, null] WHERE x > 0)", None),
    _scalar_case("size_empty", "size([])", 0),
    _scalar_case("size_null", "size(null)", None),
    _scalar_case("range_generated_on_server", "size(range(1, 40000))", 40000),
]


def _failure(record, exc):
    record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    if isinstance(exc, CaseFailure):
        record["failure"] = {"status": exc.status, "reason": exc.reason,
                             "stop_target": exc.stop_target, "details": exc.details}


@pytest.fixture(scope="module")
def extended_config():
    config = read_config(Path(os.environ["GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_CONFIG"]))
    assert config["database"]["dbms"] in {"janusgraph", "hugegraph"}
    assert config["database"]["language"] == "gremlin"
    assert config["database"]["dedicated"] is True and config["database"]["allow_reset"] is True
    assert config["measurement"]["max_rows"] >= 1000
    return config


@pytest.fixture(scope="module")
def evidence_directory(tmp_path_factory):
    supplied = os.getenv("GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_OUTPUT")
    output = Path(supplied) if supplied else tmp_path_factory.mktemp("gremlin-extended-interface")
    output.mkdir(parents=True, exist_ok=True)
    yield output
    entries = []
    for path in sorted(output.glob("*.json")):
        if path.name in {"semantic-fixture.json", "validation-summary.json"}:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if "status" in record:
            entries.append({"file": path.name, "status": record["status"], "reason": record.get("reason")})
    _write(output / "validation-summary.json", {
        "kind": "interface_and_semantics_only", "performance_screening": False,
        "planned_path_cases": len(_PATH_CASES), "planned_optional_cases": len(_OPTIONAL_CASES),
        "planned_list_cases": len(_LIST_CASES),
        "planned_compiler_boundary_checks": 1,
        "planned_hugegraph_parent_edge_gate": 1,
        "planned_pattern_inputs": len(PATTERNS) * len(SEEDS) * len(COMPLEXITIES)
                                  + len(STATE_VARIANTS) * len(SEEDS),
        "counts": dict(Counter(item["status"] for item in entries)), "entries": entries,
    })


@pytest.fixture(scope="module")
def semantic_graph(extended_config, evidence_directory):
    db = make_database(extended_config["database"], extended_config["measurement"])
    evidence = {"kind": "interface_oracle_only", "performance_screening": False,
                "version": db.version, "nodes": _NODES, "edges": _EDGES, "status": "started"}
    try:
        db._reset()
        db._prepare_schema(_SemanticGraph())
        values, _ = db._script(db._vertex_script(), {"gsp_rows": _NODES})
        assert values == [len(_NODES)], values
        values, _ = db._script(db._edge_script(), {"gsp_rows": _EDGES})
        assert values == [len(_EDGES)], values
        evidence["indexes"] = db._check_indexes(_SemanticGraph())
        # This scan is outside timing and compares every loaded property and
        # relationship identity; edge/vertex counts alone would miss mistakes.
        values, _ = db._script("g.V().hasLabel('GspExt').valueMap().toList()", require_list=True)
        expected = [{key: [value] for key, value in row["properties"].items()} for row in _NODES]
        actual = [{key: value for key, value in row.items() if key != "_gsp_identity"} for row in values]
        assert typed_bag(actual) == typed_bag(expected), (actual, expected)
        evidence["loaded_vertices"] = values
        values, _ = db._script("g.E().project('source','target','type','eid')"
                              ".by(__.outV().values('uid')).by(__.inV().values('uid'))"
                              ".by(__.label()).by(__.values('gsp_ext_eid')).toList()", require_list=True)
        expected = [{"source": row["source"], "target": row["target"], "type": row["type"],
                     "eid": row["properties"]["gsp_ext_eid"]} for row in _EDGES]
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


def _check_semantic_case(db, config, output, kind, name, text, expected):
    target = config["database"]["dbms"]
    record = {"kind": kind, "status": "started", "dbms": target, "version": db.version,
              "source_cypher": text, "expected_rows": expected, "performance_screening": False}
    try:
        compiled = generate(parse_query(text), target, "gremlin",
                            verified_features=config["database"].get("verified_features", ()))
        record.update(gremlin=compiled.text, bindings=compiled.bindings, columns=compiled.columns)
        if name == "range_generated_on_server":
            # A compact query with scalar endpoint bindings is required; a
            # client-precomputed 40,000-item list would change the mechanism.
            # This also crosses TinkerPop 3.5.1's signed-short loops counter.
            assert len(compiled.text) < 20000
            assert all(not isinstance(value, (list, dict)) for value in compiled.bindings.values())
        actual = db.execute(compiled.text, compiled.bindings, config["measurement"]["timeout_seconds"])
        record["execution"] = actual
        assert actual["complete"], actual
        physical_columns = {source for source, _ in compiled.columns}
        assert all(set(row) == physical_columns for row in actual["rows"]), actual
        record["logical_rows"] = compiled.normalize_rows(actual["rows"])
        assert typed_bag(record["logical_rows"]) == typed_bag(expected), record
        record["profile"] = db.explain(compiled.text, compiled.bindings, analyze=True)
        assert record["profile"].get("plan"), record
        record["status"] = "passed"
    except Exception as exc:
        _failure(record, exc)
        raise
    finally:
        _write(output / f"{kind}-{name}.json", record)


# Fixture-dependent tests run before generated Pattern tests rebuild the graph.
@pytest.mark.parametrize("name,text,expected", _PATH_CASES, ids=[case[0] for case in _PATH_CASES])
def test_p3_trail_semantics(semantic_graph, extended_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, extended_config, evidence_directory, "path", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _OPTIONAL_CASES, ids=[case[0] for case in _OPTIONAL_CASES])
def test_p13_optional_semantics(semantic_graph, extended_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, extended_config, evidence_directory, "optional", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _LIST_CASES, ids=[case[0] for case in _LIST_CASES])
def test_p16_list_quantifier_semantics(semantic_graph, extended_config, evidence_directory, name, text, expected):
    _check_semantic_case(semantic_graph, extended_config, evidence_directory, "list", name, text, expected)


def test_dynamic_range_is_explicitly_rejected(extended_config, evidence_directory):
    # Existing P16 uses literal endpoints. Keeping this unsupported IR form
    # explicit prevents an accidental client-side range fallback from being
    # reported as complete general range compilation.
    target = extended_config["database"]["dbms"]
    text = "RETURN range(1, COUNT { MATCH (u:GspExt) }) AS value"
    record = {"kind": "compiler_boundary_only", "performance_screening": False,
              "source_cypher": text, "dbms": target, "status": "started"}
    try:
        with pytest.raises(UnsupportedFeature) as caught:
            generate(parse_query(text), target, "gremlin",
                     verified_features=extended_config["database"].get("verified_features", ()))
        assert "gremlin_dynamic_range" in caught.value.features
        record.update(status="passed", expected_rejection="gremlin_dynamic_range")
    except Exception as exc:
        _failure(record, exc)
        raise
    finally:
        _write(evidence_directory / "boundary-dynamic-range.json", record)


def _check_pattern_interface(config, output, pattern, seed, complexity, state_variant="original"):
    target = config["database"]["dbms"]
    name = f"pattern-{pattern}-s{seed}-c{complexity}-{state_variant}"
    record = {"kind": "existing_pattern_interface", "performance_screening": False,
              "status": "started", "pattern": pattern, "dbms": target, "scale": 3,
              "seed": seed, "data_seed": 0, "complexity": complexity,
              "state_variant": state_variant, "stages": {}}
    db = None
    try:
        case = prepare_case(None, pattern, 3, seed, 0, complexity, state_variant,
                            database=config["database"])
        record["case"] = case
        assert not case.get("compile_error"), case.get("compile_error")
        assert pattern in supported_patterns(config["database"])
        db = make_database(config["database"], config["measurement"])
        record["version"] = db.version
        db.restore(case)
        baseline = _inspect_case(db, case, config, record["stages"].setdefault("initial_build", {}), profile=True)
        db.close()
        db = make_database(config["database"], config["measurement"])
        assert db.version == record["version"]
        reopened = _inspect_case(db, case, config, record["stages"].setdefault("new_connection", {}), profile=False)
        assert reopened == baseline
        db.restore(case)
        rebuilt = _inspect_case(db, case, config, record["stages"].setdefault("second_build", {}), profile=True)
        assert rebuilt == baseline
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
        _failure(record, exc)
        raise
    finally:
        _write(output / f"{name}.json", record)
        if db is not None:
            db.close()


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("complexity", COMPLEXITIES)
def test_existing_pattern_interfaces(extended_config, evidence_directory, pattern, seed, complexity):
    _check_pattern_interface(extended_config, evidence_directory, pattern, seed, complexity)


@pytest.mark.parametrize("pattern,state_variant", STATE_VARIANTS)
@pytest.mark.parametrize("seed", SEEDS)
def test_existing_state_variant_interfaces(extended_config, evidence_directory, pattern, state_variant, seed):
    _check_pattern_interface(extended_config, evidence_directory, pattern, seed, 2, state_variant)


_PARENT_LABELS = {uid: "GspExt" + "ABC"[(uid - 1) % 3] for uid in _FLAGS}
_PARENT_NODES = [{**row, "labels": [_PARENT_LABELS[row["uid"]]]} for row in _NODES]
_PARENT_EDGES = [{"source": source, "target": target,
                  "source_label": _PARENT_LABELS[source], "target_label": _PARENT_LABELS[target],
                  "type": "GSP_EXT_PARENT", "properties": {"gsp_ext_eid": eid}, "ordinal": ordinal}
                 for ordinal, (source, target, eid) in enumerate(_TRAIL_EDGES)]


class _ParentSemanticGraph:
    """Exercise P3's native schema encoding without changing its graph recipe."""

    pattern = "P3"
    indexes = [{"name": "gsp_cost_lookup", "label": label, "property": "uid"}
               for label in sorted(set(_PARENT_LABELS.values()))]

    @staticmethod
    def counts():
        return {"nodes": len(_PARENT_NODES), "edges": len(_PARENT_EDGES)}

    @staticmethod
    def schema():
        endpoints = sorted({(row["source_label"], row["target_label"]) for row in _PARENT_EDGES})
        return {"nodes": [{"label": label, "properties": {
            "uid": "integer", "gsp_ext_anchor": "boolean", "gsp_ext_flag": "boolean"}}
            for label in sorted(set(_PARENT_LABELS.values()))],
            "relationships": [{"type": "GSP_EXT_PARENT", "properties": {"gsp_ext_eid": "integer"},
                               "endpoints": [{"source_label": source, "target_label": target}
                                             for source, target in endpoints]}]}


def _parent_query(text):
    """Use the same TRAIL oracle on the test-only multi-label population."""
    text = text.replace(":GspExt", "").replace("GSP_EXT_TRAIL", "GSP_EXT_PARENT")
    for variable in ("u", "z"):
        for uid, label in _PARENT_LABELS.items():
            text = text.replace(f"({variable} {{uid:{uid}}})", f"({variable}:{label} {{uid:{uid}}})")
    return text


def test_hugegraph_parent_edge_identity_and_trails(extended_config, evidence_directory):
    if extended_config["database"]["dbms"] != "hugegraph":
        pytest.skip("native parent/sub edge labels are a HugeGraph P3 adapter gate")
    record = {"kind": "parent_edge_interface_oracle_only", "performance_screening": False,
              "status": "started", "nodes": _PARENT_NODES, "edges": _PARENT_EDGES,
              "adjacency": {}, "path_cases": {}}
    db = None
    try:
        db = make_database(extended_config["database"], extended_config["measurement"])
        record["version"] = db.version
        spec = _ParentSemanticGraph()
        db._reset()
        db._prepare_schema(spec)
        parents = db._parent_edge_labels(spec)
        assert set(parents) == {"GSP_EXT_PARENT"}
        assert len(parents["GSP_EXT_PARENT"]) > 1
        for rows, script in ((_PARENT_NODES, db._vertex_script()), (_PARENT_EDGES, db._edge_script())):
            values, _ = db._script(script, {"gsp_rows": rows})
            assert values == [len(rows)], values
        record["indexes"] = db._check_indexes(spec)
        record["physical_schema"] = db._check_parent_edge_schema(spec, parents)

        # ID-to-string conversion is solely an untimed transport inspection;
        # generated queries below keep the logical parent label unchanged.
        identity_projection = (".project('eid','identity','source','target')"
                               ".by(__.values('gsp_ext_eid'))"
                               ".by(__.id().map{ t -> t.get().toString() })"
                               ".by(__.outV().values('uid')).by(__.inV().values('uid'))")
        all_edges, _ = db._script("g.E()" + identity_projection + ".toList()", require_list=True)
        assert len(all_edges) == len(_PARENT_EDGES)
        expected_edges = [{"source": row["source"], "target": row["target"],
                           "eid": row["properties"]["gsp_ext_eid"]} for row in _PARENT_EDGES]
        assert typed_bag([{key: row[key] for key in ("source", "target", "eid")} for row in all_edges]) == typed_bag(expected_edges)
        identities = {row["eid"]: row["identity"] for row in all_edges}
        assert len(identities) == len(set(identities.values())) == len(_PARENT_EDGES)
        record["loaded_edges"] = all_edges

        for uid, label in _PARENT_LABELS.items():
            root = f"g.V('{label}:{uid}')"
            for direction in ("outE", "inE", "bothE"):
                query = root + f".{direction}('GSP_EXT_PARENT')" + identity_projection + ".toList()"
                actual, _ = db._script(query, require_list=True)
                record["adjacency"][f"{uid}-{direction}"] = {"query": query, "rows": actual}
                expected = [row for row in expected_edges
                            if (direction != "inE" and row["source"] == uid)
                            or (direction != "outE" and row["target"] == uid)]
                observed = Counter(row["eid"] for row in actual)
                expected_ids = {row["eid"] for row in expected}
                assert set(observed) == expected_ids, (uid, direction, actual, expected)
                for row in actual:
                    assert row["identity"] == identities[row["eid"]], (uid, direction, row, identities)
                    expected_row = next(item for item in expected if item["eid"] == row["eid"])
                    assert (row["source"], row["target"]) == (expected_row["source"], expected_row["target"])
                for row in expected:
                    # Native bothE may expose a self-loop once or once per
                    # direction. Require every non-loop exactly once; the
                    # compiler's undirected union below must emit loops once.
                    allowed = (1, 2) if direction == "bothE" and row["source"] == row["target"] else (1,)
                    assert observed[row["eid"]] in allowed, (uid, direction, observed, row)

        for name, source, expected in _PATH_CASES:
            if name == "length_null":
                continue
            source = _parent_query(source)
            compiled = generate(parse_query(source), "hugegraph", "gremlin",
                                verified_features=extended_config["database"].get("verified_features", ()))
            if "*0..0" not in source:
                assert "GSP_EXT_PARENT" in compiled.text
            assert not any(child["label"] in compiled.text for child in parents["GSP_EXT_PARENT"])
            evidence = {"source_cypher": source, "gremlin": compiled.text, "bindings": compiled.bindings,
                        "expected_rows": expected}
            record["path_cases"][name] = evidence
            actual = db.execute(compiled.text, compiled.bindings, extended_config["measurement"]["timeout_seconds"])
            evidence["execution"] = actual
            assert actual["complete"], actual
            evidence["logical_rows"] = compiled.normalize_rows(actual["rows"])
            assert typed_bag(evidence["logical_rows"]) == typed_bag(expected), evidence
            evidence["profile"] = db.explain(compiled.text, compiled.bindings, analyze=True)
            assert evidence["profile"].get("plan"), evidence
        record["status"] = "passed"
    except Exception as exc:
        _failure(record, exc)
        raise
    finally:
        _write(evidence_directory / "parent-edge-identity-and-trails.json", record)
        if db is not None:
            db.close()
