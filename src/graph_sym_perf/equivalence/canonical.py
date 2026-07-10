"""Typed canonical values: never stringify results for equivalence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


class NonFiniteResultValue(ValueError):
    pass


class UnsupportedTypedResultValue(ValueError):
    pass


def canonical_value(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteResultValue("typed result contains NaN or infinity")
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, list | tuple):
        return ("list", tuple(canonical_value(item) for item in value))
    if _is_graph_relationship(value):
        return (
            "relationship",
            str(getattr(value, "element_id", getattr(value, "id", ""))),
            str(getattr(value, "type", "")),
            str(getattr(value, "start_node_element_id", "")),
            str(getattr(value, "end_node_element_id", "")),
            tuple(sorted((str(key), canonical_value(item)) for key, item in value.items())),
        )
    if _is_graph_node(value):
        return (
            "node",
            str(getattr(value, "element_id", getattr(value, "id", ""))),
            tuple(sorted(str(label) for label in getattr(value, "labels", ()))),
            tuple(sorted((str(key), canonical_value(item)) for key, item in value.items())),
        )
    if isinstance(value, Mapping) and value.get("__graph_type__") in {"node", "relationship", "path"}:
        graph_type = str(value["__graph_type__"])
        return (graph_type, tuple(sorted((str(key), canonical_value(item)) for key, item in value.items() if key != "__graph_type__")))
    if isinstance(value, Mapping) and value.get("__typed_value__") in {"date", "datetime", "bytes"}:
        return ("typed", str(value["__typed_value__"]), canonical_value(value.get("value")))
    if isinstance(value, Mapping):
        return ("map", tuple(sorted((str(key), canonical_value(item)) for key, item in value.items())))
    raise UnsupportedTypedResultValue(f"unsupported typed result value: {type(value).__module__}.{type(value).__qualname__}")


def canonical_row(row: Any) -> tuple[Any, ...]:
    if isinstance(row, tuple):
        return tuple(canonical_value(value) for value in row)
    if isinstance(row, list):
        return tuple(canonical_value(value) for value in row)
    if isinstance(row, dict):
        return tuple(sorted((str(key), canonical_value(value)) for key, value in row.items()))
    return (canonical_value(row),)


def _is_graph_node(value: Any) -> bool:
    return hasattr(value, "labels") and hasattr(value, "items")


def _is_graph_relationship(value: Any) -> bool:
    return hasattr(value, "type") and hasattr(value, "items") and (
        hasattr(value, "start_node") or hasattr(value, "start_node_element_id")
    )
