"""Opt-in ArcadeDB Gremlin interface gates, with no performance screening.

GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG must name an explicitly resettable,
dedicated test configuration. GRAPHSYMPERF_ARCADE_GREMLIN_TEST_OUTPUT retains
the existing semantic fixtures and generated Pattern evidence. These checks
establish result and interface behavior, not missed-optimization findings.
"""

from collections import Counter
import json
import os
from pathlib import Path

import pytest

from executor import make_database, supported_patterns, typed_bag
from generator import PatternNotApplicable
from languages import generate
from main import prepare_legacy_case as prepare_case, read_case, read_config, save_case
from query import parse_query
from test_gremlin_semantics import _CASES, _EDGES, _NODES


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG"),
    reason="no dedicated ArcadeDB Gremlin interface-test configuration supplied")]

PATTERNS = ("P1", "P2", "P4", "P5", "P6", "P8", "P9", "P10", "P11", "P12", "P14", "P15")
STATE_VARIANTS = (("P10", "empty_low", 0), ("P11", "empty_low", 0), ("P11", "multi_low", 8))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def arcade_config():
    config = read_config(Path(os.environ["GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG"]))
    assert config["database"]["dbms"] == "arcadedb"
    assert config["database"]["language"] == "gremlin"
    assert set(PATTERNS).issubset(supported_patterns(config["database"]))
    assert config["measurement"]["max_rows"] >= 1000
    return config


