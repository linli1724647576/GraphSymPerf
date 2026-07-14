from graph_sym_perf.transforms.boolean_equivalence.proofs import (
    TruthValue,
    constant_false_conjunction as _constant_false_conjunction,
    constant_true_disjunction as _constant_true_disjunction,
    null_complete_excluded_middle as _null_complete_excluded_middle,
    null_contradiction as _null_contradiction,
    null_partition_tautology as _null_partition_tautology,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


BOOLEAN_RULES = tuple(
    rule
    for rule in RuleRegistry.default().rules_for("neo4j", SafetyTier.CORE)
    if rule.site_classes == ("where_boolean",)
)


def constant_true_disjunction(value: TruthValue) -> TruthValue:
    return _constant_true_disjunction(value)


def constant_false_conjunction(value: TruthValue) -> TruthValue:
    return _constant_false_conjunction(value)


def null_partition_tautology(value: TruthValue) -> bool:
    return _null_partition_tautology(value)


def null_contradiction(value: TruthValue) -> bool:
    return _null_contradiction(value)


def null_complete_excluded_middle(value: TruthValue) -> bool:
    return _null_complete_excluded_middle(value)


__all__ = [
    "BOOLEAN_RULES",
    "constant_false_conjunction",
    "constant_true_disjunction",
    "null_complete_excluded_middle",
    "null_contradiction",
    "null_partition_tautology",
]
