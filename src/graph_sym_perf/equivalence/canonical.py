"""Collision-free typed canonical values for equivalence checks."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from graph_sym_perf.core import (
    MissingValue,
    TypedBytes,
    TypedDecimal,
    TypedNode,
    TypedPath,
    TypedRelationship,
    TypedTemporal,
)


class NonFiniteResultValue(ValueError):
    pass


class UnsupportedTypedResultValue(ValueError):
    pass


class CanonicalKind(str, Enum):
    NULL = "null"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    LIST = "list"
    MAP = "map"
    NODE = "node"
    RELATIONSHIP = "relationship"
    PATH = "path"
    TEMPORAL = "temporal"
    BYTES = "bytes"
    MISSING = "missing"
    DECIMAL = "decimal"


@dataclass(frozen=True)
class CanonicalValue:
    kind: CanonicalKind
    payload: tuple[object, ...] = ()


def canonical_value(value: Any) -> CanonicalValue:
    if value is None:
        return CanonicalValue(CanonicalKind.NULL)
    if isinstance(value, MissingValue):
        return CanonicalValue(CanonicalKind.MISSING)
    if isinstance(value, bool):
        return CanonicalValue(CanonicalKind.BOOL, (value,))
    if isinstance(value, int):
        return CanonicalValue(CanonicalKind.INT, (value,))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteResultValue("typed result contains NaN or infinity")
        return CanonicalValue(CanonicalKind.FLOAT, (value,))
    if isinstance(value, str):
        return CanonicalValue(CanonicalKind.STRING, (value,))
    if isinstance(value, TypedBytes):
        return CanonicalValue(CanonicalKind.BYTES, (value.value,))
    if isinstance(value, TypedDecimal):
        return CanonicalValue(CanonicalKind.DECIMAL, (value.sign, value.digits, value.exponent))
    if isinstance(value, TypedTemporal):
        return CanonicalValue(CanonicalKind.TEMPORAL, (value.kind.value, value.iso_value))
    if isinstance(value, TypedNode):
        return CanonicalValue(
            CanonicalKind.NODE,
            (value.element_id, value.labels, tuple((key, canonical_value(item)) for key, item in value.properties)),
        )
    if isinstance(value, TypedRelationship):
        return CanonicalValue(
            CanonicalKind.RELATIONSHIP,
            (
                value.element_id,
                value.relationship_type,
                value.start_element_id,
                value.end_element_id,
                tuple((key, canonical_value(item)) for key, item in value.properties),
            ),
        )
    if isinstance(value, TypedPath):
        return CanonicalValue(
            CanonicalKind.PATH,
            (
                tuple(canonical_value(node) for node in value.nodes),
                tuple(canonical_value(rel) for rel in value.relationships),
            ),
        )
    if isinstance(value, list | tuple):
        return CanonicalValue(CanonicalKind.LIST, tuple(canonical_value(item) for item in value))
    if isinstance(value, Mapping):
        return CanonicalValue(
            CanonicalKind.MAP,
            tuple(sorted((str(key), canonical_value(item)) for key, item in value.items())),
        )
    raise UnsupportedTypedResultValue(
        f"unsupported typed result value: {type(value).__module__}.{type(value).__qualname__}"
    )


def canonical_row(row: Any) -> tuple[Any, ...]:
    if isinstance(row, tuple | list):
        return tuple(canonical_value(value) for value in row)
    if isinstance(row, Mapping):
        return tuple(sorted((str(key), canonical_value(value)) for key, value in row.items()))
    return (canonical_value(row),)
