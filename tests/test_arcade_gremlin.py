"""Native ArcadeDB Gremlin protocol, Java grammar and archive contracts."""

from copy import deepcopy
from dataclasses import replace
from http.client import RemoteDisconnected
import json
import socket
from types import SimpleNamespace

import pytest

import executor
import main
from executor import ArcadeDB, ArcadeDBGremlin, CaseFailure, _gremlin_state_checks, supported_patterns
from ir import Alias, Literal, QueryError, to_data
from languages import generate, render_saved
from query import parse_query
from state import GraphSpec


TARGET = {"dbms": "arcadedb", "language": "gremlin", "verified_features": ["gremlin_null_projection"]}
SETTINGS = {"timeout_seconds": 2.0, "max_rows": 2}


def adapter():
    db = ArcadeDBGremlin.__new__(ArcadeDBGremlin)
    db.database, db.uri, db.settings = "gsp_test", "http://localhost:2488", SETTINGS
    db._authorization, db._base_path = "Basic redacted", ""
    return db


def test_language_selects_native_gremlin_adapter_without_changing_cypher(monkeypatch):
    monkeypatch.setattr(ArcadeDB, "__init__", lambda self, config, settings: None)
    assert type(executor.make_database(TARGET, SETTINGS)) is ArcadeDBGremlin
    assert type(executor.make_database({"dbms": "arcadedb"}, SETTINGS)) is ArcadeDB
    assert supported_patterns(TARGET) == ({f"P{i}" for i in range(1, 16)} - {"P3", "P7", "P13"}) | {f"S{i}" for i in range(1, 11)}


def test_native_request_preserves_bindings_typed_duplicate_rows_and_extra_row_cap():
    db, calls = adapter(), []
    row = {"c0": None, "c1": [True, 1, 1.0, {"v": False}]}
    def request(path, payload, timeout):
        calls.append((path, payload, timeout))
        return {"result": [row, row], "returned": 2, "limit": 3, "truncated": False,
                "_client_elapsed_ms": 1.25}
    db._request = request
    query = "g.inject(1).is(P.eq(_gsp_p0)).project('c0').by(__.identity())"
    result = db.execute(query, {"_gsp_p0": 1}, 2)
    assert result == {"rows": [row, row], "complete": True, "elapsed_ms": 1.25}
    assert calls == [("/api/v1/query/gsp_test", {"language": "gremlin", "command": query,
                     "params": {"_gsp_p0": 1}, "limit": 3, "serializer": "record"}, 2)]


@pytest.mark.parametrize("envelope,reason", [
    ({"result": [1]}, "arcadedb_invalid_result_rows"),
    ({"result": None}, "arcadedb_invalid_result_rows"),
    ({"result": [{"c0": float("nan")}]}, "unsupported_result_type"),
    ({"result": [], "truncated": "false"}, "arcadedb_invalid_truncation_flag"),
    ({"result": [], "returned": 1}, "arcadedb_returned_count_mismatch"),
    ({"result": [{"c0": 1}], "returned": True}, "arcadedb_returned_count_mismatch"),
    ({"result": [], "limit": True}, "arcadedb_invalid_result_cap"),
])
def test_invalid_native_results_never_certify_equivalence(envelope, reason):
    db = adapter()
    db._request = lambda *args: {**envelope, "_client_elapsed_ms": 1.0}
    with pytest.raises(CaseFailure, match=reason) as failure:
        db.execute("g.inject(1)", {}, 2)
    assert failure.value.status == "incomplete_result"


@pytest.mark.parametrize("envelope", [
    {"result": [{"c0": 1}] * 3, "returned": 3, "limit": 3, "truncated": False},
    {"result": [{"c0": 1}] * 2, "returned": 2, "limit": 3, "truncated": True},
])
def test_truncated_result_is_incomplete_even_when_rows_are_equal(envelope):
    db = adapter()
    db._request = lambda *args: {**envelope, "_client_elapsed_ms": 1.0}
    result = db.execute("g.inject(1,1,1)", {}, 2)
    assert result["complete"] is False and len(result["rows"]) == 2


def test_smaller_server_cap_stops_target():
    db = adapter()
    db._request = lambda *args: {"result": [], "limit": 1, "_client_elapsed_ms": 1.0}
    with pytest.raises(CaseFailure, match="arcadedb_server_result_cap_too_small") as failure:
        db.execute("g.V()", {}, 2)
    assert failure.value.stop_target


