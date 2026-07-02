from __future__ import annotations

from graph_sym_perf.core import ConfirmationConfig, PlanDiff, VariantProfileSummary
from graph_sym_perf.app.mining import _confirmation_from_oracle
from graph_sym_perf.oracle import detect_symmetry_violation


def test_oracle_flags_latency_and_metric_supported_symmetry_violation() -> None:
    fast = VariantProfileSummary(
        variant_id="seed",
        query="MATCH (a)-[r:T0]->(b) RETURN b",
        query_kind="seed",
        median_ms=10.0,
        samples_ms=(9.0, 10.0, 11.0),
        plan=None,
        profile_plan=None,
        metrics={"db_hits": 10.0, "rows": 5.0},
    )
    slow = VariantProfileSummary(
        variant_id="case_01_edge_notation_symmetry",
        query="MATCH (b)<-[r:T0]-(a) RETURN b",
        query_kind="variant",
        median_ms=150.0,
        samples_ms=(140.0, 150.0, 160.0),
        plan=None,
        profile_plan=None,
        metrics={"db_hits": 100.0, "rows": 50.0},
    )
    diff = PlanDiff(
        structural_hash_changed=True,
        changes=({"change": "replaced"},),
        added_nodes=(),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={"Expand": 1},
        access_point_delta={"changed": True, "regressed": True},
        expand_chain_delta={"delta": 1},
        barrier_delta={"delta": 0},
        estimated_rows_delta={},
    )

    record = detect_symmetry_violation("case_0001", fast, slow, diff, ConfirmationConfig())

    assert record.is_candidate
    assert "AccessPointAsymmetry" in record.candidate_types
    assert "ExpansionAsymmetry" in record.candidate_types
    assert "OutlierVariant" in record.candidate_types
    assert record.evidence["oracle_layer_status"] == {
        "result_equivalence": "pass",
        "profile": "pass",
        "runtime": "pass",
    }


def test_oracle_does_not_promote_latency_only_signal_to_candidate() -> None:
    fast = VariantProfileSummary(
        variant_id="seed",
        query="MATCH (a)-[r:T0]->(b) RETURN b",
        query_kind="seed",
        median_ms=10.0,
        samples_ms=(10.0,),
        plan=None,
        profile_plan=None,
        metrics={"db_hits": 100.0, "rows": 100.0},
    )
    slow = VariantProfileSummary(
        variant_id="case_01_edge_notation_symmetry",
        query="MATCH (b)<-[r:T0]-(a) RETURN b",
        query_kind="variant",
        median_ms=150.0,
        samples_ms=(150.0,),
        plan=None,
        profile_plan=None,
        metrics={"db_hits": 100.0, "rows": 100.0},
    )
    diff = PlanDiff(
        structural_hash_changed=True,
        changes=({"change": "replaced"},),
        added_nodes=(),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={"Expand": 1},
        access_point_delta={"changed": True, "regressed": True},
        expand_chain_delta={"delta": 1},
        barrier_delta={"delta": 0},
        estimated_rows_delta={},
    )

    record = detect_symmetry_violation("case_0002", fast, slow, diff, ConfirmationConfig())

    # Plan-only candidates are now a legitimate weak tier (document/总体研究主线.md §6).
    # This case has plan_changed + candidate_types but no metric regression → plan_only.
    assert record.is_candidate  # plan-only IS a candidate
    assert record.evidence["support_level"] == "plan_only"
    assert record.evidence["latency"]["worsened"] is True
    assert record.evidence["oracle_layer_status"] == {
        "result_equivalence": "pass",
        "profile": "fail",
        "runtime": "fail",
    }
    confirmation = _confirmation_from_oracle("case_0002", fast, slow, record)
    assert not confirmation.confirmed
    assert confirmation.support_level == "plan_only"


def test_oracle_runtime_layer_never_passes_without_profile_layer() -> None:
    """Document 4.md §P0-3: runtime oracle MUST NOT pass independently of profile oracle.

    The oracle's internal `runtime_secondary_supported = core_metric_worse AND latency_worse`
    already enforces this dependency.  This test locks in that behaviour for
    every combination that *should* keep runtime at "fail".
    """
    config = ConfirmationConfig()
    base_fast = VariantProfileSummary(
        variant_id="seed", query="MATCH (n) RETURN n", query_kind="seed",
        median_ms=10.0, samples_ms=(10.0,), plan=None, profile_plan=None,
        metrics={"db_hits": 100.0, "rows": 100.0},
    )
    base_slow = VariantProfileSummary(
        variant_id="variant", query="MATCH (m) RETURN m", query_kind="variant",
        median_ms=200.0, samples_ms=(200.0,), plan=None, profile_plan=None,
        metrics={"db_hits": 100.0, "rows": 100.0},  # same metrics — profile NOT worsened
    )
    base_diff = PlanDiff(
        structural_hash_changed=True, changes=({"change": "replaced"},),
        added_nodes=(), removed_nodes=(), replaced_nodes=(),
        family_delta={"Expand": 1}, access_point_delta={"changed": True, "regressed": True},
        expand_chain_delta={"delta": 1}, barrier_delta={"delta": 0}, estimated_rows_delta={},
    )

    # Scenario A: latency worsens but profile is identical → runtime must be "fail"
    record_a = detect_symmetry_violation("case_a", base_fast, base_slow, base_diff, config)
    assert record_a.evidence["latency"]["worsened"] is True, "latency worsened"
    assert record_a.evidence["oracle_layer_status"]["profile"] == "fail", "profile unchanged"
    assert record_a.evidence["oracle_layer_status"]["runtime"] == "fail", (
        "runtime MUST be fail when profile is fail (document 4.md §P0-3)"
    )

    # Scenario B: profile worsens but latency is below threshold → runtime "fail"
    slow_b = VariantProfileSummary(
        variant_id="variant_b", query="MATCH (m) RETURN m", query_kind="variant",
        median_ms=12.0, samples_ms=(12.0,), plan=None, profile_plan=None,
        metrics={"db_hits": 500.0, "rows": 500.0},  # profile worsened
    )
    record_b = detect_symmetry_violation("case_b", base_fast, slow_b, base_diff, config)
    assert record_b.evidence["oracle_layer_status"]["profile"] == "pass"
    assert record_b.evidence["oracle_layer_status"]["runtime"] == "fail", (
        "runtime MUST be fail when latency threshold not met"
    )

    # Scenario C: both profile and latency worsen → runtime "pass" (only valid path)
    slow_c = VariantProfileSummary(
        variant_id="variant_c", query="MATCH (m) RETURN m", query_kind="variant",
        median_ms=200.0, samples_ms=(200.0,), plan=None, profile_plan=None,
        metrics={"db_hits": 500.0, "rows": 500.0},  # profile worsened
    )
    record_c = detect_symmetry_violation("case_c", base_fast, slow_c, base_diff, config)
    assert record_c.evidence["oracle_layer_status"]["profile"] == "pass"
    assert record_c.evidence["oracle_layer_status"]["runtime"] == "pass", (
        "runtime pass requires BOTH profile pass AND latency pass"
    )
