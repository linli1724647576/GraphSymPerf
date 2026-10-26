"""Opt-in S1--S10 interface gates on the dedicated ArcadeDB Gremlin target.

Use the same configuration/output variables as the existing ArcadeDB gates.
Evidence is isolated under ``scor/`` so the previous Pattern/semantic summary
is never overwritten. These checks establish semantics and transport only;
they do not create performance probes or missed-optimization findings.
"""

from collections import Counter
import json
import os
from pathlib import Path

import pytest

from executor import supported_patterns, typed_bag
from languages import generate
from main import read_config
from query import parse_query
from test_arcade_gremlin_integration import (
    _check_pattern_interfaces,
    _write,
    semantic_graph,  # Reuse the existing four-vertex semantic fixture verbatim.
)


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG"),
    reason="no dedicated ArcadeDB Gremlin interface-test configuration supplied")]

PATTERNS = tuple(f"S{i}" for i in range(1, 11))
SEEDS = (0, 1, 2, 3, 6, 9, 18, 27, 54)
COMPLEXITIES = (0, 1, 2)

# A distinct constant in the unselected branch lets the native PROFILE tree
# identify that branch without depending on the position of ChooseStep's
# children. The graph and expected rows come from the existing GspSem fixture.
_CONDITIONAL_CASES = [
    ("true", "CYPHER 25 WHEN true THEN { RETURN 7 AS value } ELSE { RETURN 901 AS value }",
     [{"value": 7}], "ConstantStep(901"),
    ("false", "CYPHER 25 WHEN false THEN { RETURN 902 AS value } ELSE { RETURN 9 AS value }",
     [{"value": 9}], "ConstantStep(902"),
    ("null", "CYPHER 25 WHEN null THEN { RETURN 903 AS value } ELSE { RETURN 9 AS value }",
     [{"value": 9}], "ConstantStep(903"),
    ("empty_then", "CYPHER 25 WHEN true THEN { MATCH (u:GspSem {uid:999}) RETURN 7 AS value } "
     "ELSE { RETURN 904 AS value }", [], "ConstantStep(904"),
    ("empty_else", "CYPHER 25 WHEN false THEN { RETURN 905 AS value } "
     "ELSE { MATCH (u:GspSem {uid:999}) RETURN 9 AS value }", [], "ConstantStep(905"),
    ("duplicate_then", "CYPHER 25 WHEN true THEN { MATCH (u:GspSem {uid:1})-[:GSP_SEM_LINK]->(v:GspSem) "
     "RETURN 7 AS value } ELSE { RETURN 906 AS value }", [{"value": 7}] * 3, "ConstantStep(906"),
    ("duplicate_else", "CYPHER 25 WHEN false THEN { RETURN 907 AS value } "
     "ELSE { MATCH (u:GspSem {uid:1})-[:GSP_SEM_LINK]->(v:GspSem) RETURN 9 AS value }",
     [{"value": 9}] * 3, "ConstantStep(907"),
    ("dynamic_count_true", "CYPHER 25 WHEN COUNT { MATCH (u:GspSem {uid:1}) } > 0 "
     "THEN { RETURN 7 AS value } ELSE { RETURN 908 AS value }", [{"value": 7}], "ConstantStep(908"),
    ("dynamic_count_false", "CYPHER 25 WHEN COUNT { MATCH (u:GspSem {uid:999}) } > 0 "
     "THEN { RETURN 909 AS value } ELSE { RETURN 9 AS value }", [{"value": 9}], "ConstantStep(909"),
    ("dynamic_null", "CYPHER 25 WHEN CASE WHEN COUNT { MATCH (u:GspSem {uid:1}) } > 0 "
     "THEN null ELSE true END THEN { RETURN 913 AS value } ELSE { RETURN 9 AS value }",
     [{"value": 9}], "ConstantStep(913"),
    ("same_variable_then_scope", "CYPHER 25 WHEN COUNT { MATCH (u:GspSem {uid:1}) } > 0 "
     "THEN { MATCH (u:GspSem {uid:2}) RETURN u.uid AS value } ELSE { RETURN 910 AS value }",
     [{"value": 2}], "ConstantStep(910"),
    ("same_variable_else_scope", "CYPHER 25 WHEN COUNT { MATCH (u:GspSem {uid:1}) } = 0 "
     "THEN { RETURN 911 AS value } ELSE { MATCH (u:GspSem {uid:3}) RETURN u.uid AS value }",
     [{"value": 3}], "ConstantStep(911"),
    ("unselected_graph_branch", "CYPHER 25 WHEN true THEN { RETURN 7 AS value } "
     "ELSE { MATCH (u:GspSem {uid:1})-[:GSP_SEM_LINK]->(v:GspSem) RETURN 912 AS value }",
     [{"value": 7}], "VertexStep(OUT,[GSP_SEM_LINK]"),
]