def test_sql_loader_and_schema_queries_keep_the_sql_language():
    db, calls = adapter(), []
    db._request = lambda path, payload, timeout: calls.append((path, payload)) or {"result": []}
    db._sql("CREATE VERTEX TYPE GspProbe", command=True)
    db._sql("SELECT name, type FROM schema:types")
    assert [path for path, _ in calls] == ["/api/v1/command/gsp_test", "/api/v1/query/gsp_test"]
    assert [payload["language"] for _, payload in calls] == ["sql", "sql"]


def test_profile_is_separate_native_detailed_request_and_keeps_units():
    db, calls = adapter(), []
    plan = {"type": "GremlinExecutionPlan", "cost": 9000,
            "steps": [{"name": "InjectStep", "elements": 1, "traversers": 1, "cost": 9000}]}
    def request(path, payload, timeout):
        calls.append((path, payload))
        return {"result": [{"c0": 1}], "explain": "Traversal Metrics", "explainPlan": plan,
                "profile": {"totalElapsed": 99}}
    db._request = request
    result = db.explain("g.inject(1).project('c0').by(__.identity())", {}, analyze=True)
    assert calls[0][0] == "/api/v1/query/gsp_test"
    assert calls[0][1]["profileExecution"] == "detailed"
    assert result["plan"] == {"explain": "Traversal Metrics", "explainPlan": plan}
    assert "DB Hits" not in json.dumps(result)


def test_generic_http_profile_does_not_masquerade_as_gremlin_plan():
    db = adapter()
    db._request = lambda *args: {"result": [], "profile": {"totalElapsed": 99}}
    with pytest.raises(CaseFailure, match="arcadedb_gremlin_execution_plan_unavailable"):
        db.explain("g.V()", {}, analyze=True)


def test_truncated_profile_is_not_complete_evidence():
    db = adapter()
    db._request = lambda *args: {"result": [], "truncated": True, "explain": "Traversal Metrics"}
    with pytest.raises(CaseFailure, match="arcadedb_gremlin_profile_truncated"):
        db.explain("g.V()", {}, analyze=True)


def test_terminal_explain_uses_native_command_endpoint():
    db, calls = adapter(), []
    def request(path, payload, timeout):
        calls.append((path, payload))
        return {"result": [{"result": "Traversal Explanation\nFinal Traversal"}]}
    db._request = request
    result = db.explain("g.V()", {"p": 1}, analyze=False)
    assert calls[0][0] == "/api/v1/command/gsp_test"
    assert calls[0][1]["command"] == "g.V().explain()"
    assert calls[0][1]["params"] == {"p": 1}
    assert result["format"] == "arcadedb_gremlin_native" and result["analyze"] is False


@pytest.mark.parametrize("error,status", [(socket.timeout(), "timeout"),
                                          (RemoteDisconnected(), "environment_error")])
def test_unknown_submission_outcome_is_never_retried(error, status):
    db, calls = adapter(), []
    def response():
        raise error
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                    request=lambda *args, **kwargs: calls.append("request"),
                                    getresponse=response, close=lambda: calls.append("close"))
    with pytest.raises(CaseFailure) as failure:
        db.execute("g.V()", {}, 2)
    assert failure.value.status == status and failure.value.stop_target
    assert calls == ["request", "close"]
    if status == "timeout":
        assert failure.value.details["cancellation_verified"] is False


def test_native_timer_excludes_connect_and_includes_result_read_and_json_decode(monkeypatch):
    db, clock, calls = adapter(), [0.0], []
    monkeypatch.setattr(executor, "perf_counter", lambda: clock[0])
    decode = json.loads
    def loads(body):
        clock[0] += 0.001
        return decode(body)
    monkeypatch.setattr(executor.json, "loads", loads)
    class Response:
        status = 200
        def read(self):
            clock[0] += 0.005
            return b'{"result":[{"c0":1}],"returned":1,"limit":3,"truncated":false}'
    class Connection:
        sock = None
        def connect(self):
            calls.append("connect")
            clock[0] += 100
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
        def request(self, *args, **kwargs):
            calls.append("request")
            clock[0] += 0.002
        def getresponse(self):
            clock[0] += 0.003
            return Response()
    db._connection = Connection()
    for _ in range(2):
        assert db.execute("g.V()", {}, 2)["elapsed_ms"] == pytest.approx(11.0)
    assert calls == ["connect", "request", "request"]


