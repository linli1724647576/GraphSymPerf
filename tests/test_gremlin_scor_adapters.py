"""SCor execution-entry and HTTP contracts without a running provider.

The request boundary is stubbed: these tests cannot establish native traversal
semantics. Each installed provider still needs the separate live interface gate.
"""

import json
from types import SimpleNamespace

import pytest

import executor
from executor import ApacheHugeGraph, GremlinHTTP, JanusGraph, run_case, typed_bag
from main import prepare_legacy_case as prepare_case
from test_executor import FakeDatabase, SETTINGS


TARGETS = (("janusgraph", JanusGraph), ("hugegraph", ApacheHugeGraph))
VERIFIED = ("gremlin_null_projection",)


@pytest.mark.parametrize("target,adapter", TARGETS)
@pytest.mark.parametrize("pattern", [f"S{i}" for i in range(1, 11)])
def test_existing_scor_cases_reach_gremlin_execution_with_checked_columns_and_bindings(
        monkeypatch, target, adapter, pattern):
    database = {"dbms": target, "language": "gremlin", "verified_features": VERIFIED}
    # Seed 17 / complexity 2 exercises checked dynamic guards and nested calls;
    # the compiler matrix in test_gremlin_scor covers the finite simple forms.
    case = prepare_case(None, pattern, 3, 17, 11, complexity=2, database=database)
    assert "compile_error" not in case
    assert case["target_dbms"] == target and case["reference_side"] == "b"
    assert case["query_a"].replace("'GSP_SC_HIGH'", "'GSP_SC_REF'") == case["query_b"]

    class Database(FakeDatabase):
        instances = []

        def check_state(self, supplied):
            assert supplied is case
            return {"checks": [], "indexes": []}

        def execute(self, query, parameters, timeout):
            self.calls.append(query)
            side = "a" if query == case["query_a"] else "b"
            assert query == case[f"query_{side}"] and query.startswith("g.inject(1)")
            assert parameters == {**case["parameters"], **case["query_bindings"][side]}
            rows = [{physical: row[logical] for physical, logical in case["output_columns"][side]}
                    for row in case["expected_rows"]]
            return {"rows": rows, "complete": True, "elapsed_ms": 1.0}

    monkeypatch.setattr(executor, "make_database", Database)
    report = run_case(case, database, SETTINGS)
    assert report["status"] == "no_candidate", report
    assert report["result_rows"] == len(case["expected_rows"])
    assert report["pair_kind"] == "explicit_traversal"
    assert report["discovery"]["samples"]
    assert all(db.calls and db.closed for db in Database.instances)


@pytest.mark.parametrize("target,adapter", TARGETS)
@pytest.mark.parametrize("data,expected", [
    ([], []),
    ([{"c0": None}, {"c0": False}, {"c0": 0}, {"c0": ""}, {"c0": False}],
     [{"c0": None}, {"c0": False}, {"c0": 0}, {"c0": ""}, {"c0": False}]),
])
def test_http_empty_and_falsey_scor_rows_remain_complete_typed_bags(target, adapter, data, expected):
    # HugeGraph's 3.5.1 NULL entity sentinel is a compiler-internal detail;
    # genuine scalar FALSE must survive the common HTTP result decoder.
    db = adapter.__new__(adapter)
    db.database, db.settings = "gsp_test", {"timeout_seconds": 2.0, "max_rows": 10}
    db._endpoint, db._headers, db._traversal_source = "/gremlin", {}, "g"
    db._graph_binding = "graph"
    calls = []

    class Response:
        status, reason = 200, "OK"

        def getheader(self, name, default=None):
            return default

        def read(self):
            # JanusGraph exposes typed GraphSON; HugeGraph also permits plain
            # scalar maps. Both representations carry an actual NULL value.
            raw = ([{"@type": "g:Map", "@value": ["c0", row["c0"]]} for row in data]
                   if target == "janusgraph" else data)
            return json.dumps({"status": {"code": 200}, "result": {"data": raw}}).encode()

    db._connection = SimpleNamespace(
        sock=SimpleNamespace(settimeout=lambda timeout: None),
        request=lambda *args, **kwargs: calls.append((args, kwargs)), getresponse=Response)
    result = GremlinHTTP.execute(db, "g.inject(1).project('c0')", {}, 2.0)
    assert result["complete"] is True and typed_bag(result["rows"]) == typed_bag(expected)
    assert len(calls) == 1
    sent = json.loads(calls[0][1]["body"])
    assert sent["gremlin"].endswith(".limit(11).toList()")