_NULL_SAFE = "(v.gsp_sem_flag = u.gsp_sem_flag) OR (v.gsp_sem_flag IS NULL AND u.gsp_sem_flag IS NULL)"
_MISSING_SAFE = _NULL_SAFE.replace("gsp_sem_flag", "gsp_sem_missing_key")
_MEMBERSHIP_CASES = [
    # This property is absent on every fixture vertex, so both endpoints of
    # each existing edge have NULL keys. u=1/2 have witnesses, u=3/4 do not.
    ("exists_null_member", f"MATCH (u:GspSem) WHERE EXISTS {{ MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) WHERE {_MISSING_SAFE} }} "
     "RETURN DISTINCT u.uid AS uid, u.gsp_sem_missing_key AS key", [{"uid": 1, "key": None}, {"uid": 2, "key": None}]),
    ("not_exists_null_member", f"MATCH (u:GspSem) WHERE NOT EXISTS {{ MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) WHERE {_MISSING_SAFE} }} "
     "RETURN DISTINCT u.uid AS uid, u.gsp_sem_missing_key AS key", [{"uid": 3, "key": None}, {"uid": 4, "key": None}]),
    ("exists_nonnull_member", f"MATCH (u:GspSem) WHERE EXISTS {{ MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) WHERE {_NULL_SAFE} }} "
     "RETURN DISTINCT u.gsp_sem_flag AS key", [{"key": True}]),
    ("not_exists_nonnull_member", f"MATCH (u:GspSem) WHERE NOT EXISTS {{ MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) WHERE {_NULL_SAFE} }} "
     "RETURN DISTINCT u.gsp_sem_flag AS key", [{"key": False}, {"key": None}, {"key": True}]),
    # EXISTS is a Boolean witness and must not multiply u=1 by its three
    # outgoing edges; the two different matching roots retain two NULL rows.
    ("exists_preserves_matching_rows", f"MATCH (u:GspSem) WHERE EXISTS {{ MATCH (u)-[:GSP_SEM_LINK]->(v:GspSem) WHERE {_MISSING_SAFE} }} "
     "RETURN u.gsp_sem_missing_key AS key", [{"key": None}, {"key": None}]),
]

_COALESCE_CASES = [
    ("zero", "RETURN coalesce(null, 0, COUNT { MATCH (u:GspSem) }) AS value", [{"value": 0}]),
    ("false", "RETURN coalesce(null, false, COUNT { MATCH (u:GspSem) }) AS value", [{"value": False}]),
    ("empty_string", "RETURN coalesce(null, '', COUNT { MATCH (u:GspSem) }) AS value", [{"value": ""}]),
]


@pytest.fixture(scope="module")
def arcade_config():
    config = read_config(Path(os.environ["GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG"]))
    assert config["database"]["dbms"] == "arcadedb"
    assert config["database"]["language"] == "gremlin"
    assert set(PATTERNS) <= supported_patterns(config["database"])
    assert config["measurement"]["max_rows"] >= 1000
    return config


@pytest.fixture(scope="module")
def evidence_directory(tmp_path_factory):
    supplied = os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_OUTPUT")
    root = Path(supplied) if supplied else tmp_path_factory.mktemp("arcade-gremlin-interface")
    output = root / "scor"
    output.mkdir(parents=True, exist_ok=True)
    yield output
    entries = []
    for path in sorted(output.glob("*-*.json")):
        if path.name in {"semantic-fixture.json", "validation-summary.json"}:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if "status" in record:
            entries.append({"file": path.name, "status": record["status"], "reason": record.get("reason")})
    _write(output / "validation-summary.json", {
        "kind": "interface_and_semantics_only", "performance_screening": False,
        "planned_pattern_inputs": len(PATTERNS) * len(SEEDS) * len(COMPLEXITIES),
        "planned_conditional_cases": len(_CONDITIONAL_CASES),
        "planned_null_safe_membership_cases": len(_MEMBERSHIP_CASES),
        "planned_falsey_coalesce_cases": len(_COALESCE_CASES),
        "counts": dict(Counter(item["status"] for item in entries)), "entries": entries,
    })


