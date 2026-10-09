"""Gremlin transport integrity and language-independent graph-state checks."""

import json
import socket
from http.client import RemoteDisconnected
from types import SimpleNamespace

import pytest

from executor import (ApacheHugeGraph, CaseFailure, GremlinHTTP, JanusGraph,
                      _graphson_value, _gremlin_data, _gremlin_state_checks,
                      )
from state import GraphSpec


SETTINGS = {"timeout_seconds": 2.0, "max_rows": 2}


def adapter(kind=GremlinHTTP):
    db = kind.__new__(kind)
    db.database, db.settings = "gsp_test", SETTINGS
    db._headers = {"Content-Type": "application/json", "Accept": db.mime}
    db._graph_binding, db._traversal_source = "graph", "g"
    db._endpoint = "/gremlin"
    return db


def envelope(data, code=200):
    return {"status": {"code": code, "message": ""}, "result": {"data": data}}


@pytest.mark.parametrize("raw, expected", [
    (None, None), (False, False), (1, 1),
    ({"@type": "g:Int32", "@value": 1}, 1),
    ({"@type": "g:Int64", "@value": 9007199254740993}, 9007199254740993),
    ({"@type": "g:Double", "@value": 1}, 1.0),
    ({"@type": "g:List", "@value": [True, {"@type": "g:Int64", "@value": 1}, None]}, [True, 1, None]),
    ({"@type": "g:Map", "@value": ["c0", False, "c1", {"@type": "g:List", "@value": [1, None]}]},
     {"c0": False, "c1": [1, None]}),
])
def test_graphson_keeps_scalar_container_types(raw, expected):
    actual = _graphson_value(raw)
    assert actual == expected
    assert type(actual) is type(expected)


@pytest.mark.parametrize("value", [
    {"@type": "g:Vertex", "@value": {"id": 1}},
    {"@type": "g:Edge", "@value": {"id": 1}},
    {"@type": "g:Path", "@value": {}},
    {"@type": "g:Set", "@value": [1]},
    {"@type": "g:Int64", "@value": True},
    {"@type": "g:Map", "@value": ["a", 1, "a", 2]},
    {"@type": "g:Map", "@value": [1, "a"]},
    float("nan"), float("inf"),
])
def test_graphson_does_not_erase_entity_or_invalid_types(value):
    with pytest.raises(CaseFailure) as failure:
        _graphson_value(value)
    assert failure.value.status == "incomplete_result"


def test_top_level_traverser_bulk_preserves_duplicates_and_is_bounded():
    data = [{"@type": "g:Traverser", "@value": {
        "bulk": {"@type": "g:Int64", "@value": 1_000_000},
        "value": {"@type": "g:Map", "@value": ["c0", 1]}}}]
    assert _gremlin_data(envelope(data), max_items=3) == [{"c0": 1}] * 3


@pytest.mark.parametrize("bulk", [False, -1, 0, 1.5, None])
def test_invalid_bulk_does_not_certify_a_bag(bulk):
    with pytest.raises(CaseFailure, match="invalid_gremlin_bulk"):
        _gremlin_data(envelope([{"@type": "g:Traverser", "@value": {"bulk": bulk, "value": {"c0": 1}}}]))


def test_native_http_script_can_return_scalar_but_query_requires_array():
    assert _gremlin_data(envelope("1.1.0"), require_list=False) == ["1.1.0"]
    with pytest.raises(CaseFailure, match="gremlin_response_data_not_a_list"):
        _gremlin_data(envelope({"c0": 1}))


def test_transport_appends_extra_row_probe_without_deduplicating():
    db, calls = adapter(), []
    def request(script, parameters, timeout):
        calls.append((script, parameters, timeout))
        return envelope([{"c0": False, "c1": [1, None]}] * 3), 1.0
    db._request = request
    result = db.execute('g.inject(false).project("c0").by(__.identity())', {"bound": True}, 2)
    assert len(result["rows"]) == 2
    assert result["rows"][0] == result["rows"][1]
    assert result["complete"] is False
    assert calls[0][0].endswith(".limit(3).toList()")
    assert calls[0][1] == {"bound": True}


