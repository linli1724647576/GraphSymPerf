from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters.falkordb import FalkorDBAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter
from graph_sym_perf.core import QueryExecutionError


def test_memgraph_execute_passes_timeout_to_neo4j_query_object() -> None:
    calls: list[dict[str, object]] = []

    class FakeRecord:
        def data(self):
            return {"ok": 1}

    class FakeResult:
        def __iter__(self):
            return iter((FakeRecord(),))

        def consume(self):
            return SimpleNamespace(
                server=None,
                database=None,
                result_available_after=None,
                result_consumed_after=None,
            )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def run(self, query, parameters):
            calls.append({"query": query, "parameters": parameters})
            return FakeResult()

    class FakeDriver:
        def session(self):
            return FakeSession()

    adapter = object.__new__(MemgraphAdapter)
    adapter._driver = FakeDriver()
    adapter._connect = lambda: None

    result = adapter.execute("RETURN 1 AS ok", {}, "test", timeout_seconds=7)

    assert result.rows == ({"ok": 1},)
    assert getattr(calls[0]["query"], "text") == "RETURN 1 AS ok"
    assert getattr(calls[0]["query"], "timeout") == 7


def test_neo4j_profile_timeout_does_not_fallback_to_execute() -> None:
    class TimeoutResult:
        def __iter__(self):
            raise TimeoutError("client timeout")

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def run(self, query, parameters):
            return TimeoutResult()

    adapter = object.__new__(Neo4jAdapter)
    adapter._connect = lambda: None
    adapter._session = lambda: FakeSession()
    adapter.execute = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback"))

    with pytest.raises(QueryExecutionError, match="profile failed"):
        adapter.profile("RETURN 1 AS ok", {}, "test", timeout_seconds=10)


def test_memgraph_profile_timeout_does_not_fallback_to_execute() -> None:
    calls: list[str] = []
    adapter = object.__new__(MemgraphAdapter)

    def fake_execute(query, *args, **kwargs):
        calls.append(query)
        raise TimeoutError("client timeout")

    adapter.execute = fake_execute

    with pytest.raises(TimeoutError, match="client timeout"):
        adapter.profile("RETURN 1 AS ok", {}, "test", timeout_seconds=10)

    assert calls == ["PROFILE RETURN 1 AS ok"]


def test_memgraph_filter_rejects_string_prefix_suffix_operators() -> None:
    adapter = object.__new__(MemgraphAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 STARTS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 ENDS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE (n.k1) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE ((n.k1) AND true) RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 = n.k1 RETURN n")


def test_falkordb_filter_rejects_string_ops_and_bare_property_predicates() -> None:
    adapter = object.__new__(FalkorDBAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 STARTS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE (r0.k55) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE ((r0.k55) OR true) RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WHERE r0.k55 = 1 RETURN n")
