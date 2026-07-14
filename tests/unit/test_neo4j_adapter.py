from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters import neo4j as neo4j_adapter
from graph_sym_perf.adapters.neo4j import Neo4jAdapter, _collect_rows, _is_gdsmith_index_name, _neo4j_summary
from graph_sym_perf.generation.corpus import GraphCorpus


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


class _ResultSequenceAdapter:
    def __init__(self, rows):
        self._rows = iter(rows)
        self.calls = []

    def execute(self, query, parameters, query_kind, **kwargs):
        self.calls.append((query, parameters, query_kind, kwargs))
        return SimpleNamespace(
            rows=next(self._rows),
            summary={"truncated": False},
        )


def _valid_graph_manifest_rows():
    return [
        ({"nodes": 256},),
        ({"relationships": 430},),
        ({"labels": 5},),
        ({"types": 4},),
        ({"indexes": 3},),
    ]


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


def test_graph_manifest_records_requested_and_observed_scale_from_execute_calls() -> None:
    adapter = _ResultSequenceAdapter(_valid_graph_manifest_rows())
    statements = (" CREATE (n {id: 1}) ", "CREATE (m {id: 2})")

    manifest = neo4j_adapter.read_neo4j_graph_manifest(
        adapter,
        requested_max_nodes=256,
        statements=statements,
    )

    assert manifest.requested_max_nodes == 256
    assert manifest.actual_nodes == 256
    assert manifest.actual_relationships == 430
    assert manifest.label_count == 5
    assert manifest.relationship_type_count == 4
    assert manifest.index_count == 3
    assert manifest.graph_fingerprint == GraphCorpus(statements).content_hash
    assert [call[2] for call in adapter.calls] == [
        "graph_manifest_nodes",
        "graph_manifest_relationships",
        "graph_manifest_labels",
        "graph_manifest_relationship_types",
        "graph_manifest_indexes",
    ]
    assert all(call[3]["max_rows"] == 1 for call in adapter.calls)


@pytest.mark.parametrize(
    ("row_index", "invalid_row"),
    [
        (0, ()),
        (0, ({"unexpected": 256},)),
        (0, ({"nodes": -1},)),
        (1, ({"relationships": True},)),
        (2, ({"labels": 1.5},)),
        (3, ({"types": "4"},)),
        (4, ({"indexes": None},)),
    ],
)
def test_graph_manifest_fails_closed_on_missing_or_invalid_observed_facts(
    row_index,
    invalid_row,
) -> None:
    rows = _valid_graph_manifest_rows()
    rows[row_index] = invalid_row

    with pytest.raises((TypeError, ValueError)):
        neo4j_adapter.read_neo4j_graph_manifest(
            _ResultSequenceAdapter(rows),
            requested_max_nodes=256,
            statements=("CREATE (n {id: 1})",),
        )


@pytest.mark.parametrize("requested_max_nodes", [True, False, 0, -1, 1.5, "256"])
def test_graph_manifest_rejects_invalid_requested_scale(requested_max_nodes) -> None:
    with pytest.raises((TypeError, ValueError), match="requested_max_nodes"):
        neo4j_adapter.read_neo4j_graph_manifest(
            _ResultSequenceAdapter(_valid_graph_manifest_rows()),
            requested_max_nodes=requested_max_nodes,
            statements=("CREATE (n {id: 1})",),
        )
