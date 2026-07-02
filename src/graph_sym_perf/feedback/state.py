"""Feedback state initialization and summaries."""

from __future__ import annotations

from graph_sym_perf.core import FeedbackConfig, TransformConfig
from graph_sym_perf.feedback.model import FeedbackState, RuleKey, RuleStats
from graph_sym_perf.feedback.scoring import compute_reward, rule_priority

SYMMETRY_RULES: tuple[str, ...] = (
    "node_order_symmetry",
    "edge_notation_symmetry",
    "predicate_order_symmetry",
    "pattern_order_symmetry",
    "path_endpoint_symmetry",
    "label_order_symmetry",
    "property_map_order_symmetry",
    "alpha_renaming_symmetry",
    "exploratory_pattern_order_symmetry",
    "exploratory_predicate_order_symmetry",
    "exploratory_optional_match_order_symmetry",
)

BOOL_REDUNDANCY_RULES: tuple[str, ...] = (
    "and_true_or",
    "and_expr_or_true",
    "or_false_and",
    "or_expr_and_false",
    "and_not_false_and",
    "and_case_true",
    "and_expr_null_partition",
    "or_expr_null_contradiction",
    "and_not_expr_null_contradiction",
    "and_excluded_middle_or_null",
    "or_guarded_contradiction",
    "and_double_negated_true_or",
    "arcade_and_true",
    "arcade_or_false",
    "arcade_and_not_false",
    "arcade_and_true_or_false_expr",
)


def family_rules(transform_config: TransformConfig, family: str) -> tuple[str, ...]:
    if family == "symmetry":
        selected = tuple(rule for rule in transform_config.enabled_rules if rule in SYMMETRY_RULES)
        return selected or SYMMETRY_RULES
    if family == "bool_redundancy":
        selected = tuple(rule for rule in transform_config.enabled_rules if rule in BOOL_REDUNDANCY_RULES)
        return selected or BOOL_REDUNDANCY_RULES
    return ()


def initialize_feedback_state(transform_config: TransformConfig, feedback_config: FeedbackConfig) -> FeedbackState:
    rule_stats: dict[RuleKey, RuleStats] = {}
    family_stats: dict[str, RuleStats] = {}
    for family in transform_config.enabled_families:
        rules = family_rules(transform_config, family)
        family_stats[family] = RuleStats(family=family, rule="__family__")
        for rule in rules:
            rule_stats[RuleKey(family=family, rule=rule)] = RuleStats(family=family, rule=rule)
    return FeedbackState(
        rule_stats=rule_stats,
        family_stats=family_stats,
        exploration_rate=feedback_config.exploration_rate,
        min_rules_per_case=feedback_config.min_rules_per_case,
        max_rules_per_case=feedback_config.max_rules_per_case,
        seed=feedback_config.seed,
    )


def summarize_feedback_state(state: FeedbackState, feedback_config: FeedbackConfig) -> dict[str, object]:
    for stats in state.rule_stats.values():
        if stats.attempted == 0:
            family_attempted = state.family_stats.get(stats.family, RuleStats(stats.family, "__family__")).attempted
            stats.inactive_reason = (
                "disabled_by_max_variants_per_case" if family_attempted > 0 else "not_applicable_to_case"
            )
        stats.reward = compute_reward(stats, feedback_config)
    for stats in state.family_stats.values():
        stats.reward = compute_reward(stats, feedback_config)
    ranked_rules = sorted(
        state.rule_stats.values(),
        key=lambda stats: (
            -rule_priority(stats)[0],
            -rule_priority(stats)[1],
            -rule_priority(stats)[2],
            -rule_priority(stats)[3],
            stats.family,
            stats.rule,
        ),
    )
    return {
        "enabled": feedback_config.enabled,
        "exploration_rate": state.exploration_rate,
        "min_rules_per_case": state.min_rules_per_case,
        "max_rules_per_case": state.max_rules_per_case,
        "top_rules": [
            {
                "family": stats.family,
                "rule": stats.rule,
                "reward": stats.reward,
                "attempted": stats.attempted,
                "generated": stats.generated,
                "equivalence_passed": stats.equivalence_passed,
                "candidate_count": stats.candidate_count,
                "pmc_new_units": stats.pmc_new_units,
                "inactive_reason": stats.inactive_reason,
            }
            for stats in ranked_rules[:10]
        ],
        "family_stats": {
            family: {
                "reward": stats.reward,
                "attempted": stats.attempted,
                "generated": stats.generated,
                "equivalence_passed": stats.equivalence_passed,
                "candidate_count": stats.candidate_count,
                "pmc_new_units": stats.pmc_new_units,
            }
            for family, stats in sorted(state.family_stats.items())
        },
    }
