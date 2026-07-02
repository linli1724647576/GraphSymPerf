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
        f"- Core unit observations: {summary.total_units_seen_core or summary.total_units_seen}",
        f"- Core new units: {summary.total_new_units_core or summary.total_new_units}",
        f"- All unit observations: {summary.total_units_seen_all or summary.total_units_seen}",
        f"- All new units: {summary.total_new_units_all or summary.total_new_units}",
        f"- Context units: {sum((summary.context_units or {}).values())}",
        f"- Context new units: {sum((summary.context_new_units or {}).values())}",
        "",
        "## Core Units By Kind",
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
            "## Top Core Units",
            "",
            "| Unit Key | Kind | Count | Label |",
            "|---|---|---:|---|",
        ]
    )
    for unit in summary.top_units:
        lines.append(
            f"| {unit.get('key')} | {unit.get('kind')} | {unit.get('count')} | {unit.get('label')} |"
        )
    if summary.top_context_units:
        lines.extend(
            [
                "",
                "## Context Units",
                "",
                "| Unit Key | Count | Label |",
                "|---|---:|---|",
            ]
        )
        for unit in summary.top_context_units:
            lines.append(f"| {unit.get('key')} | {unit.get('count')} | {unit.get('label')} |")
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
    unit_counts: dict[tuple[str, str], int] = {}
    unit_payloads: dict[tuple[str, str], dict[str, object]] = {}
    new_unit_keys = {
        (unit.kind, unit.key)
        for evidence in evidence_items
        for unit in evidence.new_units
    }
    for evidence in evidence_items:
        for unit in evidence.units:
            token = (unit.kind, unit.key)
            unit_counts[token] = unit_counts.get(token, 0) + 1
            unit_payloads.setdefault(
                token,
                {
                    "kind": unit.kind,
                    "key": unit.key,
                    "label": unit.label,
                    "weight": unit.weight,
                    "metadata": dict(unit.metadata),
                },
            )
    (path / "pmc_units.json").write_text(
        json.dumps(
            [
                dict(
                    unit_payloads[token],
                    count=unit_counts[token],
                    first_seen_in_run=token in new_unit_keys,
                )
                for token in sorted(unit_payloads, key=lambda item: (item[0], item[1]))
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
