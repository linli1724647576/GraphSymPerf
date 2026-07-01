from __future__ import annotations

from typing import Any, Mapping

from graph_sym_perf.adapters.kuzu import KuzuAdapter


class FakeKuzuAdapter(KuzuAdapter):
    def __init__(self) -> None:
        self.queries: list[tuple[str, Mapping[str, Any], str]] = []

    def execute(self, query: str, parameters: Mapping[str, Any], query_kind: str, **_kwargs):
        self.queries.append((query, dict(parameters), query_kind))
        return None


def test_kuzu_gdsmith_replay_generates_schema_and_load_queries() -> None:
    adapter = FakeKuzuAdapter()

    graph = adapter.load_gdsmith_graph(
        (
            "CREATE (n0 :L0{id : 1, stringProperty : 'x'})",
            "CREATE (n1 :L1{id : 2})",
            "MATCH (n0 {id : 1}), (n1 {id : 2}) MERGE(n0)-[r :T0{id : 3}]->(n1)",
        )
    )

    queries = [query for query, _parameters, _kind in adapter.queries]
    assert len(graph.nodes) == 2
    assert any(query.startswith("CREATE NODE TABLE IF NOT EXISTS L0") for query in queries)
    assert any(query.startswith("CREATE REL TABLE IF NOT EXISTS T0") for query in queries)
    assert any("CREATE (n:L0" in query for query in queries)
    assert any("CREATE (a)-[r:T0" in query for query in queries)
