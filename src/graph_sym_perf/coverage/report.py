"""PMC artifact rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from graph_sym_perf.core import stable_json_dumps, to_jsonable
from graph_sym_perf.coverage.model import PMCEvidence, PMCSummary


def render_pmc_summary_markdown(summary: PMCSummary) -> str:
    lines = [
        "# Plan Metamorphic Coverage Summary",
        "",
        f"- Run ID: {summary.run_id}",
        f"- Total unit observations: {summary.total_units_seen}",
        f"- Total new units: {summary.total_new_units}",
        "",
        "## Units By Kind",
        "",
        "| Kind | Count | New | Candidate |",
        "|---|---:|---:|---:|",
    ]
    candidate_units = summary.candidate_units_by_kind or {}
    for kind, count in summary.units_by_kind.items():
        lines.append(
            f"| {kind} | {count} | {summary.new_units_by_kind.get(kind, 0)} | {candidate_units.get(kind, 0)} |"
        )
    lines.extend(
        [
            "",
            "## Top Units",
            "",
            "| Unit Key | Kind | Count | Label |",
            "|---|---|---:|---|",
        ]
    )
    for unit in summary.top_units:
        lines.append(
            f"| {unit.get('key')} | {unit.get('kind')} | {unit.get('count')} | {unit.get('label')} |"
        )
    return "\n".join(lines) + "\n"


def write_pmc_artifacts(
    run_dir: str | Path,
    summary: PMCSummary,
    per_case_evidence: Iterable[PMCEvidence],
) -> None:
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    evidence_items = tuple(per_case_evidence)
    (path / "pmc_summary.json").write_text(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (path / "pmc_summary.md").write_text(render_pmc_summary_markdown(summary), encoding="utf-8")
    with (path / "pmc_evidence.jsonl").open("w", encoding="utf-8") as file:
        for evidence in evidence_items:
            file.write(stable_json_dumps(evidence) + "\n")
    (path / "pmc_units.json").write_text(
        json.dumps(
            [
                {
                    "case_id": evidence.case_id,
                    "transform_family": evidence.transform_family,
                    "transform_rule": evidence.transform_rule,
                    "safety_tier": evidence.safety_tier,
                    "unit_keys": [unit.key for unit in evidence.units],
                    "new_unit_keys": [unit.key for unit in evidence.new_units],
                }
                for evidence in evidence_items
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
