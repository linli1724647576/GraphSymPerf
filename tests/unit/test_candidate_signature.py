from __future__ import annotations

from graph_sym_perf.cluster.classify import classify_candidate
from graph_sym_perf.cluster.signature import build_candidate_signature
from graph_sym_perf.core import CaseRecord, ConfirmationRecord, MetricDelta, OracleRecord, PlanDiff, QueryFamily, QueryVariant, TargetSystem
from graph_sym_perf.coverage.model import CoverageUnit, PMCEvidence


def _record(case_id: str, rule: str, *, family_name: str) -> CaseRecord:
    return CaseRecord(
        system=TargetSystem.NEO4J,
        case_id=case_id,
        family=QueryFamily(
            case_id=case_id,
            seed_query="MATCH (n)-[:KNOWS]->(m) RETURN n, m",
            parameters={},
            variants=(
                QueryVariant(
                    variant_id=f"{case_id}_01_{rule}",
                    query="MATCH (m)<-[:KNOWS]-(n) RETURN n, m",
                    transform_rule=rule,
                    proof_obligation="proof",
                    metadata={"transform_family": family_name},
                ),
            ),
            source="gdsmith",
        ),
        equivalences=(),
        oracle=None,
        confirmation=None,
        artifacts={},
        state="confirmed",
        reason="candidate",
    )


def _plan_diff(*, filter_delta: int = 1, access_regressed: bool = True, rows_only: bool = False) -> PlanDiff:
    return PlanDiff(
        structural_hash_changed=True,
        changes=({"change": "replaced"},),
        added_nodes=({"operator_family": "Filter", "operator_type": "Filter"},) if filter_delta > 0 else (),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={"Filter": filter_delta},
        access_point_delta={
            "changed": access_regressed,
            "regressed": access_regressed,
            "original": ({"kind": "IndexSeek", "label": "Person"},),
            "transformed": ({"kind": "AllNodesScan", "label": "Person"},) if access_regressed else ({"kind": "IndexSeek", "label": "Person"},),
        },
        expand_chain_delta={"delta": 0, "changed": False, "original": (), "transformed": ()},
        barrier_delta={"delta": 0, "changed": False, "original": (), "transformed": ()},
        estimated_rows_delta={"ratio": 10.0, "worsened": True},
        metric_deltas=(
            (MetricDelta("rows", 10.0, 50.0, 5.0, 40.0, True),) if rows_only else (MetricDelta("db_hits", 10.0, 100.0, 10.0, 90.0, True),)
        ),
    )


def _confirmation(*, support_level: str = "profile_supported", db_hits_ratio: float = 10.0) -> ConfirmationRecord:
    return ConfirmationRecord(
        case_id="case_001",
        confirmed=True,
        original_median_ms=10.0,
        transformed_median_ms=100.0,
        latency_ratio=10.0,
        latency_delta_ms=90.0,
        original_samples_ms=(10.0,),
        transformed_samples_ms=(100.0,),
        reason="candidate",
        support_level=support_level,
        metric_deltas=(MetricDelta("db_hits", 10.0, 10.0 * db_hits_ratio, db_hits_ratio, 10.0 * (db_hits_ratio - 1.0), True),),
        fast_variant_id="seed",
        slow_variant_id="case_001_01_pattern_order_symmetry",
    )


def test_candidate_classification_covers_bool_redundancy_and_estimated_only() -> None:
    bool_labels = classify_candidate(
        transform_family="bool_redundancy",
        transform_rule="or_expr_null_contradiction",
        safety_tier="strict",
        oracle=None,
        confirmation=_confirmation(),
        plan_diff=_plan_diff(filter_delta=1, access_regressed=True),
        pmc_evidence=None,
    )
    estimated_only = classify_candidate(
        transform_family="symmetry",
        transform_rule="pattern_order_symmetry",
        safety_tier="strict",
        oracle=None,
        confirmation=_confirmation(support_level="plan_only", db_hits_ratio=1.0),
        plan_diff=PlanDiff(
            structural_hash_changed=True,
            changes=(),
            added_nodes=(),
            removed_nodes=(),
            replaced_nodes=(),
            family_delta={},
            access_point_delta={},
            expand_chain_delta={"delta": 0},
            barrier_delta={"delta": 0},
            estimated_rows_delta={"ratio": 5.0, "worsened": True},
            metric_deltas=(),
        ),
        pmc_evidence=None,
    )

    assert "NullContradictionSimplificationMissing" in bool_labels
    assert "PredicateResidueIntroduced" in bool_labels
    assert "EstimatedOnlyAnomaly" in estimated_only


def test_candidate_signature_is_stable_but_metric_buckets_split_unique_issue() -> None:
    rule = "pattern_order_symmetry"
    record_a = _record("case_001", rule, family_name="symmetry")
    record_b = _record("case_002", rule, family_name="symmetry")
    oracle = OracleRecord(case_id="case_001", is_candidate=True, candidate_types=("AccessPointAsymmetry",), reason="candidate", evidence={})
    pmc = PMCEvidence(
        case_id="case_001",
        transform_family="symmetry",
        transform_rule=rule,
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="case_001_01_pattern_order_symmetry",
        structural_hash_changed=True,
        units=(CoverageUnit(kind="access_delta", key="access_regressed", label="Access regressed"),),
        new_units=(),
        unit_count=1,
        new_unit_count=0,
    )

    sig_a = build_candidate_signature(
        record=record_a,
        transform_family="symmetry",
        transform_rule=rule,
        safety_tier="strict",
        oracle=oracle,
        confirmation=_confirmation(db_hits_ratio=10.0),
        plan_diff=_plan_diff(filter_delta=0, access_regressed=True),
        pmc_evidence=pmc,
    )
    sig_b = build_candidate_signature(
        record=record_b,
        transform_family="symmetry",
        transform_rule=rule,
        safety_tier="strict",
        oracle=oracle,
        confirmation=_confirmation(db_hits_ratio=2.0),
        plan_diff=_plan_diff(filter_delta=0, access_regressed=True),
        pmc_evidence=pmc,
    )

    assert sig_a.issue_family_key == sig_b.issue_family_key
    assert sig_a.plan_signature == sig_b.plan_signature
    assert sig_a.unique_issue_key != sig_b.unique_issue_key
