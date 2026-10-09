"""Protocol and result-integrity checks for ArcadeDB and Apache AGE."""

import json
import socket
from http.client import RemoteDisconnected
from types import SimpleNamespace

import pytest

from executor import ArcadeDB, ApacheAGE, CaseFailure, _agtype_value, _cypher_data, _native_identifier


SETTINGS = {"timeout_seconds": 2.0, "max_rows": 2}


def arcade():
    db = ArcadeDB.__new__(ArcadeDB)
    db.database, db.uri, db.settings = "gsp_test", "http://localhost:2480", SETTINGS
    db._authorization = "Basic redacted"
    db._base_path = ""
    return db


def test_arcade_transport_keeps_duplicate_rows_and_native_types():
    db = arcade()
    calls = []

    def request(path, payload, timeout):
        calls.append((path, payload))
        return {"result": [{"c0": None, "c1": [True, 1, {"v": False}]},
                           {"c0": None, "c1": [True, 1, {"v": False}]}], "_client_elapsed_ms": 1.25}

    db._request = request
    result = db.execute("RETURN null AS c0, [true, 1, {v: false}] AS c1", {}, 2)
    assert result["rows"][0] == result["rows"][1]
    assert result["complete"]
    assert calls[0][0] == "/api/v1/query/gsp_test"
    assert calls[0][1]["limit"] == 3
    assert calls[0][1]["serializer"] == "record"
    assert calls[0][1]["language"] == "opencypher"
    assert result["elapsed_ms"] == 1.25


@pytest.mark.parametrize("envelope", [
    {"result": [{"c0": 1}] * 3},
    {"result": [{"c0": 1}] * 2, "truncated": True},
])
def test_arcade_never_certifies_truncated_results(envelope):
    db = arcade()
    db._request = lambda *args, **kwargs: {**envelope, "_client_elapsed_ms": 1.0}
    assert db.execute("RETURN 1 AS c0", {}, 2)["complete"] is False


def test_arcade_rejects_small_server_cap():
    db = arcade()
    db._request = lambda *args, **kwargs: {"result": [{"c0": 1}], "limit": 1}
    with pytest.raises(CaseFailure, match="arcadedb_server_result_cap_too_small"):
        db.execute("RETURN 1 AS c0", {}, 2)


def test_arcade_network_timeout_does_not_claim_cancellation():
    db = arcade()
    def timed_out(*args, **kwargs):
        raise socket.timeout()
    db._connection = SimpleNamespace(sock=None, connect=timed_out, close=lambda: None)
    with pytest.raises(CaseFailure) as failure:
        db.execute("RETURN 1 AS c0", {}, 2)
    assert failure.value.status == "timeout"
    assert failure.value.stop_target is True
    assert failure.value.details["cancellation_verified"] is False


def test_arcade_connection_reuse_excludes_connect_and_includes_response_decode(monkeypatch):
    import executor
    db = arcade()
    clock = [0.0]
    calls = []
    monkeypatch.setattr(executor, "perf_counter", lambda: clock[0])
    original_decode = json.loads
    def decode(body):
        clock[0] += 0.001
        return original_decode(body)
    monkeypatch.setattr(executor.json, "loads", decode)
    class Response:
        status = 200
        def read(self):
            clock[0] += 0.005
            return b'{"result":[{"connected":1}]}'
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
    first = db.execute("RETURN 1 AS connected", {}, 2)
    second = db.execute("RETURN 1 AS connected", {}, 2)
    assert calls == ["connect", "request", "request"]
    assert first["elapsed_ms"] == pytest.approx(11.0)
    assert second["elapsed_ms"] == pytest.approx(11.0)
    # A deliberately closed response connection is re-established only
    # before submitting the next query and before its timer starts.
    db._connection.sock = None
    third = db.execute("RETURN 1 AS connected", {}, 2)
    assert calls == ["connect", "request", "request", "connect", "request"]
    assert third["elapsed_ms"] == pytest.approx(11.0)


def test_arcade_unknown_disconnect_does_not_retry_submitted_query():
    db = arcade()
    calls = []
    class Connection:
        sock = SimpleNamespace(settimeout=lambda timeout: None)
        def request(self, *args, **kwargs):
            calls.append("request")
        def getresponse(self):
            raise RemoteDisconnected()
        def close(self):
            calls.append("close")
    db._connection = Connection()
    with pytest.raises(CaseFailure) as failure:
        db.execute("RETURN 1 AS connected", {}, 2)
    assert failure.value.status == "environment_error"
    assert failure.value.stop_target is True
    assert calls == ["request", "close"]


