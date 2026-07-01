from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.cluster import (
    cluster_run_directory,
    render_clustered_summary_markdown,
    write_clustered_summary,
    write_review_candidates,
)
from graph_sym_perf.core import to_jsonable


def _candidate(case_id: str, *, support_level: str = "profile_supported", latency_ratio: float = 3.5) -> dict:
    return {
        "case_id": case_id,
        "system": "neo4j",
        "artifacts": {
            "fast_query": f"neo4j/{case_id}/seed_query.cypher",
            "slow_query": f"neo4j/{case_id}/case_01.cypher",
            "symmetry_family": f"neo4j/{case_id}/symmetry_family.json",
        },
        "confirmation": {
            "support_level": support_level,
            "latency_ratio": latency_ratio,
            "latency_delta_ms": 120.0,
            "fast_variant_id": "seed",
            "slow_variant_id": "case_01",
        },
        "oracle": {
            "is_candidate": True,
            "candidate_types": ["AccessPointAsymmetry", "OutlierVariant"],
            "evidence": {"latency": {"ratio": latency_ratio, "delta_ms": 120.0}},
        },
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
    assert "Candidate Families" in markdown
    assert "Representative" in markdown
    assert updated_summary["issue_family_count"] == 1


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


def test_cluster_run_directory_groups_by_candidate_type(tmp_path: Path) -> None:
    _write_cases(tmp_path, [_candidate("case_001"), _candidate("case_002")])

    summary = cluster_run_directory(tmp_path)

    assert summary["candidate_count"] == 2
    assert summary["issue_family_count"] == 1
    assert "AccessPointAsymmetry,OutlierVariant" in summary["issue_families"]
