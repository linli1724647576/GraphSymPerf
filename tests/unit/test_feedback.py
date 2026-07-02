from __future__ import annotations

from dataclasses import replace

from graph_sym_perf.core import (
    CaseRecord,
    ConfirmationRecord,
    EquivalenceRecord,
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


def test_feedback_disabled_static_rotation_prevents_rule_starvation() -> None:
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=(
            "predicate_order_symmetry",
            "pattern_order_symmetry",
            "label_order_symmetry",
            "alpha_renaming_symmetry",
        ),
        max_variants_per_case=3,
    )
    feedback = FeedbackConfig(enabled=False, exploration_rate=0.0)
    scheduler = TransformScheduler(config, feedback, initialize_feedback_state(config, feedback))

    chosen = {
        rule
        for index in range(20)
        for rule in scheduler.choose_for_case(
            QueryCase(
                case_id=f"case_{index:03d}",
                template_name="gdsmith",
                query="MATCH (n)-[:KNOWS]->(m) RETURN n, m",
                parameters={},
                base_predicate="true",
                safe_expression="n.id",
                source="gdsmith",
            )
        ).enabled_rules
    }

    assert "alpha_renaming_symmetry" in chosen


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


def test_feedback_counts_profile_and_runtime_supported_as_strong_candidate() -> None:
    config = TransformConfig(enabled_families=("symmetry",), enabled_rules=("pattern_order_symmetry",))
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=1)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_runtime",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_runtime_01_pattern_order_symmetry",
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
        case_id="case_runtime",
        family=family,
        equivalences=(),
        oracle=OracleRecord(
            case_id="case_runtime",
            is_candidate=True,
            candidate_types=("OutlierVariant",),
            reason="candidate",
            evidence={"oracle_layer_status": {"runtime": "pass"}},
        ),
        confirmation=ConfirmationRecord(
            case_id="case_runtime",
            confirmed=True,
            original_median_ms=10.0,
            transformed_median_ms=100.0,
            latency_ratio=10.0,
            latency_delta_ms=90.0,
            original_samples_ms=(10.0,),
            transformed_samples_ms=(100.0,),
            reason="candidate",
            support_level="profile_and_runtime_supported",
            fast_variant_id="seed",
            slow_variant_id="case_runtime_01_pattern_order_symmetry",
        ),
        artifacts={},
        state="profile_and_latency_supported_candidate",
        reason="candidate",
    )
    evidence = PMCEvidence(
        case_id="case_runtime",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_runtime_01_pattern_order_symmetry",
        structural_hash_changed=True,
        units=(CoverageUnit(kind="access_delta", key="access_changed", label="Access changed"),),
    )

    scheduler.update_from_case_record(record, evidence)
    stats = state.rule_stats[next(iter(state.rule_stats))]

    assert stats.candidate_count == 1
    assert stats.strong_candidate_count == 1


def test_feedback_rule_stats_are_variant_level_and_self_consistent() -> None:
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=("pattern_order_symmetry", "label_order_symmetry"),
    )
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=2)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_002",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_002_01_pattern_order_symmetry",
                query="MATCH (n) RETURN n",
                transform_rule="pattern_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
            QueryVariant(
                variant_id="case_002_02_label_order_symmetry",
                query="MATCH (n:A:B) RETURN n",
                transform_rule="label_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
        ),
        source="gdsmith",
    )
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_002",
        family=family,
        equivalences=(
            EquivalenceRecord(
                case_id="case_002_01_pattern_order_symmetry",
                passed=True,
                left_hash="a",
                right_hash="a",
                left_count=1,
                right_count=1,
                reason="equal",
            ),
            EquivalenceRecord(
                case_id="case_002_02_label_order_symmetry",
                passed=False,
                left_hash="a",
                right_hash="b",
                left_count=1,
                right_count=2,
                reason="different",
            ),
        ),
        oracle=None,
        confirmation=None,
        artifacts={},
        state="equivalence_failed",
        reason="NO_DYNAMICALLY_EQUIVALENT_VARIANT",
    )

    scheduler.update_from_case_record(record, None)

    pattern = state.rule_stats[next(key for key in state.rule_stats if key.rule == "pattern_order_symmetry")]
    label = state.rule_stats[next(key for key in state.rule_stats if key.rule == "label_order_symmetry")]
    assert pattern.generated == 1
    assert pattern.equivalence_passed == 1
    assert pattern.equivalence_failed == 0
    assert label.generated == 1
    assert label.equivalence_passed == 0
    assert label.equivalence_failed == 1
    for stats in (pattern, label):
        assert stats.generated <= stats.attempted
        assert stats.equivalence_passed + stats.equivalence_failed + stats.errors + stats.timeouts <= stats.generated


