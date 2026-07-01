"""Plan Metamorphic Coverage (PMC) data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


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
    metadata: Mapping[str, Any] = field(default_factory=dict)
