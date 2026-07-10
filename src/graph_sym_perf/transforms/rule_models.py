from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetyTier(str, Enum):
    CORE = "core"
    EXPERIMENTAL = "experimental"
    CONTROL = "control"
    REMOVED = "removed"


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    tier: SafetyTier
    proof_id: str
    purity_requirements: tuple[str, ...]
    dialects: tuple[str, ...]
    site_classes: tuple[str, ...]
    submit_eligible: bool


class RuleRegistry:
    def __init__(self, rules: tuple[RuleSpec, ...]) -> None:
        self.rules = rules

    @classmethod
    def default(cls) -> "RuleRegistry":
        return cls((
            RuleSpec("null_complete_excluded_middle", SafetyTier.CORE, "three_valued_excluded_middle_v1", ("pure",), ("cypher", "agensgraph", "apache_age"), ("where_boolean",), True),
            RuleSpec("predicate_order_symmetry", SafetyTier.CORE, "conjunct_commutativity_v1", ("pure", "total"), ("cypher", "agensgraph", "apache_age"), ("where_boolean",), True),
            RuleSpec("pattern_decomposition_symmetry", SafetyTier.EXPERIMENTAL, "relationship_uniqueness_pending", ("pure",), ("cypher",), ("match_pattern",), False),
            RuleSpec("edge_notation_symmetry", SafetyTier.CONTROL, "parser_normalization_v1", ("pure",), ("cypher",), ("match_pattern",), False),
        ))

    def rules_for(self, dbms: str, tier: SafetyTier) -> tuple[RuleSpec, ...]:
        dialect = "agensgraph" if dbms == "agensgraph" else "apache_age" if dbms == "apache_age" else "cypher"
        return tuple(rule for rule in self.rules if rule.tier is tier and dialect in rule.dialects)
