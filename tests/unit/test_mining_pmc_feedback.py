from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from graph_sym_perf.app.mining import _run_query_cases
from graph_sym_perf.app import mining
from graph_sym_perf.core import (
    CaseRecord,
    ConfirmationRecord,
    EquivalenceRecord,
    ExecutionResult,
    OracleRecord,
    QueryCase,
    QueryFamily,
    QueryVariant,
    RunPaths,
    TargetSystem,
    VariantProfileSummary,
)
from graph_sym_perf.coverage import make_pmc_evidence
from graph_sym_perf.coverage.state import PMCCoverageState
from graph_sym_perf.core import MetricDelta, PlanDiff
from graph_sym_perf.reporter import ArtifactWriter
from graph_sym_perf.config import load_run_config


class _FakeAdapter:
    target = TargetSystem.NEO4J


def _query_cases() -> tuple[QueryCase, ...]:
    return (
        QueryCase(
            case_id="case_001",
            template_name="gdsmith",
            query="MATCH (n)-[:KNOWS]->(m) RETURN n, m",
            parameters={},
            base_predicate="true",
            safe_expression="n.id",
            source="gdsmith",
        ),
        QueryCase(
            case_id="case_002",
            template_name="gdsmith",
            query="MATCH (n)-[:KNOWS]->(m) RETURN m, n",
            parameters={},
            base_predicate="true",
            safe_expression="n.id",
            source="gdsmith",
        ),
    )


def _plan_diff(multiplier: float) -> PlanDiff:
    return PlanDiff(
        structural_hash_changed=True,
        changes=({"change": "replaced"},),
        added_nodes=({"operator_family": "Filter", "operator_type": "Filter"},),
        removed_nodes=(),
        replaced_nodes=(),
        family_delta={"Filter": 1, "Barrier": 0},
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
        estimated_rows_delta={"ratio": multiplier, "worsened": True},
        metric_deltas=(
            MetricDelta("db_hits", 10.0, 10.0 * multiplier, multiplier, 10.0 * (multiplier - 1.0), True),
        ),
    )


