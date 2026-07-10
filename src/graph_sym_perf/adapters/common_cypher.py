"""Shared Cypher loading helpers for server-style property graph databases."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from graph_sym_perf.core import QueryExecutionError, QueryTimeoutError
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph, GdsmithReplayNode, GdsmithReplayRelationship, parse_gdsmith_replay_graph

_logger = logging.getLogger(__name__)


class CypherLoaderMixin:
    def execute(self, query: str, parameters: Mapping[str, Any], query_kind: str):  # pragma: no cover - protocol hook
        raise NotImplementedError

    def ensure_schema(self) -> None:
        for query in self.schema_statements():
            try:
                self.execute(query, {}, "schema")
            except Exception as exc:
                _logger.debug("Schema statement skipped (non-fatal): %s", exc)
                continue

    def schema_statements(self) -> tuple[str, ...]:
        return ()

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        for node in graph.nodes:
            self._merge_gdsmith_node(node)
        for relationship in graph.relationships:
            self._merge_gdsmith_relationship(relationship)
        return graph

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


def _query_execution_error(message: str, exc: Exception) -> QueryExecutionError:
    """Classify known typed timeout causes without exposing driver types upstream."""

    if isinstance(exc, QueryTimeoutError):
        return exc
    if isinstance(exc, TimeoutError):
        return QueryTimeoutError(message)
    if getattr(exc, "sqlstate", None) == "57014":
        return QueryTimeoutError(message)
    if getattr(exc, "code", None) in {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.TransientError.Transaction.TransactionTimedOut",
    }:
        return QueryTimeoutError(message)
    try:
        import requests

        if isinstance(exc, requests.exceptions.Timeout):
            return QueryTimeoutError(message)
    except ImportError:  # pragma: no cover - ArcadeDB dependency probe owns this failure.
        pass
    try:
        from redis.exceptions import TimeoutError as RedisTimeoutError

        if isinstance(exc, RedisTimeoutError):
            return QueryTimeoutError(message)
    except ImportError:  # pragma: no cover - FalkorDB dependency probe owns this failure.
        pass
    return QueryExecutionError(message)


def _has_bare_property_predicate(query: str) -> bool:
    """Return True if *query* has a bare property reference used as a predicate."""
    import re

    return bool(
        re.search(
            r"\bWHERE\b[^\n]*\(\s*[A-Za-z]\w*\.[A-Za-z]\w*\s*\)",
            query,
            flags=re.IGNORECASE,
        )
    )


def _has_boolean_property_operand(query: str) -> bool:
    """Return True if *query* uses a bare property as a boolean operand."""
    import re

    return bool(
        re.search(
            r"(\(\s*[A-Za-z]\w*\.[A-Za-z]\w*\s*\)\s+(AND|OR)\b)|(\b(AND|OR)\s+\(\s*[A-Za-z]\w*\.[A-Za-z]\w*\s*\))",
            query,
            flags=re.IGNORECASE,
        )
    )