@pytest.fixture(scope="module")
def evidence_directory(tmp_path_factory):
    supplied = os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_OUTPUT")
    output = Path(supplied) if supplied else tmp_path_factory.mktemp("arcade-gremlin-interface")
    output.mkdir(parents=True, exist_ok=True)
    yield output
    entries = []
    for path in sorted(output.glob("*-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "status" in record:
            entries.append({"file": path.name, "status": record["status"], "reason": record.get("reason")})
    _write(output / "validation-summary.json", {
        "kind": "interface_and_semantics_only", "performance_screening": False,
        "planned_semantic_cases": len(_CASES), "planned_pattern_inputs": len(PATTERNS) * 2 * 3 + len(STATE_VARIANTS),
        "planned_state_variants": len(STATE_VARIANTS),
        "counts": dict(Counter(item["status"] for item in entries)), "entries": entries,
    })


@pytest.fixture(scope="module")
def semantic_graph(arcade_config, evidence_directory):
    db = make_database(arcade_config["database"], arcade_config["measurement"])
    evidence = {"kind": "semantic_fixture", "version": db.version, "nodes": _NODES, "edges": _EDGES,
                "schema_commands": [], "batch_responses": []}
    try:
        current = db._sql("SELECT name, type FROM schema:types")
        for name in ("GSP_SEM_LINK", "GspSem"):
            if any(row.get("name") == name for row in current):
                command = f"DROP TYPE {name} UNSAFE"
                evidence["schema_commands"].append(command)
                db._sql(command, command=True)
        commands = ["CREATE VERTEX TYPE GspSem", "CREATE PROPERTY GspSem.uid LONG",
                    "CREATE PROPERTY GspSem.gsp_sem_flag BOOLEAN", "CREATE PROPERTY GspSem.gsp_sem_n LONG",
                    "CREATE EDGE TYPE GSP_SEM_LINK", "CREATE PROPERTY GSP_SEM_LINK.gsp_sem_eid LONG"]
        for command in commands:
            evidence["schema_commands"].append(command)
            db._sql(command, command=True)
        nodes = [{"@type": "vertex", "@class": "GspSem", "@id": f"sem:{row['uid']}", **row} for row in _NODES]
        loaded = db._batch(nodes)
        evidence["batch_responses"].append(loaded)
        assert loaded.get("verticesCreated") == len(_NODES), loaded
        assert loaded.get("edgesCreated") == 0, loaded
        identities = loaded["idMapping"]
        assert set(identities) == {row["@id"] for row in nodes}, loaded
        edges = [{"@type": "edge", "@class": "GSP_SEM_LINK", "@from": identities[f"sem:{start}"],
                  "@to": identities[f"sem:{end}"], "gsp_sem_eid": eid} for start, end, eid in _EDGES]
        loaded = db._batch(edges)
        evidence["batch_responses"].append(loaded)
        assert loaded.get("edgesCreated") == len(_EDGES), loaded
        assert loaded.get("verticesCreated") == 0, loaded
        evidence["status"] = "fixture_ready"
        _write(evidence_directory / "semantic-fixture.json", evidence)
        yield db
    finally:
        # Subsequent Pattern tests rebuild the same dedicated graph. Closing
        # this fixture must not delete a later test's reconstructed graph.
        db.close()


@pytest.mark.parametrize("name,text,expected", _CASES, ids=[case[0] for case in _CASES])
def test_arcade_existing_semantic_cases(semantic_graph, arcade_config, evidence_directory, name, text, expected):
    db = semantic_graph
    record = {"kind": "existing_semantic_fixture", "status": "started", "version": db.version,
              "source_cypher": text, "expected_rows": expected}
    path = evidence_directory / f"semantic-{name}.json"
    try:
        compiled = generate(parse_query(text), "arcadedb", "gremlin",
                            verified_features=arcade_config["database"].get("verified_features", ()))
        record.update(gremlin=compiled.text, bindings=compiled.bindings, columns=compiled.columns)
        actual = db.execute(compiled.text, compiled.bindings, arcade_config["measurement"]["timeout_seconds"])
        record["execution"] = actual
        assert actual["complete"], actual
        rows = compiled.normalize_rows(actual["rows"])
        record["logical_rows"] = rows
        assert typed_bag(rows) == typed_bag(expected), record
        record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _write(path, record)


def _inspect_case(db, case, config, evidence, *, profile):
    evidence.update(state=db.check_state(case), executions={}, plans={})
    baseline = None
    for side in ("a", "b", "ref"):
        if not case.get(f"query_{side}"):
            continue
        query = case[f"query_{side}"]
        parameters = {**case.get("parameters", {}), **case.get("query_bindings", {}).get(side, {})}
        actual = db.execute(query, parameters, config["measurement"]["timeout_seconds"])
        evidence["executions"][side] = actual
        assert actual["complete"], (side, actual)
        columns = case["output_columns"][side]
        assert all(set(row) == {source for source, _ in columns} for row in actual["rows"]), actual
        rows = [{logical: row[source] for source, logical in columns} for row in actual["rows"]]
        evidence["executions"][side]["logical_rows"] = rows
        bag = typed_bag(rows)
        if baseline is None:
            baseline = bag
        assert bag == baseline, (side, evidence)
        if "expected_rows" in case:
            assert bag == typed_bag(case["expected_rows"]), (side, case["expected_rows"], rows)
        if profile:
            evidence["plans"][side] = db.explain(query, parameters, analyze=True)
            assert evidence["plans"][side].get("plan"), (side, evidence["plans"][side])
    return baseline


def _check_pattern_interfaces(arcade_config, evidence_directory, pattern, seed, complexity, *,
                              state_variant="original", expected_row_count=None):
    name = f"pattern-{pattern}-s{seed}-c{complexity}"
    if state_variant != "original":
        name += f"-{state_variant}"
    record = {"kind": "existing_pattern_interface", "status": "started", "pattern": pattern,
              "scale": 3, "seed": seed, "data_seed": 0, "complexity": complexity,
              "state_variant": state_variant, "stages": {}}
    path = evidence_directory / f"{name}.json"
    db = None
    try:
        try:
            case = prepare_case(None, pattern, 3, seed, 0, complexity, state_variant,
                                database=arcade_config["database"])
        except PatternNotApplicable as exc:
            record.update(status="not_applicable", reason=str(exc))
            pytest.skip(f"existing Pattern generator: {exc}")
        record["case"] = case
        if case.get("compile_error"):
            error = case["compile_error"]
            if error["status"] == "unsupported_transform":
                record.update(status="unsupported_transform", reason=error["reason"])
                pytest.skip(f"existing transformation has no target difference: {error['reason']}")
            record.update(status="failed", reason=f"supported Pattern failed compilation: {error}")
            pytest.fail(f"supported Pattern failed compilation: {error}")
        db = make_database(arcade_config["database"], arcade_config["measurement"])
        record["version"] = db.version
        db.restore(case)
        evidence = record["stages"].setdefault("initial_build", {})
        baseline = _inspect_case(db, case, arcade_config, evidence, profile=True)
        if expected_row_count is not None:
            assert sum(baseline.values()) == expected_row_count, record
        db.close()
        db = None

        db = make_database(arcade_config["database"], arcade_config["measurement"])
        assert db.version == record["version"]
        evidence = record["stages"].setdefault("new_connection", {})
        reopened = _inspect_case(db, case, arcade_config, evidence, profile=False)
        assert reopened == baseline, record
        db.restore(case)
        evidence = record["stages"].setdefault("second_build", {})
        rebuilt = _inspect_case(db, case, arcade_config, evidence, profile=True)
        assert rebuilt == baseline, record

        archive = evidence_directory / "archives" / name
        save_case(archive, case, arcade_config["measurement"], {
            "status": "interface_checked", "dbms": "arcadedb", "language": "gremlin",
            "database_version": db.version, "performance_screening": False,
        })
        restored, settings = read_case(archive)
        assert restored == case and settings == arcade_config["measurement"]
        record.update(status="passed", archive_roundtrip=True)
    except Exception as exc:
        record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _write(path, record)
        if db is not None:
            db.close()


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("seed", [0, 17])
@pytest.mark.parametrize("complexity", [0, 1, 2])
def test_arcade_existing_pattern_interfaces(arcade_config, evidence_directory, pattern, seed, complexity):
    _check_pattern_interfaces(arcade_config, evidence_directory, pattern, seed, complexity)


@pytest.mark.parametrize("pattern,variant,rows", STATE_VARIANTS)
def test_arcade_existing_empty_and_multi_low_states(arcade_config, evidence_directory, pattern, variant, rows):
    _check_pattern_interfaces(arcade_config, evidence_directory, pattern, 214501591, 2,
                              state_variant=variant, expected_row_count=rows)
