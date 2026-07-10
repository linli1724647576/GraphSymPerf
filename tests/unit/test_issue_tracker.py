from __future__ import annotations

import json
from pathlib import Path

import yaml

from graph_sym_perf.issues import build_issue_tracking_artifacts


def test_issue_tracking_writes_reportable_and_status_artifacts(tmp_path: Path) -> None:
    triage_payload = {
        "items": [
            {
                "representative_case_id": "case001",
                "system": "neo4j",
                "cluster_key": "neo4j|symmetry|root",
                "root_cause_labels": ("PredicatePushdownBlocked",),
                "transform_rules": {"predicate_order_symmetry": 1},
                "artifact_paths": {"issue_url": "https://github.com/neo4j/neo4j/issues/123"},
            }
        ]
    }
    validation_summary = {
        "results": [
            {
                "case_id": "case001",
                "system": "neo4j",
                "equivalence_passed": True,
                "pipeline_stage": "submit_ready",
                "reduction_status": "reduced_valid",
                "latency_ratio": 5.0,
                "db_hits_ratio": 8.0,
                "reducer": {
                    "artifact_dir": str(tmp_path / "neo4j" / "case001" / "reduced"),
                    "query_reduction": {"fast_query_path": "fast.cypher", "slow_query_path": "slow.cypher"},
                    "graph_reduction": {"graph_path": "graph.cypher"},
                },
                "issue_draft_path": "issue_drafts/case001.md",
            }
        ]
    }

    payload = build_issue_tracking_artifacts(tmp_path, triage_payload, validation_summary, issue_search=False)

    assert payload["reportable_count"] == 1
    reportable = json.loads((tmp_path / "reportable_issues.json").read_text(encoding="utf-8"))
    reported = yaml.safe_load((tmp_path / "reported_issues.yaml").read_text(encoding="utf-8"))
    assert reportable["issues"][0]["status"] == "reported"
    assert reportable["issues"][0]["issue_url"].endswith("/123")
    assert reported["issues"][0]["case_id"] == "case001"
    assert (tmp_path / "reported_issues.csv").exists()
    assert (tmp_path / "issue_status_table.csv").exists()
    assert (tmp_path / "developer_feedback_summary.md").exists()


def test_issue_tracking_uses_searcher_for_possible_duplicates(tmp_path: Path) -> None:
    triage_payload = {
        "items": [
            {
                "representative_case_id": "case002",
                "system": "memgraph",
                "root_cause_labels": ("WorkInflation",),
                "transform_rules": {"or_false_and": 1},
            }
        ]
    }
    validation_summary = {
        "results": [
            {
                "case_id": "case002",
                "system": "memgraph",
                "equivalence_passed": True,
                "pipeline_stage": "submit_ready",
                "reduction_status": "reduced_valid",
                "latency_ratio": 12.0,
                "reducer": {},
            }
        ]
    }

    def fake_searcher(item):
        assert item["case_id"] == "case002"
        return {
            "status": "searched",
            "repository": "memgraph/memgraph",
            "items": ({"html_url": "https://github.com/memgraph/memgraph/issues/4208", "title": "Similar"},),
        }

    build_issue_tracking_artifacts(tmp_path, triage_payload, validation_summary, issue_search=True, searcher=fake_searcher)

    reportable = json.loads((tmp_path / "reportable_issues.json").read_text(encoding="utf-8"))
    assert reportable["issues"][0]["status"] == "possible_duplicate"
    assert reportable["issues"][0]["duplicate_of"].endswith("/4208")


def test_issue_tracking_requires_submit_ready_stage_not_profile_support(tmp_path: Path) -> None:
    triage_payload = {
        "items": [
            {
                "representative_case_id": "case003",
                "system": "neo4j",
                "root_cause_labels": ("WorkInflation",),
                "transform_rules": {"pattern_order_symmetry": 1},
            }
        ]
    }
    validation_summary = {
        "results": [
            {
                "case_id": "case003",
                "system": "neo4j",
                "equivalence_passed": True,
                "support_level": "profile_and_runtime_supported",
                "pipeline_stage": "screened",
                "reduction_status": "reduced_valid",
                "latency_ratio": 12.0,
                "reducer": {},
            }
        ]
    }

    build_issue_tracking_artifacts(tmp_path, triage_payload, validation_summary, issue_search=False)

    reportable = json.loads((tmp_path / "reportable_issues.json").read_text(encoding="utf-8"))
    assert reportable["issues"] == []


def test_issue_tracking_uses_submit_ready_as_the_only_reportability_stage_gate(tmp_path: Path) -> None:
    triage_payload = {"items": [{"representative_case_id": "case004", "system": "neo4j"}]}
    validation_summary = {
        "results": [
            {
                "case_id": "case004",
                "system": "neo4j",
                "pipeline_stage": "submit_ready",
                "profile_supported": False,
                "plan_changed": False,
                "pmc_novelty": 0,
            }
        ]
    }

    payload = build_issue_tracking_artifacts(tmp_path, triage_payload, validation_summary)

    assert payload["reportable_count"] == 1


def test_reported_issue_count_counts_submitted_not_waiting_submit_ready(tmp_path: Path) -> None:
    triage_payload = {"items": [{"representative_case_id": "case005", "system": "neo4j"}]}
    validation_summary = {
        "results": [{"case_id": "case005", "system": "neo4j", "pipeline_stage": "submit_ready"}]
    }

    payload = build_issue_tracking_artifacts(tmp_path, triage_payload, validation_summary)

    assert payload["reportable_count"] == 1
    assert payload["reported_issue_count"] == 0
    reported = yaml.safe_load((tmp_path / "reported_issues.yaml").read_text(encoding="utf-8"))
    assert reported["issues"] == []
