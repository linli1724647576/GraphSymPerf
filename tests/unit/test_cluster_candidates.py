from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.cluster import cluster_run_directory, render_clustered_summary_markdown, write_clustered_summary, write_review_candidates
from graph_sym_perf.core import to_jsonable


def _plan_diff(*, access_regressed: bool = True, db_hits_ratio: float = 10.0, filter_delta: int = 1) -> dict:
    return {
        "structural_hash_changed": True,
        "changes": [{"change": "replaced"}],
        "added_nodes": [{"operator_family": "Filter", "operator_type": "Filter"}] if filter_delta > 0 else [],
        "removed_nodes": [],
        "replaced_nodes": [],
        "family_delta": {"Filter": filter_delta},
        "access_point_delta": {
            "changed": access_regressed,
            "regressed": access_regressed,
            "original_best_rank": 1,
            "transformed_best_rank": 4 if access_regressed else 1,
            "original": [{"kind": "IndexSeek", "variable": "n", "label": "Person"}],
            "transformed": [{"kind": "AllNodesScan", "variable": "n", "label": "Person"}] if access_regressed else [{"kind": "IndexSeek", "variable": "n", "label": "Person"}],
        },
        "expand_chain_delta": {"changed": False, "delta": 0, "original": [], "transformed": []},
        "barrier_delta": {"changed": False, "delta": 0, "original": [], "transformed": []},
        "estimated_rows_delta": {"ratio": db_hits_ratio, "worsened": True},
        "metric_deltas": [
            {
                "metric": "db_hits",
                "original": 10.0,
                "transformed": 10.0 * db_hits_ratio,
                "ratio": db_hits_ratio,
                "delta": 10.0 * (db_hits_ratio - 1.0),
                "worsened": True,
            }
        ],
    }


def _candidate(
    case_id: str,
    *,
    support_level: str = "profile_supported",
    latency_ratio: float = 10.0,
    db_hits_ratio: float = 10.0,
) -> dict:
    transform_rule = "pattern_order_symmetry"
    return {
        "case_id": case_id,
        "system": "neo4j",
        "family": {
            "case_id": case_id,
            "seed_query": "MATCH (n)-[:KNOWS]->(m) RETURN n, m",
            "parameters": {},
            "variants": [
                {
                    "variant_id": f"{case_id}_01_{transform_rule}",
                    "query": "MATCH (m)<-[:KNOWS]-(n) RETURN n, m",
                    "transform_rule": transform_rule,
                    "proof_obligation": "proof",
                    "safety_tier": "strict",
                    "metadata": {"transform_family": "symmetry"},
                }
            ],
            "skipped_variants": [],
            "source": "gdsmith",
        },
        "equivalences": [
            {
                "case_id": f"{case_id}_01_{transform_rule}",
                "passed": True,
                "left_hash": "a",
                "right_hash": "a",
                "left_count": 1,
                "right_count": 1,
                "reason": "equal",
            }
        ],
        "artifacts": {
            "fast_query": f"neo4j/{case_id}/seed_query.cypher",
            "slow_query": f"neo4j/{case_id}/case_01.cypher",
            "symmetry_family": f"neo4j/{case_id}/symmetry_family.json",
            "pmc_evidence": f"neo4j/{case_id}/pmc_evidence.json",
        },
        "confirmation": {
            "support_level": support_level,
            "latency_ratio": latency_ratio,
            "latency_delta_ms": 120.0,
            "fast_variant_id": "seed",
            "slow_variant_id": f"{case_id}_01_{transform_rule}",
            "metric_deltas": [
                {
                    "metric": "db_hits",
                    "original": 10.0,
                    "transformed": 10.0 * db_hits_ratio,
                    "ratio": db_hits_ratio,
                    "delta": 10.0 * (db_hits_ratio - 1.0),
                    "worsened": True,
                }
            ],
        },
        "oracle": {
            "is_candidate": True,
            "candidate_types": ["AccessPointAsymmetry", "OutlierVariant"],
            "reason": "candidate",
            "evidence": {"latency": {"ratio": latency_ratio, "delta_ms": 120.0}, "operator_tree_diff": _plan_diff(db_hits_ratio=db_hits_ratio)},
        },
        "pmc": {
            "case_id": case_id,
            "transform_family": "symmetry",
            "transform_rule": transform_rule,
            "safety_tier": "strict",
            "fast_variant_id": "seed",
            "slow_variant_id": f"{case_id}_01_{transform_rule}",
            "structural_hash_changed": True,
            "units": [
                {"kind": "access_delta", "key": "access_regressed", "label": "Access regressed"},
                {"kind": "work_delta", "key": "db_hits_ratio_bucket:10x" if db_hits_ratio >= 10 else "db_hits_ratio_bucket:2x", "label": "DB hits ratio bucket"},
            ],
            "new_units": [{"kind": "access_delta", "key": "access_regressed", "label": "Access regressed"}],
            "unit_count": 2,
            "new_unit_count": 1,
            "novelty_score": 3.0,
            "metadata": {"is_candidate": True},
        },
        "state": "confirmed" if support_level == "profile_supported" else "equivalent_no_candidate",
        "reason": "candidate",
    }


