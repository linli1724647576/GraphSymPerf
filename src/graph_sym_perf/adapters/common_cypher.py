"""Shared Cypher loading helpers for server-style property graph databases."""

from __future__ import annotations

from typing import Any, Mapping

from graph_sym_perf.datasets import MiniLdbcGraph, MiniLdbcNode, MiniLdbcRelationship


class CypherLoaderMixin:
    def execute(self, query: str, parameters: Mapping[str, Any], query_kind: str):  # pragma: no cover - protocol hook
        raise NotImplementedError

    def ensure_schema(self) -> None:
        for query in self.schema_statements():
            try:
                self.execute(query, {}, "schema")
            except Exception:
                continue

    def schema_statements(self) -> tuple[str, ...]:
        return ()

    def load_mini_ldbc(self, graph: MiniLdbcGraph) -> None:
        for node in graph.nodes:
            self._merge_node(node)
        for relationship in graph.relationships:
            self._merge_relationship(relationship)

    def _merge_node(self, node: MiniLdbcNode) -> None:
        query = f"""
MERGE (n:{node.label} {{gbp_id: $gbp_id}})
SET n += $properties
"""
        self.execute(
            _compact(query),
            {"gbp_id": node.properties["gbp_id"], "properties": dict(node.properties)},
            "load_node",
        )

    def _merge_relationship(self, relationship: MiniLdbcRelationship) -> None:
        query = f"""
MATCH (a:{relationship.start_label} {{gbp_id: $start_id, gbp_dataset: $dataset_id}})
MATCH (b:{relationship.end_label} {{gbp_id: $end_id, gbp_dataset: $dataset_id}})
MERGE (a)-[r:{relationship.relationship_type} {{gbp_dataset: $dataset_id}}]->(b)
SET r += $properties
"""
        self.execute(
            _compact(query),
            {
                "start_id": relationship.start_id,
                "end_id": relationship.end_id,
                "dataset_id": relationship.properties["gbp_dataset"],
                "properties": dict(relationship.properties),
            },
            "load_relationship",
        )


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())
