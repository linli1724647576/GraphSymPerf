"""Graph-snapshot fingerprints required by independent validation sessions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from graph_sym_perf.equivalence.canonical import canonical_value
from graph_sym_perf.gdsmith.replay import parse_gdsmith_replay_graph


@dataclass(frozen=True)
class GraphSnapshot:
    statements: tuple[str, ...]
    content_hash: str

    @classmethod
    def from_statements(cls, statements: tuple[str, ...] | list[str]) -> "GraphSnapshot":
        normalized = tuple(statement.strip() for statement in statements if statement.strip())
        payload = "\n".join(normalized).encode("utf-8")
        return cls(normalized, hashlib.sha256(payload).hexdigest())


@dataclass(frozen=True)
class GraphSnapshotVerification:
    verified: bool
    fingerprint: str | None
    reason_codes: tuple[str, ...]


def _identity_identifier(value: str) -> str:
    return value


def verify_graph_snapshot(
    adapter: Any,
    snapshot: GraphSnapshot,
    *,
    max_rows: int = 100_000,
    timeout_seconds: float = 30.0,
) -> GraphSnapshotVerification:
    """Read back graph-shape counts; a loader receipt is not verification."""

    identifier_normalizer = getattr(adapter, "graph_identifier_normalizer", None)
    if not callable(identifier_normalizer):
        identifier_normalizer = _identity_identifier
    expected = _expected_graph_shape(snapshot, identifier_normalizer=identifier_normalizer)
    queries = (
        (
            "validation_graph_nodes",
            "MATCH (n) RETURN n.id AS node_id, labels(n) AS labels, properties(n) AS properties ORDER BY node_id",
        ),
        (
            "validation_graph_relationships",
            "MATCH (a)-[r]->(b) RETURN a.id AS start_id, b.id AS end_id, type(r) AS relationship_type, properties(r) AS properties ORDER BY start_id, end_id, relationship_type",
        ),
    )
    actual: dict[str, object] = {}
    try:
        for query_kind, query in queries:
            result = adapter.execute(
                query,
                {},
                query_kind,
                max_rows=max_rows,
                timeout_seconds=timeout_seconds,
            )
            if bool(result.summary.get("truncated")) or len(result.rows) > max_rows:
                return GraphSnapshotVerification(False, None, ("graph_fingerprint_truncated",))
            actual[query_kind] = result.rows
        normalized = _actual_graph_shape(actual, identifier_normalizer=identifier_normalizer)
    except Exception as exc:
        return GraphSnapshotVerification(False, None, (f"graph_fingerprint_read_error:{type(exc).__name__}",))
    if normalized != expected:
        return GraphSnapshotVerification(False, None, ("graph_fingerprint_mismatch",))
    return GraphSnapshotVerification(True, snapshot.content_hash, ())


def _expected_graph_shape(
    snapshot: GraphSnapshot,
    *,
    identifier_normalizer: Callable[[str], str] = _identity_identifier,
) -> tuple[object, ...]:
    graph = parse_gdsmith_replay_graph(snapshot.statements)
    return (
        tuple(
            sorted(
                ((
                    canonical_value(node.node_id),
                    tuple(sorted(identifier_normalizer(label) for label in node.labels)),
                    canonical_value(node.properties),
                )
                for node in graph.nodes
                ),
                key=repr,
            )
        ),
        tuple(
            sorted(
                ((
                    canonical_value(relationship.start_id),
                    canonical_value(relationship.end_id),
                    identifier_normalizer(relationship.relationship_type),
                    canonical_value(relationship.properties),
                )
                for relationship in graph.relationships
                ),
                key=repr,
            )
        ),
    )


def _actual_graph_shape(
    actual: dict[str, object],
    *,
    identifier_normalizer: Callable[[str], str] = _identity_identifier,
) -> tuple[object, ...]:
    return (
        tuple(
            sorted(
                ((
                    canonical_value(row["node_id"]),
                    tuple(sorted(identifier_normalizer(str(label)) for label in row["labels"])),
                    canonical_value(row["properties"]),
                )
                for row in tuple(actual["validation_graph_nodes"])
                ),
                key=repr,
            )
        ),
        tuple(
            sorted(
                ((
                    canonical_value(row["start_id"]),
                    canonical_value(row["end_id"]),
                    identifier_normalizer(str(row["relationship_type"])),
                    canonical_value(row["properties"]),
                )
                for row in tuple(actual["validation_graph_relationships"])
                ),
                key=repr,
            )
        ),
    )