def _profile_steps(value):
    """Visit native steps recursively, including nested branches and guards."""
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and "elements" in value and "traversers" in value:
            yield value
        for child in value.values():
            yield from _profile_steps(child)
    elif isinstance(value, list):
        for child in value:
            yield from _profile_steps(child)


def _check_unselected_branch(profile, marker):
    matches = [step for step in _profile_steps(profile) if step["name"].startswith(marker)]
    evidence = {"marker": marker, "status": "checked" if matches else "not_exposed", "steps": matches}
    # A provider may omit a pruned branch from its native plan. Record this
    # limit explicitly; no positional guess or invented zero is permitted.
    for step in matches:
        assert step["elements"] == step["traversers"] == 0, evidence
        # PROFILE setup/initialization time is not proof of processed rows.
        # Keep native timing fields in evidence; zero row/traverser counts
        # establish that the unselected branch did not process its input.
    return evidence


def _check_semantic_case(db, config, output, kind, name, text, expected, *, unselected_marker=None):
    record = {"kind": kind, "status": "started", "version": db.version,
              "source_cypher": text, "expected_rows": expected, "performance_screening": False}
    try:
        compiled = generate(parse_query(text), "arcadedb", "gremlin",
                            verified_features=config["database"].get("verified_features", ()))
        record.update(gremlin=compiled.text, bindings=compiled.bindings, columns=compiled.columns)
        actual = db.execute(compiled.text, compiled.bindings, config["measurement"]["timeout_seconds"])
        record["execution"] = actual
        assert actual["complete"], actual
        record["logical_rows"] = compiled.normalize_rows(actual["rows"])
        assert typed_bag(record["logical_rows"]) == typed_bag(expected), record
        record["profile"] = db.explain(compiled.text, compiled.bindings, analyze=True)
        assert record["profile"].get("plan"), record
        if unselected_marker:
            record["unselected_branch_work"] = _check_unselected_branch(record["profile"], unselected_marker)
        record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _write(output / f"{kind}-{name}.json", record)


# These fixture checks deliberately precede graph-rebuilding Pattern checks.
@pytest.mark.parametrize("name,text,expected,marker", _CONDITIONAL_CASES,
                         ids=[case[0] for case in _CONDITIONAL_CASES])
def test_arcade_conditional_query_semantics(semantic_graph, arcade_config, evidence_directory,
                                            name, text, expected, marker):
    _check_semantic_case(semantic_graph, arcade_config, evidence_directory, "conditional", name,
                         text, expected, unselected_marker=marker)


@pytest.mark.parametrize("name,text,expected", _MEMBERSHIP_CASES, ids=[case[0] for case in _MEMBERSHIP_CASES])
def test_arcade_s10_nonempty_null_safe_membership(semantic_graph, arcade_config, evidence_directory,
                                                 name, text, expected):
    assert expected, "An empty-only S10 oracle cannot establish NULL-safe membership semantics"
    _check_semantic_case(semantic_graph, arcade_config, evidence_directory, "membership", name, text, expected)


@pytest.mark.parametrize("name,text,expected", _COALESCE_CASES, ids=[case[0] for case in _COALESCE_CASES])
def test_arcade_s7_falsey_values_are_not_null(semantic_graph, arcade_config, evidence_directory,
                                            name, text, expected):
    _check_semantic_case(semantic_graph, arcade_config, evidence_directory, "coalesce", name, text, expected)


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("complexity", COMPLEXITIES)
def test_arcade_existing_scor_pattern_interfaces(arcade_config, evidence_directory, pattern, seed, complexity):
    _check_pattern_interfaces(arcade_config, evidence_directory, pattern, seed, complexity)
