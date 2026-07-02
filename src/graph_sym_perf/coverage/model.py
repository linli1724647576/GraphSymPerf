"""Plan Metamorphic Coverage (PMC) data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class PMCType(str, Enum):
    CORE = "core"
    CONTEXT = "context"


CORE_PMC_KINDS = frozenset(
    {
        "operator_delta",
        "access_delta",
        "expand_delta",
        "join_delta",
        "predicate_delta",
        "barrier_delta",
        "cardinality_delta",
        "work_delta",
        "structural",
    }
)
CONTEXT_PMC_KINDS = frozenset({"transformation_context"})


@dataclass(frozen=True)
class CoverageUnit:
    kind: str
    key: str
    label: str
    weight: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PMCEvidence:
    case_id: str
    transform_family: str
    transform_rule: str
    safety_tier: str
    fast_variant_id: str | None
    slow_variant_id: str | None
    structural_hash_changed: bool
    units: tuple[CoverageUnit, ...]
    new_units: tuple[CoverageUnit, ...] = ()
    unit_count: int = 0
    new_unit_count: int = 0
    novelty_score: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PMCSummary:
    run_id: str
    total_units_seen: int
    total_new_units: int
    units_by_kind: Mapping[str, int]
    new_units_by_kind: Mapping[str, int]
    units_by_transform_family: Mapping[str, int]
    units_by_transform_rule: Mapping[str, int]
    candidate_units_by_kind: Mapping[str, int]
    top_units: tuple[Mapping[str, Any], ...]
    total_units_seen_all: int = 0
    total_units_seen_core: int = 0
    total_new_units_all: int = 0
    total_new_units_core: int = 0
    core_units: Mapping[str, int] = field(default_factory=dict)
    core_new_units: Mapping[str, int] = field(default_factory=dict)
    context_units: Mapping[str, int] = field(default_factory=dict)
    context_new_units: Mapping[str, int] = field(default_factory=dict)
    top_context_units: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


def pmc_type_for_unit(unit: CoverageUnit) -> PMCType:
    if unit.kind in CONTEXT_PMC_KINDS:
        return PMCType.CONTEXT
    return PMCType.CORE


def filter_core_units(units: Iterable[CoverageUnit]) -> tuple[CoverageUnit, ...]:
    return tuple(unit for unit in units if pmc_type_for_unit(unit) is PMCType.CORE)


def filter_context_units(units: Iterable[CoverageUnit]) -> tuple[CoverageUnit, ...]:
    return tuple(unit for unit in units if pmc_type_for_unit(unit) is PMCType.CONTEXT)
