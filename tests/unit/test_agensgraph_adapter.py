from __future__ import annotations

from graph_sym_perf.adapters.agensgraph import (
    AgensGraphAdapter,
    _inline_parameters,
)
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.datasets import MiniLdbcGraph, MiniLdbcNode, MiniLdbcRelationship


def test_agensgraph_inline_parameters_render_nested_cypher_literals() -> None:
    rendered = _inline_parameters(
        "MERGE (n:Person {gbp_id: $gbp_id}) SET n += $properties",
        {
            "gbp_id": "person_001",
            "properties": {
                "gbp_id": "person_001",
                "age": 20,
                "active": True,
                "tags": ["graph", "cypher"],
            },
        },
    )

    assert "gbp_id: 'person_001'" in rendered
    assert "age: 20" in rendered
    assert "active: true" in rendered
    assert "tags: ['graph', 'cypher']" in rendered


def test_agensgraph_load_mini_ldbc_prepares_vertex_and_edge_labels() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement):
            self.statements.append(statement)

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()
            self.commits = 0

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.commits += 1

        def rollback(self):
            raise AssertionError("load_mini_ldbc should not rollback")

    graph = MiniLdbcGraph(
        dataset_id="unit",
        nodes=(
            MiniLdbcNode("Person", {"gbp_id": "person_001", "gbp_dataset": "unit"}),
            MiniLdbcNode("Post", {"gbp_id": "post_001", "gbp_dataset": "unit"}),
        ),
        relationships=(
            MiniLdbcRelationship(
                "LIKES",
                "Person",
                "person_001",
                "Post",
                "post_001",
                {"gbp_dataset": "unit"},
            ),
        ),
    )
    adapter = object.__new__(AgensGraphAdapter)
    adapter._conn = FakeConnection()
    adapter._graph_name = "graph_sym_perf"
    adapter._connect = lambda: None
    executed: list[tuple[str, dict[str, object], str]] = []

    def fake_execute(query, parameters, query_kind, **_kwargs):
        executed.append((query, dict(parameters), query_kind))
        return ExecutionResult(TargetSystem.AGENSGRAPH, query_kind, (), 0.0, {})

    adapter.execute = fake_execute

    adapter.load_mini_ldbc(graph)

    statements = adapter._conn.cursor_obj.statements
    assert "CREATE GRAPH IF NOT EXISTS graph_sym_perf" in statements
    assert "CREATE VLABEL IF NOT EXISTS Person" in statements
    assert "CREATE VLABEL IF NOT EXISTS Post" in statements
    assert "CREATE ELABEL IF NOT EXISTS LIKES" in statements
    assert [item[2] for item in executed] == ["load_node", "load_node", "load_relationship"]


def test_agensgraph_replay_filter_rejects_unsupported_label_patterns() -> None:
    adapter = object.__new__(AgensGraphAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n:L0:L1) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n:L0) MATCH (n:L1) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) RETURN DISTINCT n.k1 ORDER BY n.k2")
    assert adapter.gdsmith_query_is_replayable("MATCH (n:L0)-[r:T0]->(m:L1) RETURN n")