def test_arcade_redirect_is_not_followed_or_given_credentials():
    db = arcade()
    calls = []
    class Response:
        status, reason = 302, "Found"
        headers = {"Location": "https://other.example/"}
        def read(self):
            return b'{}'
    class Connection:
        sock = SimpleNamespace(settimeout=lambda timeout: None)
        def request(self, method, path, **kwargs):
            calls.append((method, path))
        def getresponse(self):
            return Response()
    db._connection = Connection()
    with pytest.raises(CaseFailure, match="arcadedb_http_302"):
        db.execute("RETURN 1 AS connected", {}, 2)
    assert calls == [("POST", "/api/v1/query/gsp_test")]


def test_arcade_profile_is_collected_separately():
    db = arcade()
    calls = []
    def query(text, parameters, timeout, **kwargs):
        calls.append(text)
        return {"result": [], "explain": "+ Filter", "explainPlan": {"steps": []}}
    db._query = query
    result = db.explain("RETURN 1 AS c0", {}, analyze=True)
    assert calls == ["PROFILE RETURN 1 AS c0"]
    assert result["plan"]["explain"] == "+ Filter"


def test_arcade_loading_uses_label_and_uid_identity(monkeypatch):
    class Spec:
        pattern = "P1"
        indexes = []
        def schema(self):
            return {"nodes": [{"label": label, "properties": {"uid": "integer"}} for label in ("A", "B")],
                    "relationships": [{"type": "T", "properties": {}}]}
        def iter_batches(self, kind, size):
            if kind == "nodes":
                yield [{"labels": ["A"], "uid": 1, "properties": {"uid": 1}},
                       {"labels": ["B"], "uid": 1, "properties": {"uid": 1}}]
            else:
                yield [{"source_label": "A", "source": 1, "target_label": "B", "target": 1,
                        "type": "T", "properties": {}}]
    monkeypatch.setattr("state.GraphSpec.from_dict", lambda state: Spec())
    db = arcade()
    db._sql = lambda *args, **kwargs: []
    batches = []
    def batch(records):
        batches.append(records)
        if records[0]["@type"] == "vertex":
            return {"verticesCreated": 2, "edgesCreated": 0, "idMapping": {"A:1": "#1:0", "B:1": "#2:0"}}
        return {"verticesCreated": 0, "edgesCreated": 1}
    db._batch = batch
    db.restore({"graph_spec": {}})
    assert batches[1][0]["@from"] == "#1:0"
    assert batches[1][0]["@to"] == "#2:0"


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("null", None), ("true", True), ("false", False),
    ("1", 1), ("1.0", 1.0), ('"null"', "null"),
    ('[null, true, 1, {"k": [2]}]', [None, True, 1, {"k": [2]}]),
])
def test_age_decodes_agtype_without_erasing_types(raw, expected):
    actual = _agtype_value(raw)
    assert actual == expected
    assert type(actual) is type(expected)


@pytest.mark.parametrize("raw", [
    '{"id": 1, "label": "A", "properties": {}}::vertex',
    '{"id": 1}::edge', "[1]::path", "1::numeric", "NaN", "Infinity", 1,
])
def test_age_does_not_strip_unsupported_type_annotations(raw):
    with pytest.raises(CaseFailure):
        _agtype_value(raw)


def age():
    sql = pytest.importorskip("psycopg.sql")
    db = ApacheAGE.__new__(ApacheAGE)
    db.database, db.graph, db.settings = "gsp_test", "gsp_cases", SETTINGS
    db._sql_module, db._columns, db._prepared = sql, {}, {}
    return db


def test_age_sql_wrapper_quotes_query_and_output_names():
    db = age()
    statement = db._cypher_sql("RETURN 'it is safe' AS c0", ("c0",)).as_string()
    assert "ag_catalog.cypher('gsp_cases', $gsp$RETURN 'it is safe' AS c0$gsp$)" in statement
    assert '"c0" agtype' in statement


def test_age_dollar_literal_cannot_be_closed_by_query_text():
    db = age()
    query = "RETURN '$gsp$); SELECT secret FROM other; --' AS c0"
    statement = db._cypher_sql(query, ("c0",)).as_string()
    assert "$gsp_$" + query + "$gsp_$" in statement