def test_projected_rows_are_required():
    db = adapter()
    db._request = lambda *args: (envelope([1, 2]), 1.0)
    with pytest.raises(CaseFailure, match="gremlin_projection_must_be_a_map"):
        db.execute("g.inject(1,2)", {}, 2)


def test_native_profile_is_preserved_without_db_hits_alias():
    db, calls = adapter(), []
    raw = {"@type": "g:TraversalMetrics", "@value": {"duration": 0.123, "metrics": []}}
    db._request = lambda query, parameters, timeout: (calls.append(query) or envelope([raw]), 1.0)
    result = db.explain("g.V().project('c0').by('uid')", {}, analyze=True)
    assert calls == ["(g.V().project('c0').by('uid')).profile().next()"]
    assert result["plan"] == [raw]
    assert "DB Hits" not in json.dumps(result)


def test_network_timeout_does_not_claim_cancellation_or_replay():
    db, calls = adapter(), []
    def request(*args, **kwargs):
        calls.append("request")
        raise socket.timeout()
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                     request=request, close=lambda: calls.append("close"))
    with pytest.raises(CaseFailure) as failure:
        db.execute("g.inject(1).project('c0').by(__.identity())", {}, 2)
    assert failure.value.status == "timeout"
    assert failure.value.stop_target is True
    assert failure.value.details["cancellation_verified"] is False
    assert calls == ["request", "close"]


def test_disconnect_after_submission_is_not_retried():
    db, calls = adapter(), []
    def disconnected():
        raise RemoteDisconnected()
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                     request=lambda *args, **kwargs: calls.append("request"),
                                     getresponse=disconnected, close=lambda: calls.append("close"))
    with pytest.raises(CaseFailure) as failure:
        db.execute("g.inject(1).project('c0').by(__.identity())", {}, 2)
    assert failure.value.status == "environment_error" and failure.value.stop_target
    assert calls == ["request", "close"]


def test_connection_establishment_excluded_response_decode_included(monkeypatch):
    import executor
    db, clock, calls = adapter(), [0.0], []
    monkeypatch.setattr(executor, "perf_counter", lambda: clock[0])
    decode = json.loads
    def loads(body):
        clock[0] += 0.001
        return decode(body)
    monkeypatch.setattr(executor.json, "loads", loads)
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return default
        def read(self):
            clock[0] += 0.005
            return b'{"status":{"code":200},"result":{"data":[{"c0":1}]}}'
    class Connection:
        sock = None
        def connect(self):
            calls.append("connect")
            clock[0] += 100.0
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
        def request(self, *args, **kwargs):
            calls.append("request")
            clock[0] += 0.002
        def getresponse(self):
            clock[0] += 0.003
            return Response()
    db._connection = Connection()
    first = db.execute("g.inject(1).project('c0').by(__.identity())", {}, 2)
    second = db.execute("g.inject(1).project('c0').by(__.identity())", {}, 2)
    assert calls == ["connect", "request", "request"]
    assert first["elapsed_ms"] == pytest.approx(11.0)
    assert second["elapsed_ms"] == pytest.approx(11.0)


@pytest.mark.parametrize("code,message,status", [(598, "evaluation timed out", "timeout"),
                                                  (599, "backend unavailable", "query_error"),
                                                  (500, "No signature of method", "unsupported_feature")])
def test_server_status_does_not_guess_timeout_from_all_errors(code, message, status):
    db = adapter()
    class Response:
        status, reason = 500, "Server Error"
        def getheader(self, name, default=None):
            return default
        def read(self):
            value = envelope([], code)
            value["status"]["message"] = message
            return json.dumps(value).encode()
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                     request=lambda *args, **kwargs: None, getresponse=Response)
    with pytest.raises(CaseFailure) as failure:
        db.execute("g.inject(1).project('c0').by(__.identity())", {}, 2)
    assert failure.value.status == status
    if status == "timeout":
        assert failure.value.stop_target and not failure.value.details["cancellation_verified"]