def test_feedback_case_level_error_does_not_contaminate_variant_rules() -> None:
    """Document 4.md §P0-2: case-level error/timeout MUST NOT propagate to every variant."""
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=("pattern_order_symmetry", "label_order_symmetry", "alpha_renaming_symmetry"),
    )
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=3)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_err",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_err_01_pattern_order_symmetry",
                query="MATCH (n) RETURN n",
                transform_rule="pattern_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
            QueryVariant(
                variant_id="case_err_02_label_order_symmetry",
                query="MATCH (n:A:B) RETURN n",
                transform_rule="label_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
            QueryVariant(
                variant_id="case_err_03_alpha_renaming_symmetry",
                query="MATCH (x) RETURN x",
                transform_rule="alpha_renaming_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
        ),
        source="gdsmith",
    )
    # Simulate seed-execution error — the case-level state is "error" but
    # equivalences are empty (no per-variant checking happened).
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_err",
        family=family,
        equivalences=(),  # no per-variant equivalence was run
        oracle=None,
        confirmation=None,
        artifacts={},
        state="error",
        reason="ConnectionError: seed execution failed",
    )

    scheduler.update_from_case_record(record, None)

    # Every variant should be counted as attempted + generated, but NONE should
    # have errors incremented just because the case-level state is "error".
    for key, stats in state.rule_stats.items():
        assert stats.attempted == 1, f"{key.rule}: attempted"
        assert stats.generated == 1, f"{key.rule}: generated"
        assert stats.errors == 0, f"{key.rule}: errors must not cross-contaminate from case-level state"
        assert stats.equivalence_passed == 0, f"{key.rule}: no equivalence was run"
        assert stats.equivalence_failed == 0, f"{key.rule}: no equivalence was run"


def test_feedback_case_level_timeout_does_not_contaminate_variant_rules() -> None:
    """Document 4.md §P0-2: case-level timeout reason MUST NOT increment timeouts for every rule."""
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=("pattern_order_symmetry", "label_order_symmetry"),
    )
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=2)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_timeout",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_timeout_01_pattern_order_symmetry",
                query="MATCH (n) RETURN n",
                transform_rule="pattern_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
            QueryVariant(
                variant_id="case_timeout_02_label_order_symmetry",
                query="MATCH (n:A:B) RETURN n",
                transform_rule="label_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
        ),
        source="gdsmith",
    )
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_timeout",
        family=family,
        equivalences=(
            EquivalenceRecord(case_id="case_timeout_01_pattern_order_symmetry", passed=True, left_hash="a", right_hash="a", left_count=1, right_count=1, reason="equal"),
            EquivalenceRecord(case_id="case_timeout_02_label_order_symmetry", passed=True, left_hash="a", right_hash="a", left_count=1, right_count=1, reason="equal"),
        ),
        oracle=None,
        confirmation=None,
        artifacts={},
        state="equivalent_no_candidate",
        reason="PROFILE_TIMEOUT",  # case-level timeout — should NOT affect per-rule stats
    )

    scheduler.update_from_case_record(record, None)

    for key, stats in state.rule_stats.items():
        assert stats.timeouts == 0, f"{key.rule}: case-level timeout must not cross-contaminate to rule stats"


def test_feedback_skipped_variants_increment_attempted_but_not_generated() -> None:
    """Skipped variants (failed static safety) are attempted but not generated."""
    config = TransformConfig(
        enabled_families=("symmetry",),
        enabled_rules=("pattern_order_symmetry", "predicate_order_symmetry"),
    )
    feedback = FeedbackConfig(enabled=True, exploration_rate=0.0, max_rules_per_case=2)
    state = initialize_feedback_state(config, feedback)
    scheduler = TransformScheduler(config, feedback, state)
    family = QueryFamily(
        case_id="case_skipped",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_skipped_01_pattern_order_symmetry",
                query="MATCH (n) RETURN n",
                transform_rule="pattern_order_symmetry",
                proof_obligation="proof",
                metadata={"transform_family": "symmetry"},
            ),
        ),
        skipped_variants=(
            QueryVariant(
                variant_id="case_skipped_02_predicate_order_symmetry",
                query="",
                transform_rule="predicate_order_symmetry",
                proof_obligation="none",
                metadata={"transform_family": "symmetry"},
                skip_reason="no_applicable_predicate",
                safety_tier="strict",
            ),
        ),
        source="gdsmith",
    )
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_skipped",
        family=family,
        equivalences=(
            EquivalenceRecord(case_id="case_skipped_01_pattern_order_symmetry", passed=True, left_hash="a", right_hash="a", left_count=1, right_count=1, reason="equal"),
        ),
        oracle=None,
        confirmation=None,
        artifacts={},
        state="equivalent_no_candidate",
        reason="single variant",
    )

    scheduler.update_from_case_record(record, None)

    pattern_key = next(k for k in state.rule_stats if k.rule == "pattern_order_symmetry")
    pred_key = next(k for k in state.rule_stats if k.rule == "predicate_order_symmetry")

    # pattern_order_symmetry: generated variant → attempted=1, generated=1
    assert state.rule_stats[pattern_key].attempted == 1
    assert state.rule_stats[pattern_key].generated == 1
    assert state.rule_stats[pattern_key].skipped == 0

    # predicate_order_symmetry: skipped variant → attempted=1, generated=0, skipped=1
    assert state.rule_stats[pred_key].attempted == 1
    assert state.rule_stats[pred_key].generated == 0
    assert state.rule_stats[pred_key].skipped == 1

    # Invariant: generated <= attempted (document 4.md §P0-2)
    for stats in state.rule_stats.values():
        assert stats.generated <= stats.attempted
