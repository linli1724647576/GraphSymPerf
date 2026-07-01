"""Review candidate extraction for symmetry violations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.core import stable_json_dumps, to_jsonable


def write_review_candidates(run_dir: str | Path, *, update_summary: bool = True) -> Mapping[str, Any]:
    path = Path(run_dir)
    records = _load_case_records(path / "cases.jsonl")
    candidates = tuple(
        dict(candidate, rank=index)
        for index, candidate in enumerate(sorted(_candidate_items(records), key=_sort_key), start=1)
    )
    payload: Mapping[str, Any] = {
        "run_id": path.name,
        "candidate_count": len(candidates),
        "selection_policy": "oracle.is_candidate == true",
        "ranking_policy": "sort by support level, latency ratio, latency delta, then case id",
        "candidates": candidates,
    }
    _write_json(path / "review_candidates.json", payload)
    (path / "review_candidates.md").write_text(render_review_candidates_markdown(payload), encoding="utf-8")
    if update_summary:
        _update_summary(path, payload)
    return payload


def write_exploratory_leads(run_dir: str | Path, *, update_summary: bool = True) -> Mapping[str, Any]:
    path = Path(run_dir)
    records = _load_case_records(path / "cases.jsonl")
    leads = tuple(
        dict(candidate, rank=index)
        for index, candidate in enumerate(sorted(_exploratory_items(records), key=_sort_key), start=1)
    )
    payload: Mapping[str, Any] = {
        "run_id": path.name,
        "lead_count": len(leads),
        "selection_policy": "exploratory_oracle.is_candidate == true",
        "ranking_policy": "sort by support level, latency ratio, latency delta, then case id",
        "warning": "Exploratory leads are not issue-ready until promoted to strict semantic proof rules.",
        "leads": leads,
    }
    _write_json(path / "exploratory_leads.json", payload)
    (path / "exploratory_leads.md").write_text(render_exploratory_leads_markdown(payload), encoding="utf-8")
    if update_summary:
        _update_summary(path, {"exploratory_lead_count": len(leads), "exploratory_leads_artifact": "exploratory_leads.json"})
    return payload


def cluster_run_directory(run_dir: str | Path) -> Mapping[str, Any]:
    path = Path(run_dir)
    records = _load_case_records(path / "cases.jsonl")
    candidates = tuple(_candidate_items(records))
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        key = ",".join(candidate.get("candidate_types") or ("unknown",))
        groups.setdefault(key, []).append(candidate)
    return {
        "run_id": path.name,
        "candidate_count": len(candidates),
        "issue_family_count": len(groups),
        "issue_families": {
            key: {
                "size": len(items),
                "representative_case_id": sorted(str(item["case_id"]) for item in items)[0],
            }
            for key, items in sorted(groups.items())
        },
    }


def write_clustered_summary(run_dir: str | Path, *, update_summary: bool = True) -> Mapping[str, Any]:
    path = Path(run_dir)
    payload = cluster_run_directory(path)
    _write_json(path / "clustered_summary.json", payload)
    (path / "clustered_summary.md").write_text(render_clustered_summary_markdown(payload), encoding="utf-8")
    if update_summary:
        _update_summary(path, payload)
    return payload


def render_review_candidates_markdown(payload: Mapping[str, Any]) -> str:
    candidates = tuple(payload.get("candidates") or ())
    lines = [
        f"# GraphSymPerf Review Candidates: {payload.get('run_id')}",
        "",
        "## Conclusion",
        "",
        f"This run produced {len(candidates)} symmetry-violation candidates.",
        "",
        "| Rank | Case | Support | Types | Fast | Slow | Latency Ratio | Delta ms | Artifacts |",
        "|---:|---|---|---|---|---|---:|---:|---|",
    ]
    for candidate in candidates:
        lines.append(
            "| {rank} | {case} | {support} | {types} | {fast} | {slow} | {ratio} | {delta} | {artifacts} |".format(
                rank=candidate.get("rank"),
                case=candidate.get("case_id"),
                support=candidate.get("support_level"),
                types=", ".join(candidate.get("candidate_types") or ()),
                fast=candidate.get("fast_variant_id"),
                slow=candidate.get("slow_variant_id"),
                ratio=_fmt(candidate.get("latency_ratio")),
                delta=_fmt(candidate.get("latency_delta_ms")),
                artifacts=_artifact_links(candidate.get("artifacts") or {}),
            )
        )
    lines.extend(
        [
            "",
            "## Review Checklist",
            "",
            "1. Re-run the fast and slow Cypher files in the same dedicated database.",
            "2. Confirm result equivalence before interpreting latency or PROFILE differences.",
            "3. Inspect `plan_diff.json`, `oracle.json`, and `profile_summaries.json` for the causal plan signal.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_exploratory_leads_markdown(payload: Mapping[str, Any]) -> str:
    leads = tuple(payload.get("leads") or ())
    lines = [
        f"# GraphSymPerf Exploratory Leads: {payload.get('run_id')}",
        "",
        "## Conclusion",
        "",
        f"This run produced {len(leads)} exploratory leads.",
        "",
        "These leads are not issue-ready until the responsible transformation is promoted to a strict semantic proof rule.",
        "",
        "| Rank | Case | Support | Types | Fast | Slow | Latency Ratio | Delta ms | Artifacts |",
        "|---:|---|---|---|---|---|---:|---:|---|",
    ]
    for candidate in leads:
        lines.append(
            "| {rank} | {case} | {support} | {types} | {fast} | {slow} | {ratio} | {delta} | {artifacts} |".format(
                rank=candidate.get("rank"),
                case=candidate.get("case_id"),
                support=candidate.get("support_level"),
                types=", ".join(candidate.get("candidate_types") or ()),
                fast=candidate.get("fast_variant_id"),
                slow=candidate.get("slow_variant_id"),
                ratio=_fmt(candidate.get("latency_ratio")),
                delta=_fmt(candidate.get("latency_delta_ms")),
                artifacts=_artifact_links(candidate.get("artifacts") or {}, prefix="exploratory_"),
            )
        )
    return "\n".join(lines) + "\n"


def render_clustered_summary_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# GraphSymPerf Candidate Families: {payload.get('run_id')}",
        "",
        f"Candidate count: {payload.get('candidate_count', 0)}",
        f"Issue family count: {payload.get('issue_family_count', 0)}",
        "",
        "| Family | Size | Representative |",
        "|---|---:|---|",
    ]
    for key, item in (payload.get("issue_families") or {}).items():
        lines.append(f"| {key} | {item.get('size')} | {item.get('representative_case_id')} |")
    return "\n".join(lines) + "\n"


def _candidate_items(records: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    items: list[Mapping[str, Any]] = []
    for record in records:
        oracle = record.get("oracle") or {}
        confirmation = record.get("confirmation") or {}
        if not oracle.get("is_candidate"):
            continue
        evidence = oracle.get("evidence") or {}
        latency = evidence.get("latency") or {}
        items.append(
            {
                "case_id": record.get("case_id"),
                "system": record.get("system"),
                "candidate_types": tuple(oracle.get("candidate_types") or ()),
                "support_level": confirmation.get("support_level", "none"),
                "fast_variant_id": confirmation.get("fast_variant_id") or evidence.get("fast_variant_id"),
                "slow_variant_id": confirmation.get("slow_variant_id") or evidence.get("slow_variant_id"),
                "latency_ratio": confirmation.get("latency_ratio") or latency.get("ratio"),
                "latency_delta_ms": confirmation.get("latency_delta_ms") or latency.get("delta_ms"),
                "artifacts": record.get("artifacts") or {},
            }
        )
    return tuple(items)


def _exploratory_items(records: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    items: list[Mapping[str, Any]] = []
    for record in records:
        oracle = record.get("exploratory_oracle") or {}
        confirmation = record.get("exploratory_confirmation") or {}
        if not oracle.get("is_candidate"):
            continue
        evidence = oracle.get("evidence") or {}
        latency = evidence.get("latency") or {}
        items.append(
            {
                "case_id": record.get("case_id"),
                "system": record.get("system"),
                "candidate_types": tuple(oracle.get("candidate_types") or ()),
                "support_level": confirmation.get("support_level", "none"),
                "fast_variant_id": confirmation.get("fast_variant_id") or evidence.get("fast_variant_id"),
                "slow_variant_id": confirmation.get("slow_variant_id") or evidence.get("slow_variant_id"),
                "latency_ratio": confirmation.get("latency_ratio") or latency.get("ratio"),
                "latency_delta_ms": confirmation.get("latency_delta_ms") or latency.get("delta_ms"),
                "artifacts": record.get("artifacts") or {},
            }
        )
    return tuple(items)


def _sort_key(candidate: Mapping[str, Any]) -> tuple[int, float, float, str]:
    support_rank = {"profile_supported": 3, "latency_supported": 2, "plan_only": 1, "none": 0}
    return (
        -support_rank.get(str(candidate.get("support_level")), 0),
        -float(candidate.get("latency_ratio") or 0.0),
        -float(candidate.get("latency_delta_ms") or 0.0),
        str(candidate.get("case_id")),
    )


def _artifact_links(artifacts: Mapping[str, Any], *, prefix: str = "") -> str:
    wanted = (f"{prefix}fast_query", f"{prefix}slow_query", "symmetry_family")
    values = [str(artifacts[key]) for key in wanted if key in artifacts]
    return ", ".join(values)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _load_case_records(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.exists():
        return ()
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on {path}:{line_number}: {exc}") from exc
        if not isinstance(record, Mapping):
            raise ValueError(f"case record on {path}:{line_number} must be a JSON object")
        records.append(record)
    return tuple(records)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _update_summary(path: Path, payload: Mapping[str, Any]) -> None:
    summary_path = path / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(json.loads(stable_json_dumps(payload)))
    _write_json(summary_path, summary)