def test_structured_checks_render_null_as_absent_property_and_keep_reference_subset_directions():
    spec = GraphSpec("P15", 2, 7)
    rendered = _gremlin_state_checks(spec.state_checks())
    null_witness = next(check for check in rendered if check["expected_rows"] == [{"uid": 3, "keep": None}])
    assert 'coalesce(__.values("keep"), __.constant(null))' in null_witness["query"]
    assert not any("MATCH" in check["query"] or "CREATE" in check["query"] for check in rendered)
    references = [check for check in rendered if "other.get().inVertex().id()" in check["query"]]
    assert len(references) == 2
    assert 'hasLabel("GSP_REFERENCE")' in references[0]["query"]
    assert 'outE("GSP_EXPAND")' in references[0]["query"]
    assert '.not(__.has("keep", true))' in references[1]["query"]


def test_inverse_edge_check_uses_pair_key_and_exactly_one_reverse_edge():
    checks = _gremlin_state_checks(GraphSpec("P2", 2, 9).state_checks())
    inverse = [check for check in checks if "f.outVertex().id()" in check["query"]]
    assert len(inverse) == 2
    assert all('f.value("pair")' in check["query"] and "next() != 1" in check["query"] for check in inverse)


def test_single_label_rejection_precedes_graph_reset():
    db, calls = adapter(), []
    db._reset = lambda: calls.append("reset")
    with pytest.raises(CaseFailure, match="gremlin_single_vertex_label"):
        db.restore({"graph_spec": GraphSpec("P7", 2, 0).to_dict()})
    assert calls == []


def test_graph_loading_streams_recipe_and_assigns_parallel_edge_ordinals(monkeypatch):
    db, batches = adapter(ApacheHugeGraph), []
    db._reset = lambda: None
    db._prepare_schema = lambda spec: None
    db._check_indexes = lambda spec: []
    def submit(script, parameters):
        rows = parameters["gsp_rows"]
        batches.append((script, rows))
        return [len(rows)], 1.0
    db._script = submit
    db.restore({"graph_spec": GraphSpec("P2", 2, 9).to_dict()})
    nodes = [row for _, batch in batches for row in batch if "labels" in row]
    edges = [row for _, batch in batches for row in batch if "type" in row]
    assert len(nodes) == 6 and len(edges) == 6
    assert [row["ordinal"] for row in edges] == list(range(6))
    assert all(type(row["properties"]["pair"]) is int for row in edges)
    assert "r.source_label+':'+r.source" in db._edge_script()
    assert "r.target_label+':'+r.target" in db._edge_script()
    assert "'_gsp_edge_ordinal'" in db._edge_script()


@pytest.mark.parametrize("pattern", ["P3", "P12"])
def test_hugegraph_multi_endpoint_relationship_rejected_before_data_reset(pattern):
    db, calls = adapter(ApacheHugeGraph), []
    db._reset = lambda: calls.append("reset")
    with pytest.raises(CaseFailure, match="hugegraph_multi_endpoint_relationship_type"):
        db.restore({"graph_spec": GraphSpec(pattern, 2, 0).to_dict()})
    assert calls == []


@pytest.mark.parametrize("kind,fields", [(JanusGraph, {"keys": ["id"], "label": "Wrong", "state": ["ENABLED"], "composite": True}),
                                         (JanusGraph, {"keys": ["id"], "label": "GspLow", "state": ["REGISTERED"], "composite": True}),
                                         (ApacheHugeGraph, {"keys": ["uid"], "label": "GspLow", "status": "CREATED", "kind": "SECONDARY"}),
                                         (ApacheHugeGraph, {"keys": ["id"], "label": "GspLow", "status": "CREATING", "kind": "SECONDARY"})])