@pytest.mark.parametrize("value,literal", [(None, "null"), (True, "true"), (False, "false"),
                                           (-(2**63), "-9223372036854775808L"),
                                           (2**63 - 1, "9223372036854775807L"),
                                           (1, "1L"), (1.0, "1.0D"), (1.5, "1.5D"), (-0.0, "-0.0D")])
def test_java_constant_literal_lowering_preserves_scalar_types(value, literal):
    tree = replace(parse_query("RETURN 1 AS value"), returns=(Alias(Literal(value), "value"),))
    compiled = generate(tree, "arcadedb", "gremlin", verified_features=TARGET["verified_features"])
    assert compiled.text.endswith(f".by(__.constant({literal}))")
    assert compiled.bindings == {}
    assert render_saved(tree, "arcadedb", "gremlin") == compiled


def test_java_string_literals_escape_quotes_controls_and_disable_interpolation():
    value = "${System.exit(1)}'; g.V().drop(); //\n\"\\\t中文"
    tree = replace(parse_query("RETURN 1 AS value"), returns=(Alias(Literal(value), "value"),))
    compiled = generate(tree, "arcadedb", "gremlin")
    assert "constant('${" in compiled.text
    assert "\\';" in compiled.text and "\\n" in compiled.text and "\\t" in compiled.text
    assert "\\u4e2d\\u6587" in compiled.text
    assert value not in compiled.text and "\n" not in compiled.text
    # Other providers retain their original parameterized compiler contract.
    other = generate(tree, "janusgraph", "gremlin")
    assert value in other.bindings.values() and "System.exit" not in other.text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**63, -(2**63)-1, []])
def test_invalid_literals_are_rejected_before_java_rendering(value):
    tree = replace(parse_query("RETURN 1 AS value"), returns=(Alias(Literal(value), "value"),))
    with pytest.raises(QueryError):
        generate(tree, "arcadedb", "gremlin")


def test_java_predicate_values_keep_native_bindings():
    compiled = generate(parse_query("MATCH (u:A {uid:7}) WHERE u.keep = true RETURN u.uid, 1 AS n"),
                        "arcadedb", "gremlin", verified_features=TARGET["verified_features"])
    assert compiled.bindings == {"_gsp_p0": 7, "_gsp_p1": True}
    assert ".has('uid',P.eq(_gsp_p0))" in compiled.text
    assert ".is(P.eq(_gsp_p1))" in compiled.text and ".constant(1L)" in compiled.text


@pytest.mark.parametrize("pattern", ["P2", "P4", "P5", "P15"])
def test_java_state_checks_use_traversal_steps_and_preserve_expected_checks(pattern):
    checks = GraphSpec(pattern, 2, 17).state_checks()
    original, native = _gremlin_state_checks(checks), _gremlin_state_checks(checks, lambda_free=True)
    assert [check["expected_rows"] for check in native] == [check["expected_rows"] for check in original]
    assert all("{" not in check["query"] and "next()" not in check["query"] for check in native)
    assert any("{" in check["query"] for check in original)


def literal_case(value=1):
    a = replace(parse_query("RETURN 1 AS value"), returns=(Alias(Literal(value), "value"),))
    b = parse_query("RETURN coalesce(null, 1) AS value")
    return main.compile_case({"pattern": "P1", "seed": 7, "scale": 1, "setup": [], "checks": [],
                              "parameters": {}, "expected_rows": [{"value": value}],
                              "query_ir": {"a": to_data(a), "b": to_data(b)}}, TARGET)


def test_native_literal_archive_roundtrips_and_type_changes_affect_identity(tmp_path):
    case = literal_case()
    main.save_case(tmp_path, case, SETTINGS, {"status": "no_candidate"})
    restored, _ = main.read_case(tmp_path)
    assert restored == case and restored.get("query_bindings", {}) == {}
    assert len({main.case_digest(literal_case(v)) for v in (True, 1, 1.0)}) == 3
    record = json.loads((tmp_path / "result.json").read_text())
    changed = deepcopy(case)
    changed["query_a"] = changed["query_a"].replace("constant(1L)", "constant(2L)")
    record["input_hash"] = main.case_digest(changed)
    record["extension_hash"] = main.extension_digest(changed)
    (tmp_path / "result.json").write_text(json.dumps(record))
    (tmp_path / "query_a.gremlin").write_text(changed["query_a"] + ";\n")
    with pytest.raises(ValueError, match="compiled query differs"):
        main.read_case(tmp_path)
