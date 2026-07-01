"""Shared Cypher loading helpers for server-style property graph databases."""

from __future__ import annotations

from typing import Any, Mapping

from graph_sym_perf.datasets import MiniLdbcGraph, MiniLdbcNode, MiniLdbcRelationship
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph, GdsmithReplayNode, GdsmithReplayRelationship, parse_gdsmith_replay_graph


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

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        for node in graph.nodes:
            self._merge_gdsmith_node(node)
        for relationship in graph.relationships:
            self._merge_gdsmith_relationship(relationship)
        return graph

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

    def _merge_gdsmith_node(self, node: GdsmithReplayNode) -> None:
        labels = "".join(f":{label}" for label in (node.labels or ("GdsmithNode",)))
        query = f"""
MERGE (n{labels} {{id: $id}})
SET n += $properties
"""
        properties = dict(node.properties)
        properties.setdefault("id", node.node_id)
        self.execute(
            _compact(query),
            {"id": node.node_id, "properties": properties},
            "gdsmith_load_node",
        )

    def _merge_gdsmith_relationship(self, relationship: GdsmithReplayRelationship) -> None:
        rel_match = "{id: $rel_id}" if relationship.relationship_id is not None else ""
        query = f"""
MATCH (a {{id: $start_id}})
MATCH (b {{id: $end_id}})
MERGE (a)-[r:{relationship.relationship_type} {rel_match}]->(b)
SET r += $properties
"""
        properties = dict(relationship.properties)
        if relationship.relationship_id is not None:
            properties.setdefault("id", relationship.relationship_id)
        parameters = {
            "start_id": relationship.start_id,
            "end_id": relationship.end_id,
            "rel_id": relationship.relationship_id,
            "properties": properties,
        }
        self.execute(_compact(query), parameters, "gdsmith_load_relationship")


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())
