from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.experiment.aggregate import aggregate_run_root
from graph_sym_perf.experiment.triage import run_triage


def test_aggregate_and_triage_emit_expected_artifacts(tmp_path: Path) -> None:
    run_root = tmp_path / "runs" / "main_effectiveness_2026_01"
    shard_dir = run_root / "shard_000"
    case_dir = shard_dir / "neo4j" / "main_effectiveness_2026_01__neo4j__shard000__case00000__seed20260522"
    case_dir.mkdir(parents=True, exist_ok=True)

    _write_jsonl(
        shard_dir / "cases.jsonl",
        [
            {
                "system": "neo4j",
                "case_id": case_dir.name,
                "state": "confirmed",
                "oracle": {"is_candidate": True},
                "confirmation": {
                    "support_level": "profile_supported",
                    "latency_ratio": 12.5,
                    "latency_delta_ms": 120.0,
                },
            }
        ],
    )
    _write_jsonl(
        shard_dir / "candidate_signatures.jsonl",
        [
            {
                "case_id": case_dir.name,
                "system": "neo4j",
                "transform_family": "symmetry",
                "transform_rule": "predicate_order_symmetry",
                "safety_tier": "strict",
                "candidate_types": ["latency"],
                "root_cause_labels": ["PredicatePushdownBlocked"],
                "support_level": "profile_supported",
                "claim_level": "strong_candidate",
                "plan_signature": "sig-plan",
                "metric_signature": "sig-metric",
                "pmc_signature": "sig-pmc",
                "issue_family_key": "neo4j|symmetry|PredicatePushdownBlocked|latency|predicate_delta",
                "unique_issue_key": "neo4j|symmetry|predicate_order_symmetry|PredicatePushdownBlocked|sig-plan|sig-metric",
                "duplicate_key": "neo4j|predicate_order_symmetry|sig-plan|sig-pmc",
                "metadata": {
                    "developer_status": "reported",
                    "latency_ratio": 12.5,
                    "latency_delta_ms": 120.0,
                    "db_hits_ratio": 8.0,
                    "rows_ratio": 4.0,
                    "pmc_new_units": 3,
                    "query_length": 88,
                    "artifact_paths": {
                        "fast_query": "neo4j/%s/fast_query.cypher" % case_dir.name,
                        "slow_query": "neo4j/%s/slow_query.cypher" % case_dir.name,
                    },
                },
            }
        ],
    )
    _write_jsonl(
        shard_dir / "pmc_evidence.jsonl",
        [
            {
                "case_id": case_dir.name,
                "transform_family": "symmetry",
                "transform_rule": "predicate_order_symmetry",
                "safety_tier": "strict",
                "fast_variant_id": "seed",
                "slow_variant_id": "variant",
                "structural_hash_changed": True,
                "units": [
                    {
                        "kind": "predicate_delta",
                        "key": "predicate_delta:Filter:+1",
                        "label": "Filter delta",
                        "weight": 1.0,
                        "metadata": {},
                    }
                ],
                "metadata": {"is_candidate": True},
            }
        ],
    )
    (shard_dir / "rule_stats.json").write_text(
        json.dumps(
            [
                {
                    "family": "symmetry",
                    "rule": "predicate_order_symmetry",
                    "attempted": 1,
                    "generated": 1,
                    "candidate_count": 1,
                    "pmc_new_units": 3,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (shard_dir / "experiment_manifest.json").write_text("{}", encoding="utf-8")
    (case_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    (case_dir / "fast_query.cypher").write_text("RETURN 1 AS ok\n", encoding="utf-8")
    (case_dir / "slow_query.cypher").write_text("RETURN 1 AS ok\n", encoding="utf-8")

    summary = aggregate_run_root(run_root)
    triage = run_triage(run_root, top=10)

    assert summary["candidate_count"] == 1
    assert summary["unique_issue_count"] == 1
    assert (run_root / "aggregate_summary.json").exists()
    assert (run_root / "aggregate_candidate_signatures.jsonl").exists()
    assert (run_root / "tables" / "unique_issues.csv").exists()
    assert triage["items"][0]["representative_case_id"] == case_dir.name
    assert (run_root / "triage_top10.json").exists()


def test_aggregate_prefers_profile_and_runtime_supported_representative(tmp_path: Path) -> None:
    run_root = tmp_path / "runs" / "main_effectiveness_2026_01"
    shard_dir = run_root / "shard_000"
    shard_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(shard_dir / "cases.jsonl", [])
    _write_jsonl(
        shard_dir / "candidate_signatures.jsonl",
        [
            {
                "case_id": "case_profile",
                "system": "neo4j",
                "transform_family": "symmetry",
                "transform_rule": "predicate_order_symmetry",
                "safety_tier": "strict",
                "candidate_types": ["latency"],
                "root_cause_labels": ["PredicatePushdownBlocked"],
                "support_level": "profile_supported",
                "claim_level": "strong_candidate",
                "plan_signature": "sig-plan",
                "metric_signature": "sig-metric",
                "pmc_signature": "sig-pmc",
                "issue_family_key": "family",
                "unique_issue_key": "unique",
                "duplicate_key": "dup",
                "metadata": {"latency_ratio": 200.0, "db_hits_ratio": 200.0, "rows_ratio": 200.0},
            },
            {
                "case_id": "case_runtime",
                "system": "neo4j",
                "transform_family": "symmetry",
                "transform_rule": "predicate_order_symmetry",
                "safety_tier": "strict",
                "candidate_types": ["latency"],
                "root_cause_labels": ["PredicatePushdownBlocked"],
                "support_level": "profile_and_runtime_supported",
                "claim_level": "strong_candidate",
                "plan_signature": "sig-plan",
                "metric_signature": "sig-metric",
                "pmc_signature": "sig-pmc",
                "issue_family_key": "family",
                "unique_issue_key": "unique",
                "duplicate_key": "dup",
                "metadata": {"latency_ratio": 10.0, "db_hits_ratio": 10.0, "rows_ratio": 10.0},
            },
        ],
    )
    _write_jsonl(shard_dir / "pmc_evidence.jsonl", [])
    (shard_dir / "rule_stats.json").write_text("[]\n", encoding="utf-8")

    aggregate_run_root(run_root)
    signatures = [
        json.loads(line)
        for line in (run_root / "aggregate_candidate_signatures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert signatures[0]["case_id"] == "case_runtime"


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
