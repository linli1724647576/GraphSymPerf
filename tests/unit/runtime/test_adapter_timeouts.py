from __future__ import annotations

from collections.abc import Callable

import pytest

from graph_sym_perf.adapters.agensgraph import AgensGraphAdapter
from graph_sym_perf.adapters.apache_age import ApacheAgeAdapter
from graph_sym_perf.adapters.arcadedb import ArcadeDBAdapter
from graph_sym_perf.adapters.falkordb import FalkorDBAdapter
from graph_sym_perf.adapters.ladybugdb import LadybugDBAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter
from graph_sym_perf.core import QueryExecutionError, QueryTimeoutError
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import RuntimePolicy, RuntimeVerdict


class _TimeoutSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, *_args, **_kwargs):
        raise TimeoutError("deadline")


class _TimeoutDriver:
    def session(self, **_kwargs):
        return _TimeoutSession()


class _Neo4jClientConfiguredTimeout(Exception):
    code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"


class _Neo4jClientConfiguredTimeoutSession(_TimeoutSession):
    def run(self, *_args, **_kwargs):
        raise _Neo4jClientConfiguredTimeout("client transaction timeout")


class _Neo4jClientConfiguredTimeoutDriver:
    def session(self, **_kwargs):
        return _Neo4jClientConfiguredTimeoutSession()


class _TimeoutGraph:
    def query(self, *_args, **_kwargs):
        raise TimeoutError("deadline")


class _TimeoutCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        raise TimeoutError("deadline")


class _TimeoutConnection:
    def cursor(self):
        return _TimeoutCursor()

    def rollback(self) -> None:
        return None


class _TimeoutHttpSession:
    def request(self, *_args, **_kwargs):
        raise TimeoutError("deadline")


class _TimeoutEmbeddedConnection:
    def execute(self, *_args, **_kwargs):
        raise TimeoutError("deadline")


def _neo4j() -> Neo4jAdapter:
    adapter = object.__new__(Neo4jAdapter)
    adapter._driver = _TimeoutDriver()
    adapter._database = None
    adapter._connect = lambda: None
    return adapter


def _memgraph() -> MemgraphAdapter:
    adapter = object.__new__(MemgraphAdapter)
    adapter._driver = _TimeoutDriver()
    adapter._connect = lambda: None
    return adapter


def _falkordb() -> FalkorDBAdapter:
    adapter = object.__new__(FalkorDBAdapter)
    adapter._graph = _TimeoutGraph()
    adapter._connect = lambda: None
    return adapter


def _agensgraph() -> AgensGraphAdapter:
    adapter = object.__new__(AgensGraphAdapter)
    adapter._conn = _TimeoutConnection()
    adapter._graph_name = "graphsymperf"
    adapter._connect = lambda: None
    return adapter


def _apache_age() -> ApacheAgeAdapter:
    adapter = object.__new__(ApacheAgeAdapter)
    adapter._conn = _TimeoutConnection()
    adapter._graph_name = "graphsymperf"
    adapter._connect = lambda: None
    return adapter


def _arcadedb() -> ArcadeDBAdapter:
    adapter = object.__new__(ArcadeDBAdapter)
    adapter._session = _TimeoutHttpSession()
    adapter._base_url = "http://127.0.0.1:2481"
    adapter._database = "graphsymperf"
    adapter._connect = lambda: None
    return adapter


def _ladybugdb() -> LadybugDBAdapter:
    adapter = object.__new__(LadybugDBAdapter)
    adapter._conn = _TimeoutEmbeddedConnection()
    adapter._connect = lambda: None
    return adapter


@pytest.mark.parametrize(
    "adapter_factory",
    [_neo4j, _memgraph, _falkordb, _agensgraph, _apache_age, _arcadedb, _ladybugdb],
    ids=["neo4j", "memgraph", "falkordb", "agensgraph", "apache-age", "arcadedb", "ladybugdb"],
)
def test_execute_timeout_is_standardized_for_every_adapter(adapter_factory: Callable[[], object]) -> None:
    adapter = adapter_factory()

    with pytest.raises(QueryExecutionError) as captured:
        adapter.execute("MATCH (n) RETURN n", {}, "runtime")

    assert isinstance(captured.value, QueryTimeoutError)


def test_runtime_harness_recognizes_typed_adapter_timeout() -> None:
    evidence = RuntimeHarness().measure_pair(
        _falkordb(),
        "MATCH (a) RETURN a",
        "MATCH (b) RETURN b",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0),
    )

    assert evidence.verdict is RuntimeVerdict.INCONCLUSIVE
    assert evidence.reason_codes == ("both_timeout",)


def test_neo4j_client_configured_transaction_timeout_is_typed() -> None:
    adapter = object.__new__(Neo4jAdapter)
    adapter._driver = _Neo4jClientConfiguredTimeoutDriver()
    adapter._database = None
    adapter._connect = lambda: None

    with pytest.raises(QueryTimeoutError):
        adapter.execute(
            "MATCH (n) RETURN n",
            {},
            "runtime",
            timeout_seconds=60.0,
        )
