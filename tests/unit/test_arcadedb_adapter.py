from __future__ import annotations

from types import SimpleNamespace

from graph_sym_perf.adapters.arcadedb import ArcadeDBAdapter, _arcade_plan_payload, _arcade_rows
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.plan import normalize_plan


def test_arcadedb_rows_use_result_payload() -> None:
    rows = _arcade_rows({"result": [{"a": 1}, ["x", 2], "value"]})

    assert rows == ({"a": 1}, {"col1": "x", "col2": 2}, {"value": "value"})


def test_arcadedb_profile_payload_is_normalized() -> None:
    payload = {"result": [{"operator": "Node By Label Scan", "rows": 5}, {"operator": "Filter", "rows": 2}]}

    plan = normalize_plan(TargetSystem.ARCADEDB, _arcade_plan_payload(payload))

    assert [node.operator_family for node in plan.nodes] == ["Scan", "Filter"]
    assert plan.metrics["rows"] == 7.0


def test_arcadedb_replay_filter_rejects_only_known_unsupported_fragments() -> None:
    adapter = object.__new__(ArcadeDBAdapter)

    assert adapter.gdsmith_query_is_replayable("MATCH (n:L0)-[r:T0]->(m:L1) WHERE r.id > 0 RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WITH n MATCH (m) RETURN m")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) OPTIONAL MATCH (n)-->(m) RETURN m")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) UNWIND [1] AS x RETURN x")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) CALL { RETURN 1 AS x } RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE EXISTS { MATCH (n)-->() } RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) RETURN n UNION MATCH (m) RETURN m")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n)<-[]-(m) RETURN n")


def test_arcadedb_ensure_schema_creates_database_when_exists_result_false() -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, object]):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    adapter = object.__new__(ArcadeDBAdapter)
    adapter._database = "graphsymperf"
    adapter._session = SimpleNamespace()
    adapter._connect = lambda: None

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if path.startswith("/api/v1/exists/"):
            return FakeResponse(200, {"result": False})
        if path == "/api/v1/server":
            return FakeResponse(200, {"result": "ok"})
        raise AssertionError(path)

    adapter._request = fake_request

    adapter.ensure_schema()

    assert calls == [
        ("GET", "/api/v1/exists/graphsymperf"),
        ("POST", "/api/v1/server"),
    ]


def test_arcadedb_execute_passes_timeout_to_http_request() -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"result": [{"ok": 1}]}

    adapter = object.__new__(ArcadeDBAdapter)
    adapter._database = "graphsymperf"
    adapter._base_url = "http://127.0.0.1:2481"
    adapter._connect = lambda: None

    class FakeSession:
        def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse()

    adapter._session = FakeSession()

    result = adapter.execute("MATCH (n) RETURN n", {}, "test", timeout_seconds=7)

    assert result.rows == ({"ok": 1},)
    assert calls[0]["timeout"] == 7
