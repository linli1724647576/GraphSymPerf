from __future__ import annotations

from graph_sym_perf.core import MetricDelta, PlanDiff
from graph_sym_perf.coverage import (
    PMCType,
    PMCCoverageState,
    extract_pmc_units,
    filter_context_units,
    filter_core_units,
    ratio_bucket_label,
)
from graph_sym_perf.coverage.model import CoverageUnit, PMCEvidence


def _plan_diff() -> PlanDiff:
    return PlanDiff(
        structural_hash_changed=True,
        changes=({"change": "replaced"},),
        added_nodes=(
            {"operator_family": "Filter", "operator_type": "Filter", "details": "x.name = 1"},
        ),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={"Filter": 1, "Expand": 0, "Barrier": 0},
        access_point_delta={
            "changed": True,
            "regressed": True,
            "original_best_rank": 1,
            "transformed_best_rank": 4,
            "original": ({"kind": "IndexSeek", "variable": "n", "label": "Person"},),
            "transformed": ({"kind": "AllNodesScan", "variable": "n", "label": "Person"},),
        },
        expand_chain_delta={"changed": False, "delta": 0, "original": (), "transformed": ()},
        barrier_delta={"changed": False, "delta": 0, "original": (), "transformed": ()},
        estimated_rows_delta={
            "metric": "estimated_rows",
            "original": 10.0,
            "transformed": 120.0,
            "ratio": 12.0,
            "delta": 110.0,
            "worsened": True,
        },
        metric_deltas=(
            MetricDelta(metric="db_hits", original=10.0, transformed=100.0, ratio=10.0, delta=90.0, worsened=True),
            MetricDelta(metric="rows", original=5.0, transformed=25.0, ratio=5.0, delta=20.0, worsened=True),
        ),
    )


def test_extract_pmc_units_covers_operator_access_and_work() -> None:
    units = extract_pmc_units(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_01",
        plan_diff=_plan_diff(),
    )
    keys = {unit.key for unit in units}

    assert "operator_added:Filter" in keys
    assert "access_regressed" in keys
    assert "db_hits_ratio_bucket:10x" in keys


def test_pmc_coverage_state_marks_only_first_observation_as_new() -> None:
    units = extract_pmc_units(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_01",
        plan_diff=_plan_diff(),
    )
    evidence = PMCEvidence(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_01",
        structural_hash_changed=True,
        units=units,
        metadata={"is_candidate": True},
    )
    state = PMCCoverageState(novelty_weights={"access_delta": 2.0, "operator_delta": 1.0, "work_delta": 2.0, "structural": 0.5})

    first = state.observe(evidence)
    second = state.observe(evidence)
    summary = state.snapshot("run-001")

    core_units = filter_core_units(units)

    assert first.new_unit_count == len(core_units)
    assert second.new_unit_count == 0
    assert summary.total_new_units == len(core_units)
    assert summary.total_new_units_all == len(units)
    assert summary.units_by_kind["access_delta"] >= 1
    assert summary.candidate_units_by_kind["access_delta"] >= 1


def test_ratio_bucket_label_uses_stable_thresholds() -> None:
    assert ratio_bucket_label(1.4) == "below"
    assert ratio_bucket_label(1.5) == "1.5x"
    assert ratio_bucket_label(2.3) == "2x"
    assert ratio_bucket_label(10.1) == "10x"
    assert ratio_bucket_label(1000.0) == "1000x"


def test_pmc_summary_separates_core_and_context_units() -> None:
    state = PMCCoverageState()
    evidence = PMCEvidence(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_001_01_pattern_order_symmetry",
        structural_hash_changed=False,
        units=(
            CoverageUnit(kind="transformation_context", key="family:symmetry", label="Transform family: symmetry"),
            CoverageUnit(kind="access_delta", key="access_changed", label="Access changed"),
        ),
    )

    state.observe(evidence)
    summary = state.snapshot("run-core")

    assert summary.total_units_seen == 1
    assert summary.total_units_seen_core == 1
    assert summary.total_units_seen_all == 2
    assert summary.core_units["access_delta"] == 1
    assert summary.core_new_units["access_delta"] == 1
    assert summary.context_units["transformation_context"] == 1
    assert summary.context_new_units["transformation_context"] == 1
    assert all(unit["kind"] != "transformation_context" for unit in summary.top_units)
    assert summary.top_context_units[0]["key"] == "family:symmetry"


def test_pmc_filter_helpers_split_core_and_context_units() -> None:
    units = (
        CoverageUnit(kind="transformation_context", key="family:symmetry", label="Transform family"),
        CoverageUnit(kind="access_delta", key="access_changed", label="Access changed"),
    )

    assert PMCType.CORE.value == "core"
    assert tuple(unit.key for unit in filter_core_units(units)) == ("access_changed",)
    assert tuple(unit.key for unit in filter_context_units(units)) == ("family:symmetry",)


def test_metric_aliases_generate_cardinality_work_and_latency_units() -> None:
    plan = PlanDiff(
        structural_hash_changed=False,
        changes=(),
        added_nodes=(),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={},
        access_point_delta={},
        expand_chain_delta={"delta": 0},
        barrier_delta={"delta": 0},
        estimated_rows_delta={},
        metric_deltas=(
            MetricDelta("actual_rows", 10.0, 54.0, 5.4, 44.0, True),
            MetricDelta("actual_hits", 7.0, 70.0, 10.0, 63.0, True),
            MetricDelta("median_ms", 10.0, 62.0, 6.2, 52.0, True),
            MetricDelta("missing_metric", None, None, None, None, False),
        ),
    )

    units = extract_pmc_units(
        case_id="case_aliases",
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_aliases_01_pattern_order_symmetry",
        plan_diff=plan,
    )
    keys = {unit.key for unit in units}

    assert "actual_rows_inflated:5x" in keys
    assert "db_hits_ratio_bucket:10x" in keys
    assert "latency_ratio_bucket:5x" in keys
    assert all("missing_metric" not in key for key in keys)
