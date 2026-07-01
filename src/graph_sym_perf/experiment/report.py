"""Reader-facing experiment report export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.experiment.aggregate import aggregate_run_root


def export_run_report(run_dir: str | Path) -> Mapping[str, Any]:
    root = Path(run_dir)
    aggregate_path = root / "aggregate_summary.json"
    if not aggregate_path.exists():
        shard_dirs = tuple(sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("shard_"))) if root.exists() else ()
        if shard_dirs:
            aggregate_run_root(root)
        else:
            raise FileNotFoundError(f"missing aggregate summary: {aggregate_path}")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    triage = _load_json(root / "triage_top100.json") or _load_json(root / "triage_top20.json") or {}
    validation = _load_json(root / "validation_summary.json") or {}
    payload = {
        "run_dir": str(root),
        "aggregate": aggregate,
        "triage": triage,
        "validation": validation,
    }
    (root / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "report.md").write_text(_render_report_markdown(payload), encoding="utf-8")
    return payload


def _load_json(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _render_report_markdown(payload: Mapping[str, Any]) -> str:
    aggregate = payload.get("aggregate") or {}
    triage = payload.get("triage") or {}
    validation = payload.get("validation") or {}
    lines = [
        f"# Experiment Report: {Path(str(payload.get('run_dir'))).name}",
        "",
        "## Aggregate",
        "",
        f"- Shards: {aggregate.get('shard_count')}",
        f"- Unique candidate signatures: {aggregate.get('candidate_count')}",
        f"- Unique issues: {aggregate.get('unique_issue_count')}",
        f"- Issue families: {aggregate.get('issue_family_count')}",
        "",
        "## Triage",
        "",
        f"- Triaged representatives: {len((triage.get('items') or []))}",
        "",
        "## Validation",
        "",
        f"- Validated representatives: {validation.get('validated', 0)}",
        f"- Equivalence passing: {validation.get('passed', 0)}",
    ]
    return "\n".join(lines) + "\n"
