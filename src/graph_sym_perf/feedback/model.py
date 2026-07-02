"""Feedback-guided transform scheduling data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RuleKey:
    family: str
    rule: str


@dataclass
class RuleStats:
    family: str
    rule: str
    attempted: int = 0
    generated: int = 0
    skipped: int = 0
    equivalence_passed: int = 0
    equivalence_failed: int = 0
    profile_available: int = 0
    candidate_count: int = 0
    strong_candidate_count: int = 0
    pmc_units: int = 0
    pmc_new_units: int = 0
    timeouts: int = 0
    errors: int = 0
    reward: float = 0.0
    inactive_reason: str | None = None


@dataclass(frozen=True)
class FeedbackDecision:
    family: str
    enabled_rules: tuple[str, ...]
    reason: str
    scores: Mapping[str, float]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FeedbackState:
    rule_stats: dict[RuleKey, RuleStats] = field(default_factory=dict)
    family_stats: dict[str, RuleStats] = field(default_factory=dict)
    exploration_rate: float = 0.15
    min_rules_per_case: int = 1
    max_rules_per_case: int | None = None
    seed: int = 20260522
