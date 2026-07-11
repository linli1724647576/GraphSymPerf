"""Shared fail-closed graph lifecycle operations."""

from __future__ import annotations

from typing import Any


def reset_graph_state(
    adapter: Any,
    *,
    query_kind: str,
    timeout_seconds: float | None = None,
) -> str:
    """Reset through an adapter-native database operation or a Cypher fallback."""

    reset_graph = getattr(adapter, "reset_graph", None)
    if callable(reset_graph) and reset_graph(timeout_seconds=timeout_seconds) is True:
        return "adapter"
    kwargs = {"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}
    adapter.execute("MATCH (n) DETACH DELETE n", {}, query_kind, **kwargs)
    return "cypher"


__all__ = ["reset_graph_state"]
