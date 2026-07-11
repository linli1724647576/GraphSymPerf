from __future__ import annotations

from graph_sym_perf.adapters.neo4j import Neo4jAdapter, _collect_rows, _is_gdsmith_index_name, _neo4j_summary


class FakeRecord:
    def __init__(self, payload):
        self.payload = payload

    def data(self):
        return self.payload


class FakeResult:
    def __init__(self, rows):
        self.rows = [FakeRecord(row) for row in rows]
        self.consumed = False

    def __iter__(self):
        return iter(self.rows)

    def consume(self):
        self.consumed = True
        return object()


def test_collect_rows_stops_after_row_cap_plus_one() -> None:
    result = FakeResult([{"id": 1}, {"id": 2}, {"id": 3}])

    rows, summary, truncated = _collect_rows(result, max_rows=2)

    assert rows == ({"id": 1}, {"id": 2}, {"id": 3})
    assert summary is None
    assert truncated
    assert not result.consumed


def test_collect_rows_consumes_complete_small_result() -> None:
    result = FakeResult([{"id": 1}])

    rows, summary, truncated = _collect_rows(result, max_rows=2)

    assert rows == ({"id": 1},)
    assert summary is not None
    assert not truncated
    assert result.consumed


def test_neo4j_summary_records_truncation() -> None:
    summary = _neo4j_summary(None, truncated=True, row_cap=2)

    assert summary["truncated"] is True
    assert summary["row_cap"] == 2


def test_schema_reset_only_recognizes_gdsmith_generated_index_names() -> None:
    assert _is_gdsmith_index_name("i0")
    assert _is_gdsmith_index_name("i123")
    assert not _is_gdsmith_index_name("gbp_person_dataset")
    assert not _is_gdsmith_index_name("i0_injected")


def test_neo4j_reset_graph_uses_bounded_transaction_batches() -> None:
    calls: list[tuple[str, str, float | None]] = []
    adapter = object.__new__(Neo4jAdapter)

    def execute(query, _parameters, query_kind, *, timeout_seconds=None, **_kwargs):
        calls.append((query, query_kind, timeout_seconds))

    adapter.execute = execute

    assert adapter.reset_graph(timeout_seconds=45) is True
    assert len(calls) == 1
    query, kind, timeout = calls[0]
    assert "CALL (n)" in query
    assert "IN TRANSACTIONS OF 1000 ROWS" in query
    assert kind == "neo4j_database_reset"
    assert timeout == 45
