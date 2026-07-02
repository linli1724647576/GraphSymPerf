from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.experiment.calibration_health import run_calibration_health


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def test_calibration_health_flags_documented_calibration_inconsistencies(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "summary.json",
        {
            "generated_cases": 2,
            "runtime_confirmed": 1,
            "states": {"skipped": 1, "profile_supported_candidate": 1},
            "transform_rules": {"where_and_true_or_expr": 1},
            "pmc_total_units_seen": 3,
        },
    )
    _write_json(tmp_path / "pmc_summary.json", {"total_units_seen": 3, "top_units": []})
    (tmp_path / "pmc_evidence.jsonl").write_text("", encoding="utf-8")
    _write_json(tmp_path / "pmc_units.json", [])
    _write_json(
        tmp_path / "rule_stats.json",
        [
            {
                "family": "symmetry",
                "rule": "predicate_order_symmetry",
                "attempted": 10,
                "generated": 1,
                "equivalence_passed": 0,
                "equivalence_failed": 9,
                "errors": 2,
                "timeouts": 0,
                "skipped": 9,
            }
        ],
    )
    _write_json(
        tmp_path / "run_config_snapshot.json",
        {"transforms": {"enabled_rules": ["and_true_or", "predicate_order_symmetry"]}},
    )
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "system": "neo4j",
                "case_id": "case_001",
                "state": "profile_supported_candidate",
                "reason": "candidate",
                "family": {
                    "variants": [
                        {
                            "variant_id": "case_001_01_where_and_true_or_expr",
                            "transform_rule": "where_and_true_or_expr",
                            "metadata": {"transform_family": "bool_redundancy"},
                        }
                    ],
                    "skipped_variants": [],
                },
                "oracle": {"is_candidate": True, "evidence": {"latency": {"worsened": False}}},
                "confirmation": {"support_level": "profile_supported"},
                "artifacts": {"fast_query": "neo4j/case_001/fast_query.cypher"},
            },
            sort_keys=True,
        )
        + "\n"
        + json.dumps(
            {
                "system": "memgraph",
                "case_id": "case_002",
                "state": "skipped",
                "reason": "GDSMITH_QUERY_NOT_REPLAYABLE_ON_TARGET",
                "family": {"variants": [], "skipped_variants": []},
                "oracle": None,
                "confirmation": None,
                "artifacts": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_calibration_health(tmp_path)
    codes = {issue["code"] for issue in report["issues"]}

    assert not report["ok"]
    assert "pmc_evidence_empty" in codes
    assert "pmc_units_empty" in codes
    assert "rule_outcomes_gt_generated" in codes
    assert "runtime_confirmed_mismatch" in codes
    assert "actual_rule_not_enabled" in codes
    assert "artifact_path_missing" in codes
    assert (tmp_path / "calibration_health_report.json").exists()
    assert (tmp_path / "calibration_health_report.md").exists()