def test_run_query_cases_writes_pmc_and_feedback_artifacts(tmp_path: Path, monkeypatch) -> None:
    config = load_run_config("config/graph_sym_perf.yaml")
    config = replace(
        config,
        paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"),
        feedback=replace(config.feedback, enabled=True, exploration_rate=0.0, max_rules_per_case=1),
    )
    writer = ArtifactWriter(config, "run-smoke")
    counter = {"index": 0}

    def fake_generate(query_case, config, *, target=None, family_name=None):
        rule = config.enabled_rules[0]
        return QueryFamily(
            case_id=query_case.case_id,
            seed_query=query_case.query,
            parameters=query_case.parameters,
            variants=(
                QueryVariant(
                    variant_id=f"{query_case.case_id}_01_{rule}",
                    query=query_case.query,
                    transform_rule=rule,
                    proof_obligation="proof",
                    metadata={"transform_family": family_name or "symmetry"},
                ),
            ),
            source=query_case.source,
        )

    def fake_run_family(config, writer, adapter, family, artifacts):
        index = counter["index"]
        counter["index"] += 1
        variant = family.variants[0]
        oracle = OracleRecord(
            case_id=family.case_id,
            is_candidate=index == 0,
            candidate_types=("OutlierVariant",),
            reason="candidate" if index == 0 else "no-signal",
            evidence={},
        )
        confirmation = ConfirmationRecord(
            case_id=family.case_id,
            confirmed=index == 0,
            original_median_ms=10.0,
            transformed_median_ms=100.0 if index == 0 else 20.0,
            latency_ratio=10.0 if index == 0 else 2.0,
            latency_delta_ms=90.0 if index == 0 else 10.0,
            original_samples_ms=(10.0,),
            transformed_samples_ms=(100.0 if index == 0 else 20.0,),
            reason="candidate" if index == 0 else "no-signal",
            support_level="profile_supported" if index == 0 else "plan_only",
            fast_variant_id="seed",
            slow_variant_id=variant.variant_id,
        )
        pmc = make_pmc_evidence(
            case_id=family.case_id,
            transform_family=str(variant.metadata["transform_family"]),
            transform_rule=variant.transform_rule,
            safety_tier="strict",
            fast_variant_id="seed",
            slow_variant_id=variant.variant_id,
            plan_diff=_plan_diff(10.0 if index == 0 else 2.0),
            coverage_config=config.coverage,
            metadata={"is_candidate": index == 0},
        )
        return CaseRecord(
            system=TargetSystem.NEO4J,
            case_id=family.case_id,
            family=family,
            equivalences=(
                EquivalenceRecord(
                    case_id=variant.variant_id,
                    passed=True,
                    left_hash="a",
                    right_hash="a",
                    left_count=1,
                    right_count=1,
                    reason="equal",
                ),
            ),
            oracle=oracle,
            confirmation=confirmation,
            artifacts=artifacts,
            state="confirmed" if index == 0 else "equivalent_no_candidate",
            reason=confirmation.reason,
            pmc=pmc,
        )

    monkeypatch.setattr("graph_sym_perf.app.mining.generate_transform_family", fake_generate)
    monkeypatch.setattr("graph_sym_perf.app.mining._run_family", fake_run_family)

    records, outputs = _run_query_cases(config, writer, [_FakeAdapter()], _query_cases())
    run_dir = tmp_path / "run-smoke"

    assert len(records) == 2
    assert outputs["pmc_summary"].total_units_seen >= 2
    assert (run_dir / "pmc_summary.json").exists()
    assert (run_dir / "pmc_evidence.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads((run_dir / "pmc_units.json").read_text(encoding="utf-8"))
    assert (run_dir / "feedback_summary.json").exists()
    assert (run_dir / "rule_stats.json").exists()
    assert (run_dir / "neo4j" / "case_001" / "pmc_evidence.json").exists()

    pmc_summary = json.loads((run_dir / "pmc_summary.json").read_text(encoding="utf-8"))
    feedback_summary = json.loads((run_dir / "feedback_summary.json").read_text(encoding="utf-8"))

    assert pmc_summary["total_new_units"] >= 1
    assert "top_rules" in feedback_summary


def test_bool_pairwise_case_level_pmc_matches_strongest_oracle(tmp_path: Path, monkeypatch) -> None:
    config = load_run_config("config/graph_sym_perf.yaml")
    config = replace(config, paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"))
    writer = ArtifactWriter(config, "run-bool-pairwise")
    family = QueryFamily(
        case_id="case_pairwise",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(
            QueryVariant(
                variant_id="case_pairwise_01_weak_rule",
                query="MATCH (n) WHERE true RETURN n",
                transform_rule="weak_rule",
                proof_obligation="proof",
                safety_tier="strict",
                metadata={"transform_family": "bool_redundancy"},
            ),
            QueryVariant(
                variant_id="case_pairwise_02_strong_rule",
                query="MATCH (n) WHERE true OR false RETURN n",
                transform_rule="strong_rule",
                proof_obligation="proof",
                safety_tier="strict",
                metadata={"transform_family": "bool_redundancy"},
            ),
        ),
        source="gdsmith",
    )

    class Adapter:
        target = TargetSystem.NEO4J

        def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
            return ExecutionResult(
                system=TargetSystem.NEO4J,
                query_kind=query_kind,
                rows=({"n": 1},),
                elapsed_ms=1.0,
                summary={},
            )

    def fake_profile(config, adapter, variant_id, query, parameters):
        median = {"seed": 10.0, "case_pairwise_01_weak_rule": 20.0, "case_pairwise_02_strong_rule": 100.0}[variant_id]
        return VariantProfileSummary(
            variant_id=variant_id,
            query=query,
            query_kind=variant_id,
            median_ms=median,
            samples_ms=(median,),
            plan=None,
            profile_plan=None,
            metrics={"db_hits": median},
        )

    def fake_oracle_from_profiles(config, writer, adapter, family, comparable, artifacts, base, *, prefix):
        slow = max(comparable, key=lambda item: item.median_ms or 0.0)
        strong = slow.variant_id == "case_pairwise_02_strong_rule"
        oracle = OracleRecord(
            case_id=family.case_id,
            is_candidate=True,
            candidate_types=("OutlierVariant",),
            reason="candidate",
            evidence={
                "support_level": "profile_and_runtime_supported" if strong else "plan_only",
                "oracle_layer_status": {"runtime": "pass" if strong else "fail"},
            },
        )
        confirmation = ConfirmationRecord(
            case_id=family.case_id,
            confirmed=strong,
            original_median_ms=10.0,
            transformed_median_ms=slow.median_ms,
            latency_ratio=10.0 if strong else 2.0,
            latency_delta_ms=90.0 if strong else 10.0,
            original_samples_ms=(10.0,),
            transformed_samples_ms=(slow.median_ms or 0.0,),
            reason="candidate",
            support_level="profile_and_runtime_supported" if strong else "plan_only",
            fast_variant_id="seed",
            slow_variant_id=slow.variant_id,
        )
        return oracle, confirmation, _plan_diff(10.0 if strong else 2.0), comparable[0], slow

    def fake_pmc(config, family, fast, slow, plan_diff, oracle, confirmation, *, safety_tier):
        rule = next(variant.transform_rule for variant in family.variants if variant.variant_id == slow.variant_id)
        return make_pmc_evidence(
            case_id=family.case_id,
            transform_family="bool_redundancy",
            transform_rule=rule,
            safety_tier=safety_tier,
            fast_variant_id="seed",
            slow_variant_id=slow.variant_id,
            plan_diff=_plan_diff(10.0 if rule == "strong_rule" else 2.0),
            coverage_config=config.coverage,
            metadata={"is_candidate": oracle.is_candidate},
        )

    monkeypatch.setattr(mining, "_profile_variant", fake_profile)
    monkeypatch.setattr(mining, "_oracle_from_profiles", fake_oracle_from_profiles)
    monkeypatch.setattr(mining, "_build_pmc_evidence", fake_pmc)

    record = mining._run_family(config, writer, Adapter(), family, writer.write_query_family("neo4j", family))

    assert record.confirmation is not None
    assert record.confirmation.support_level == "profile_and_runtime_supported"
    assert record.pmc is not None
    assert record.pmc.transform_rule == "strong_rule"


def test_attach_pmc_does_not_observe_case_level_pair_twice(tmp_path: Path) -> None:
    config = load_run_config("config/graph_sym_perf.yaml")
    config = replace(config, paths=RunPaths(artifact_dir=f"{tmp_path.as_posix()}/{{run_id}}"))
    writer = ArtifactWriter(config, "run-attach-pmc")
    family = QueryFamily(
        case_id="case_attach",
        seed_query="MATCH (n) RETURN n",
        parameters={},
        variants=(),
        source="gdsmith",
    )
    first = make_pmc_evidence(
        case_id="case_attach",
        transform_family="bool_redundancy",
        transform_rule="first_rule",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="first",
        plan_diff=_plan_diff(2.0),
        coverage_config=config.coverage,
    )
    second = make_pmc_evidence(
        case_id="case_attach",
        transform_family="bool_redundancy",
        transform_rule="second_rule",
        safety_tier="strict",
        fast_variant_id="seed",
        slow_variant_id="second",
        plan_diff=_plan_diff(10.0),
        coverage_config=config.coverage,
    )
    record = CaseRecord(
        system=TargetSystem.NEO4J,
        case_id="case_attach",
        family=family,
        equivalences=(),
        oracle=None,
        confirmation=None,
        artifacts={},
        state="equivalent_no_candidate",
        reason="test",
        pmc=second,
        _per_variant_pmcs=(first, second),
    )

    _, observed = mining._attach_pmc(writer, config, PMCCoverageState(), record)

    assert [item.transform_rule for item in observed] == ["second_rule", "first_rule"]