def _write_cases(run_dir: Path, records: list[dict]) -> None:
    run_dir.mkdir(exist_ok=True)
    (run_dir / "summary.json").write_text('{"run_id":"cluster-test"}\n', encoding="utf-8")
    (run_dir / "cases.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def test_clustered_summary_artifacts_are_json_and_markdown(tmp_path: Path) -> None:
    _write_cases(tmp_path, [_candidate("case_001")])

    summary = write_clustered_summary(tmp_path)
    payload = to_jsonable(summary)
    markdown = render_clustered_summary_markdown(summary)
    updated_summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))

    assert payload["candidate_count"] == 1
    assert (tmp_path / "clustered_summary.json").exists()
    assert (tmp_path / "clustered_summary.md").exists()
    assert (tmp_path / "candidate_signatures.jsonl").exists()
    assert (tmp_path / "issue_families.json").exists()
    assert (tmp_path / "unique_issues.json").exists()
    assert (tmp_path / "duplicate_groups.json").exists()
    assert "Candidate Clustering Summary" in markdown
    assert "Representative Cases" in markdown
    assert updated_summary["issue_family_count"] == 1
    assert updated_summary["unique_issue_count"] == 1


def test_clustered_summary_handles_missing_cases_jsonl(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text('{"run_id":"empty-run"}\n', encoding="utf-8")

    summary = write_clustered_summary(tmp_path)
    review = write_review_candidates(tmp_path)
    updated_summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))

    assert summary["candidate_count"] == 0
    assert review["candidate_count"] == 0
    assert updated_summary["candidate_count"] == 0
    assert (tmp_path / "clustered_summary.md").exists()


def test_review_candidates_exports_sorted_list(tmp_path: Path) -> None:
    _write_cases(
        tmp_path,
        [
            _candidate("case_001", latency_ratio=3.5),
            _candidate("case_002", latency_ratio=8.0),
            _candidate("case_003", support_level="plan_only", latency_ratio=99.0),
        ],
    )

    payload = write_review_candidates(tmp_path)
    markdown = (tmp_path / "review_candidates.md").read_text(encoding="utf-8")

    assert payload["candidate_count"] == 3
    assert payload["candidates"][0]["case_id"] == "case_002"
    assert "GraphSymPerf Review Candidates" in markdown
    assert "case_001" in markdown


def test_cluster_run_directory_deduplicates_and_prefers_profile_supported_representative(tmp_path: Path) -> None:
    _write_cases(
        tmp_path,
        [
            _candidate("case_001", support_level="profile_supported", db_hits_ratio=10.0),
            _candidate("case_002", support_level="profile_supported", db_hits_ratio=10.0),
            _candidate("case_003", support_level="plan_only", db_hits_ratio=2.0),
        ],
    )

    summary = cluster_run_directory(tmp_path)

    assert summary["candidate_count"] == 3
    assert summary["issue_family_count"] == 1
    assert summary["unique_issue_count"] == 2
    assert summary["duplicate_group_count"] == 1
    assert summary["representatives"][0]["representative_case_id"] in {"case_001", "case_002"}
