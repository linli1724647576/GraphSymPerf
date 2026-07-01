from __future__ import annotations

from dataclasses import replace

from graph_sym_perf.core import (
    CaseRecord,
    ConfirmationRecord,
    FeedbackConfig,
    OracleRecord,
    QueryCase,
    QueryFamily,
    QueryVariant,
    TargetSystem,
    TransformConfig,
)
from graph_sym_perf.coverage.model import CoverageUnit, PMCEvidence
from graph_sym_perf.feedback import TransformScheduler, initialize_feedback_state


def _query_case() -> QueryCase:
    return QueryCase(
        case_id="case_001",
        template_name="gdsmith",
        query="MATCH (n)-[:KNOWS]->(m) RETURN n, m",
        parameters={},
        base_predicate="true",
        safe_expression="n.id",
        source="gdsmith",
    )


def test_feedback_scheduler_is_deterministic_and_respects_high_reward_rules() -> None:
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=("pattern_order_symmetry", "predicate_order_symmetry"),
    )
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=1, seed=7)
    state = initialize_feedback_state(config, feedback)
    state.rule_stats[next(iter(state.rule_stats))].reward = 0.0
    for key, stats in state.rule_stats.items():
        if key.rule == "pattern_order_symmetry":
            stats.reward = 20.0
            stats.pmc_new_units = 5
            stats.attempted = 5
        else:
            stats.reward = 1.0
            stats.attempted = 5
    scheduler = TransformScheduler(config, feedback, state)

    first = scheduler.choose_for_case(_query_case())
    second = scheduler.choose_for_case(_query_case())

    assert first.family == "symmetry"
    assert first.enabled_rules == ("pattern_order_symmetry",)
    assert second.enabled_rules == first.enabled_rules


def test_feedback_disabled_keeps_static_family_choice() -> None:
    config = TransformConfig(
        enabled_families=("symmetry", "bool_redundancy"),
        enabled_rules=("pattern_order_symmetry", "predicate_order_symmetry"),
    )
    feedback = FeedbackConfig(enabled=False, exploration_rate=0.0, max_rules_per_case=1)
    scheduler = TransformScheduler(config, feedback, initialize_feedback_state(config, feedback))

    decision = scheduler.choose_for_case(_query_case())

    assert decision.family == "symmetry"
    assert decision.enabled_rules == ("pattern_order_symmetry", "predicate_order_symmetry")


def test_feedback_update_counts_equivalence_candidate_and_pmc_units() -> None:
    config = TransformConfig(enabled_families=("symmetry",), enabled_rules=("pattern_order_symmetry",))
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=1)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_001",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_001_01_pattern_order_symmetry",
                query="MATCH (n) RETURN n",
                transform_rule="pattern_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
        ),
        source="gdsmith",
    )
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_001",
        family=family,
        equivalences=(),
        oracle=OracleRecord(
            case_id="case_001",
            is_candidate=True,
            candidate_types=("OutlierVariant",),
            reason="candidate",
            evidence={},
        ),
        confirmation=ConfirmationRecord(
            case_id="case_001",
            confirmed=True,
            original_median_ms=10.0,
            transformed_median_ms=100.0,
            latency_ratio=10.0,
            latency_delta_ms=90.0,
            original_samples_ms=(10.0,),
            transformed_samples_ms=(100.0,),
            reason="candidate",
            support_level="profile_supported",
            fast_variant_id="seed",
            slow_variant_id="case_001_01_pattern_order_symmetry",
        ),
        artifacts={},
        state="confirmed",
        reason="candidate",
    )
    evidence = PMCEvidence(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_001_01_pattern_order_symmetry",
        structural_hash_changed=True,
        units=(CoverageUnit(kind="access_delta", key="access_changed", label="Access changed"),),
        new_units=(CoverageUnit(kind="access_delta", key="access_changed", label="Access changed"),),
        unit_count=1,
        new_unit_count=1,
    )

    scheduler.update_from_case_record(record, evidence)
    stats = state.rule_stats[next(iter(state.rule_stats))]

    assert stats.generated == 1
    assert stats.profile_available == 1
    assert stats.candidate_count == 1
    assert stats.strong_candidate_count == 1
    assert stats.pmc_new_units == 1
