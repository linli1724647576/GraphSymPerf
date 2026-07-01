from __future__ import annotations

from graph_sym_perf.core import ConfirmationConfig, PlanDiff, VariantProfileSummary
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
