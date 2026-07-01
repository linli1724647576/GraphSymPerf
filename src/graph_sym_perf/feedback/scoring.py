"""Deterministic and explainable feedback scoring."""

from __future__ import annotations

from graph_sym_perf.core import FeedbackConfig
from graph_sym_perf.feedback.model import RuleStats


def compute_reward(stats: RuleStats, feedback_config: FeedbackConfig) -> float:
    weights = feedback_config.reward_weights
    return round(
        float(weights.get("generated", 1.0)) * stats.generated
        + float(weights.get("equivalence_passed", 2.0)) * stats.equivalence_passed
        + float(weights.get("profile_available", 3.0)) * stats.profile_available
        + float(weights.get("candidate_count", 5.0)) * stats.candidate_count
        + float(weights.get("strong_candidate_count", 8.0)) * stats.strong_candidate_count
        + float(weights.get("pmc_new_units", 1.5)) * stats.pmc_new_units
        + float(weights.get("pmc_units", 0.2)) * stats.pmc_units
        + float(weights.get("equivalence_failed", -2.0)) * stats.equivalence_failed
        + float(weights.get("timeout", weights.get("timeouts", -3.0))) * stats.timeouts
        + float(weights.get("error", weights.get("errors", -2.0))) * stats.errors
        + float(weights.get("skipped", -1.0)) * stats.skipped,
        4,
    )


def rule_priority(stats: RuleStats) -> tuple[float, float, float, float, int, int]:
    attempts = max(stats.attempted, 1)
    novelty_rate = stats.pmc_new_units / attempts
    equivalence_rate = stats.equivalence_passed / attempts
    candidate_density = stats.candidate_count / attempts
    failure_penalty = (stats.equivalence_failed + stats.timeouts + stats.errors + stats.skipped) / attempts
    return (
        stats.reward,
        novelty_rate,
        equivalence_rate,
        candidate_density,
        -int(round(failure_penalty * 1000)),
        -stats.attempted,
    )
