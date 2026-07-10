"""Typed canonical values: never stringify results for equivalence."""

from __future__ import annotations

from typing import Any


def canonical_value(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, list | tuple):
        return ("list", tuple(canonical_value(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((str(key), canonical_value(item)) for key, item in value.items())))
    return (type(value).__qualname__, repr(value))


def canonical_row(row: Any) -> tuple[Any, ...]:
    if isinstance(row, tuple):
        return tuple(canonical_value(value) for value in row)
    if isinstance(row, list):
        return tuple(canonical_value(value) for value in row)
    if isinstance(row, dict):
        return tuple(sorted((str(key), canonical_value(value)) for key, value in row.items()))
    return (canonical_value(row),)