def test_native_index_must_match_requested_key_label_and_ready_state(kind, fields):
    db = adapter(kind)
    db._script = lambda *args, **kwargs: ([fields], 1.0)
    with pytest.raises(CaseFailure) as failure:
        db._check_indexes(GraphSpec("P1", 2, 7))
    assert failure.value.status == "cost_check_failed"


def test_gzip_response_preserves_null_types_duplicates_and_completion():
    import gzip
    db = adapter(ApacheHugeGraph)
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return "gzip" if name == "Content-Encoding" else default
        def read(self):
            return gzip.compress(json.dumps(envelope([{"c0": None, "c1": [False, 1]}] * 2)).encode())
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                     request=lambda *args, **kwargs: None, getresponse=Response)
    result = db.execute("g.inject(1).project('c0')", {}, 2)
    assert result["rows"] == [{"c0": None, "c1": [False, 1]}] * 2
    assert result["complete"] is True


def test_malformed_gzip_response_stops_unknown_execution_without_retry():
    db, calls = adapter(ApacheHugeGraph), []
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return "gzip" if name == "Content-Encoding" else default
        def read(self):
            return b"not a gzip response"
    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
                                     request=lambda *args, **kwargs: calls.append("request"),
                                     getresponse=Response, close=lambda: calls.append("close"))
    with pytest.raises(CaseFailure) as failure:
        db.execute("g.inject(1).project('c0')", {}, 2)
    assert failure.value.status == "environment_error" and failure.value.stop_target is True
    assert calls == ["request", "close"]


def test_hugegraph_population_checks_do_not_require_extra_experiment_indexes():
    db = adapter(ApacheHugeGraph)
    spec = GraphSpec("P15", 2, 7)
    calls = []
    rendered = _gremlin_state_checks(spec.state_checks())
    expected = iter(check["expected_rows"] for check in rendered)
    def execute(query, parameters, timeout):
        calls.append(query)
        return {"rows": next(expected), "complete": True, "elapsed_ms": 1.0}
    db.execute = execute
    db._check_indexes = lambda spec: []
    db.check_state({"graph_spec": spec.to_dict()})
    assert any('g.V("GspAnchor:3")' in query for query in calls)
    root_queries = [query for query in calls if query.startswith("g.V()")]
    assert all('.has("uid"' not in query for query in root_queries)
    assert any('v.property("keep").isPresent()' in query for query in calls)


@pytest.mark.parametrize("pattern", ["P1", "P2", "P4", "P15"])
def test_scan_population_checks_keep_structural_expected_results_and_local_edge_filters(pattern):
    checks = GraphSpec(pattern, 1000, 17).state_checks()
    original = _gremlin_state_checks(checks)
    scanned = _gremlin_state_checks(checks, scan_edges=True)
    assert [item["expected_rows"] for item in scanned] == [item["expected_rows"] for item in original]
    edge_queries = [item["query"] for item in scanned if item["query"].startswith("g.E()")]
    assert edge_queries and all(query.startswith("g.E().filter{ t -> t.get().label() == ") for query in edge_queries)
    # Both inverse and reference checks retain the endpoint/pair validation;
    # disabling the global label-index root does not discard local relations.
    if pattern == "P2":
        assert sum('f.value("pair")' in query for query in edge_queries) == 2
    if pattern in {"P4", "P15"}:
        assert sum("other.get().inVertex().id() == e.inVertex().id()" in query for query in edge_queries) == 2


def test_hugegraph_state_checks_do_not_request_global_edge_label_index(monkeypatch):
    db = adapter(ApacheHugeGraph)
    spec = GraphSpec("P2", 1000, 17)
    expected = iter(check["expected_rows"] for check in _gremlin_state_checks(spec.state_checks()))
    def execute(query, parameters, timeout):
        if query.startswith("g.E().hasLabel("):
            raise CaseFailure("query_error", "edge_cache_query_id_exceeds_16k")
        return {"rows": next(expected), "complete": True, "elapsed_ms": 1.0}
    db.execute = execute
    db._check_indexes = lambda spec: []
    db.check_state({"graph_spec": spec.to_dict()})
