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
    assert report["overall_status"] == "FAIL"
    assert report["pmc_valid"] is False
    assert report["rule_stats_valid"] is False
    assert report["oracle_valid"] is False
    assert report["artifact_completeness"] is False
    assert "pmc_evidence_empty" in codes
    assert "pmc_units_empty" in codes
    assert "rule_outcomes_gt_generated" in codes
    assert "runtime_confirmed_mismatch" in codes
    assert "actual_rule_not_enabled" in codes
    assert "artifact_path_missing" in codes
    assert (tmp_path / "calibration_health_report.json").exists()
    assert (tmp_path / "calibration_health_report.md").exists()


def test_calibration_health_reports_warn_and_fail_context_pollution_levels(tmp_path: Path) -> None:
    common = {
        "summary.json": {"generated_cases": 1, "runtime_confirmed": 0, "states": {}, "pmc_total_units_seen_all": 10},
        "pmc_units.json": [
            {"kind": "access_delta", "key": f"access:{i}", "count": 1}
            for i in range(6)
        ]
        + [
            {"kind": "transformation_context", "key": f"context:{i}", "count": 1}
            for i in range(4)
        ],
        "rule_stats.json": [],
        "cases.jsonl": "",
    }
    for name, payload in common.items():
        path = tmp_path / name
        if name.endswith(".jsonl"):
            path.write_text(str(payload), encoding="utf-8")
        else:
            _write_json(path, payload)
    (tmp_path / "pmc_evidence.jsonl").write_text(
        "\n".join(
            json.dumps({"case_id": "case_001", "units": [{"kind": "access_delta", "key": f"access:{i}"}]})
            for i in range(6)
        )
        + "\n"
        + "\n".join(
            json.dumps({"case_id": "case_001", "units": [{"kind": "transformation_context", "key": f"context:{i}"}]})
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )
    case_dir = tmp_path / "neo4j" / "case_001"
    case_dir.mkdir(parents=True)
    _write_json(case_dir / "pmc_evidence.json", {"case_id": "case_001", "units": []})
    _write_json(
        tmp_path / "pmc_summary.json",
        {
            "total_units_seen_all": 10,
            "total_units_seen_core": 6,
            "context_units": {"transformation_context": 4},
            "top_units": [],
        },
    )

    report = run_calibration_health(tmp_path)

    assert report["context_pollution_level"] == "WARN"
    assert report["overall_status"] == "WARN"

    _write_json(
        tmp_path / "pmc_units.json",
        [
            {"kind": "access_delta", "key": f"access:{i}", "count": 1}
            for i in range(2)
        ]
        + [
            {"kind": "transformation_context", "key": f"context:{i}", "count": 1}
            for i in range(8)
        ],
    )
    (tmp_path / "pmc_evidence.jsonl").write_text(
        "\n".join(
            json.dumps({"case_id": "case_001", "units": [{"kind": "access_delta", "key": f"access:{i}"}]})
            for i in range(2)
        )
        + "\n"
        + "\n".join(
            json.dumps({"case_id": "case_001", "units": [{"kind": "transformation_context", "key": f"context:{i}"}]})
            for i in range(8)
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "pmc_summary.json",
        {
            "total_units_seen_all": 10,
            "total_units_seen_core": 2,
            "context_units": {"transformation_context": 8},
            "top_units": [],
        },
    )

    report = run_calibration_health(tmp_path)

    assert report["context_pollution_level"] == "FAIL"
    assert report["overall_status"] == "FAIL"