def test_age_parameters_use_prepared_agtype_map():
    db = age()
    calls = []
    db._sql = lambda statement, parameters=None: calls.append(statement.as_string())
    statement = db._statement("RETURN $v AS c0", {"v": [False, 1]}, ("c0",)).as_string()
    assert len(calls) == 1
    assert "(agtype) AS SELECT" in calls[0] and ", $1)" in calls[0]
    assert "EXECUTE" in statement and json.dumps({"v": [False, 1]}) in statement
    db._statement("RETURN $v AS c0", {"v": [True, 2]}, ("c0",))
    assert len(calls) == 1


def test_age_timeout_requires_postgresql_acknowledgement_and_health():
    db = age()
    class Cancelled(Exception):
        sqlstate = "57014"
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            raise Cancelled("statement timeout")
    db.connection = SimpleNamespace(cursor=Cursor)
    db._sql = lambda *args, **kwargs: []
    db.health = lambda: True
    with pytest.raises(CaseFailure) as failure:
        db._run("RETURN 1 AS c0", {}, 2, ("c0",))
    assert failure.value.status == "timeout"
    assert failure.value.stop_target is False
    assert failure.value.details["cancellation_verified"] is True


def test_age_failed_health_stops_target_after_cancel():
    db = age()
    class Cancelled(Exception):
        sqlstate = "57014"
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            raise Cancelled()
    db.connection = SimpleNamespace(cursor=Cursor)
    db._sql = lambda *args, **kwargs: []
    db.health = lambda: False
    with pytest.raises(CaseFailure) as failure:
        db._run("RETURN 1 AS c0", {}, 2, ("c0",))
    assert failure.value.stop_target is True
    assert failure.value.details["cancellation_verified"] is False


def test_age_duplicate_rows_and_extra_probe_preserve_incomplete_result():
    db = age()
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            pass
        def fetchmany(self, count):
            assert count == 3
            return [("null", "true"), ("null", "true"), ("null", "true")]
    db.connection = SimpleNamespace(cursor=Cursor)
    db._sql = lambda *args, **kwargs: []
    result = db._run("RETURN null AS c0, true AS c1", {}, 2, ("c0", "c1"))
    assert result["rows"] == [{"c0": None, "c1": True}] * 2
    assert result["complete"] is False


def test_age_wrong_width_cannot_silently_lose_projected_columns():
    db = age()
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            pass
        def fetchmany(self, count):
            return [("1",)]
    db.connection = SimpleNamespace(cursor=Cursor)
    db._sql = lambda *args, **kwargs: []
    with pytest.raises(CaseFailure, match="age_result_column_width_mismatch"):
        db._run("RETURN 1 AS c0, 2 AS c1", {}, 2, ("c0", "c1"))


def test_native_schema_identifiers_cannot_be_arbitrary_statements():
    with pytest.raises(CaseFailure):
        _native_identifier("A; DROP GRAPH other")
    assert _cypher_data([{ "uid": 1, "keep": False }]) == "[{uid: 1, keep: false}]"


def test_age_reports_gin_map_and_btree_expression_as_distinct_native_indexes():
    db = age()
    spec = SimpleNamespace(indexes=[{"name": "gsp_cost_lookup", "label": "Low", "property": "id"}])
    def query(statement, parameters):
        name = parameters[1]
        definition = ("CREATE INDEX gsp_cost_lookup ON graph.Low USING gin (properties)" if name == "gsp_cost_lookup"
                      else "CREATE INDEX gsp_cost_lookup_scalar ON graph.Low USING btree "
                           "(agtype_access_operator(VARIADIC ARRAY[properties, '\"id\"'::agtype]))")
        return [(True, True, "Low", definition)]
    db._sql = query
    evidence = db._check_indexes(spec)
    assert [item["kind"] for item in evidence] == ["GIN_property_map", "BTREE_property_expression"]
    assert [item["includes_whole_property_map"] for item in evidence] == [True, False]


def test_age_rejects_btree_on_another_property():
    db = age()
    spec = SimpleNamespace(indexes=[{"name": "gsp_cost_lookup", "label": "Low", "property": "id"}])
    def query(statement, parameters):
        definition = ("CREATE INDEX base ON graph.Low USING gin (properties)" if parameters[1] == "gsp_cost_lookup"
                      else "CREATE INDEX scalar ON graph.Low USING btree "
                           "(agtype_access_operator(VARIADIC ARRAY[properties, '\"uid\"'::agtype]))")
        return [(True, True, "Low", definition)]
    db._sql = query
    with pytest.raises(CaseFailure, match="age_cost_index_wrong_property"):
        db._check_indexes(spec)
