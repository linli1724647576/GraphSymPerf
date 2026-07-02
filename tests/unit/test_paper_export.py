from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from graph_sym_perf.experiment.aggregate import aggregate_run_root
from graph_sym_perf.experiment.paper_export import PAPER_FIGURES, PAPER_TABLES
from graph_sym_perf.experiment.report import export_run_report


def test_report_exports_schedule_required_tables_and_figures(tmp_path: Path) -> None:
    run_root = tmp_path / "runs" / "main_effectiveness_2026_01"
    shard_dir = run_root / "shard_000"
    case_id = "main_effectiveness_2026_01__neo4j__shard000__case00000__seed20260522"
    case_dir = shard_dir / "neo4j" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        shard_dir / "cases.jsonl",
        [
            {
                "system": "neo4j",
                "case_id": case_id,
                "state": "confirmed",
                "equivalences": [{"passed": True}],
                "oracle": {"is_candidate": True},
                "confirmation": {"support_level": "profile_and_runtime_supported"},
            }
        ],
    )
    _write_jsonl(
        shard_dir / "candidate_signatures.jsonl",
        [
            {
                "case_id": case_id,
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
                "issue_family_key": "neo4j|symmetry|PredicatePushdownBlocked|latency|predicate_delta",
                "unique_issue_key": "neo4j|symmetry|predicate_order_symmetry|PredicatePushdownBlocked|sig-plan|sig-metric",
                "duplicate_key": "neo4j|predicate_order_symmetry|sig-plan|sig-pmc",
                "metadata": {
                    "latency_ratio": 12.5,
                    "pmc_new_units": 3,
                    "artifact_paths": {
                        "fast_query": f"neo4j/{case_id}/fast_query.cypher",
                        "slow_query": f"neo4j/{case_id}/slow_query.cypher",
                    },
                },
            }
        ],
    )
    _write_jsonl(
        shard_dir / "pmc_evidence.jsonl",
        [
            {
                "case_id": case_id,
                "transform_family": "symmetry",
                "transform_rule": "predicate_order_symmetry",
                "safety_tier": "strict",
                "structural_hash_changed": True,
                "units": [{"kind": "predicate_delta", "key": "predicate_delta:Filter:+1", "label": "Filter delta"}],
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
                    "reward": 12.0,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (shard_dir / "preflight.json").write_text(
        json.dumps({"systems": [{"system": "neo4j", "state": "ready", "dependency_ok": True, "environment_ok": True, "connectivity_ok": True}]})
        + "\n",
        encoding="utf-8",
    )
    (shard_dir / "experiment_manifest.json").write_text(
        json.dumps({"systems": [{"target": "neo4j", "required_env": ["NEO4J_URI"], "capabilities": {"profile": True}}]}) + "\n",
        encoding="utf-8",
    )
    (run_root / "reported_issues.yaml").write_text(
        yaml.safe_dump({"issues": [{"case_id": case_id, "system": "neo4j", "developer_status": "reported", "status": "reported"}]}),
        encoding="utf-8",
    )
    ablation = tmp_path / "runs" / "ablation_bool_static"
    ablation.mkdir(parents=True)
    (ablation / "aggregate_summary.json").write_text(
        json.dumps({"record_count": 10, "candidate_count": 2, "unique_issue_count": 1, "support_levels": {"profile_supported": 1}}) + "\n",
        encoding="utf-8",
    )
    (ablation / "aggregate_pmc_summary.json").write_text(json.dumps({"total_units_seen": 5}) + "\n", encoding="utf-8")

    aggregate_run_root(run_root)
    payload = export_run_report(run_root, ablation_runs=(ablation,), oracle_run=ablation)

    assert payload["paper_exports"]["table_count"] == len(PAPER_TABLES)
    assert payload["paper_exports"]["figure_count"] == len(PAPER_FIGURES)
    for filename in PAPER_TABLES:
        path = run_root / "tables" / filename
        assert path.exists(), filename
        assert path.read_text(encoding="utf-8").splitlines()[0]
    for filename in PAPER_FIGURES:
        path = run_root / "figures" / filename
        assert path.exists(), filename
        assert path.read_text(encoding="utf-8").splitlines()[0]
    ablation_rows = (run_root / "tables" / "ablation_comparison.csv").read_text(encoding="utf-8")
    assert "ablation_bool_static" in ablation_rows
    funnel_rows = list(csv.DictReader((run_root / "tables" / "candidate_funnel.csv").open(encoding="utf-8")))
    overall = next(row for row in funnel_rows if row["scope"] == "overall")
    assert overall["profile_supported_candidates"] == "1"


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
