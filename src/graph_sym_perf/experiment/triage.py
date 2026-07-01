"""Top-candidate triage and validation."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.config import environment_with_dotenv
from graph_sym_perf.confirm.runtime import evaluate_confirmation
from graph_sym_perf.core import ConfigError, MetricDelta, ProfileRecord, QueryPair
from graph_sym_perf.experiment.manifest import load_experiment_manifest


def run_triage(run_dir: str | Path, *, top: int = 100) -> Mapping[str, Any]:
    root = Path(run_dir)
    unique_issues_path = root / "aggregate_unique_issues.json"
    if not unique_issues_path.exists():
        raise FileNotFoundError(f"missing {unique_issues_path}")
    issues = json.loads(unique_issues_path.read_text(encoding="utf-8"))
    ranked = list(issues)
    ranked.sort(key=_triage_sort_key)
    selected = [dict(item, rank=index) for index, item in enumerate(ranked[:top], start=1)]
    payload = {"run_dir": str(root), "top": top, "items": selected}
    _write_json(root / f"triage_top{top}.json", payload)
    _write_markdown(root / f"triage_top{top}.md", payload)
    _write_csv(root / f"triage_top{top}.csv", selected)
    return payload


def validate_triage(
    triage_path: str | Path,
    *,
    runtime_repeats: int = 15,
    reduce: bool = False,
) -> Mapping[str, Any]:
    triage_file = Path(triage_path)
    payload = json.loads(triage_file.read_text(encoding="utf-8"))
    root = triage_file.parent
    manifest_path = root / "aggregate_experiment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing {manifest_path}")
    manifest = load_experiment_manifest(manifest_path)
    env = environment_with_dotenv()
    run_config = manifest.to_run_config()
    results: list[Mapping[str, Any]] = []
    for item in payload.get("items") or ():
        system = str(item.get("system") or "")
        system_config = next((cfg for cfg in run_config.systems if cfg.target.value == system), None)
        if system_config is None:
            raise ConfigError(f"system {system} missing from aggregate manifest")
        adapter = create_adapter(system_config, run_config, env, allow_create=False)
        try:
            case_dir = Path(str(item.get("case_dir")))
            result = _validate_one(
                adapter,
                case_dir,
                runtime_repeats=runtime_repeats,
                reduce=reduce,
                top_item=item,
                run_config=run_config,
            )
            results.append(result)
        finally:
            adapter.close()
    summary = {
        "triage_path": str(triage_file),
        "validated": len(results),
        "passed": sum(1 for item in results if item.get("equivalence_passed")),
        "runtime_supported": sum(1 for item in results if item.get("support_level") == "profile_supported"),
        "results": results,
    }
    _write_json(root / "validation_summary.json", summary)
    return summary


def _validate_one(adapter, case_dir: Path, *, runtime_repeats: int, reduce: bool, top_item: Mapping[str, Any], run_config) -> Mapping[str, Any]:
    artifacts = top_item.get("artifact_paths") or {}
    parameters = json.loads((case_dir / "parameters.json").read_text(encoding="utf-8"))
    fast_query = _read_query(case_dir, artifacts.get("fast_query"))
    slow_query = _read_query(case_dir, artifacts.get("slow_query"))
    base_rows = adapter.execute(fast_query, parameters, "validate_fast", max_rows=run_config.equivalence.max_result_rows).rows
    transformed_rows = adapter.execute(
        slow_query,
        parameters,
        "validate_slow",
        max_rows=run_config.equivalence.max_result_rows,
    ).rows
    equivalence_passed = base_rows == transformed_rows
    original_profiles: list[ProfileRecord] = []
    transformed_profiles: list[ProfileRecord] = []
    original_samples: list[float] = []
    transformed_samples: list[float] = []
    for repeat in range(max(1, runtime_repeats)):
        fast_result = adapter.profile(fast_query, parameters, f"validate_fast_{repeat}", timeout_seconds=run_config.confirmation.timeout_seconds)
        slow_result = adapter.profile(slow_query, parameters, f"validate_slow_{repeat}", timeout_seconds=run_config.confirmation.timeout_seconds)
        original_samples.append(float(fast_result.elapsed_ms))
        transformed_samples.append(float(slow_result.elapsed_ms))
        original_profiles.append(_profile_from_execution(adapter, fast_result))
        transformed_profiles.append(_profile_from_execution(adapter, slow_result))
    confirmation = evaluate_confirmation(
        str(top_item.get("representative_case_id") or top_item.get("case_id")),
        tuple(original_samples),
        tuple(transformed_samples),
        replace(run_config.confirmation, repeats=runtime_repeats),
        tuple(original_profiles),
        tuple(transformed_profiles),
    )
    reducer_summary = {
        "mode": "no_op",
        "query_length": min(len(fast_query), len(slow_query)),
        "requested": bool(reduce),
    }
    issue_draft_path = None
    if equivalence_passed and confirmation.confirmed:
        issue_draft_path = case_dir.parent.parent.parent / "issue_drafts" / f"{top_item.get('representative_case_id')}.md"
        issue_draft_path.parent.mkdir(parents=True, exist_ok=True)
        issue_draft_path.write_text(_render_issue_draft(top_item, confirmation, reducer_summary), encoding="utf-8")
    return {
        "case_id": top_item.get("representative_case_id"),
        "system": top_item.get("system"),
        "equivalence_passed": equivalence_passed,
        "support_level": confirmation.support_level,
        "latency_ratio": confirmation.latency_ratio,
        "latency_delta_ms": confirmation.latency_delta_ms,
        "db_hits_ratio": _metric_ratio(confirmation.metric_deltas, "db_hits"),
        "rows_ratio": _metric_ratio(confirmation.metric_deltas, "rows"),
        "reducer": reducer_summary,
        "issue_draft_path": str(issue_draft_path) if issue_draft_path is not None else None,
    }


def _profile_from_execution(adapter, execution_result) -> ProfileRecord:
    from graph_sym_perf.plan import profile_record_from_execution

    return profile_record_from_execution(execution_result)


def _metric_ratio(deltas: tuple[MetricDelta, ...], metric: str) -> float | None:
    for delta in deltas:
        if delta.metric == metric:
            return delta.ratio
    return None


def _read_query(case_dir: Path, artifact_path: Any) -> str:
    if artifact_path is None:
        raise FileNotFoundError(f"missing query artifact under {case_dir}")
    path = case_dir.parents[1] / str(artifact_path)
    return path.read_text(encoding="utf-8")


def _triage_sort_key(item: Mapping[str, Any]) -> tuple[int, int, int, float, float, float, float, int, str]:
    return (
        -_developer_rank(str(item.get("claim_level") or "")),
        -_support_rank(str(item.get("support_level") or "")),
        -_root_cause_rank(tuple(item.get("root_cause_labels") or ())),
        -float(item.get("db_hits_ratio") or 0.0),
        -float(item.get("rows_ratio") or 0.0),
        -float(item.get("latency_ratio") or 0.0),
        -float(item.get("pmc_new_units") or 0.0),
        int(item.get("query_length") or 10**9),
        str(item.get("representative_case_id") or ""),
    )


def _developer_rank(value: str) -> int:
    return {
        "fixed": 5,
        "developer_confirmed": 4,
        "confirmed": 3,
        "reported": 2,
        "strong_candidate": 1,
    }.get(value, 0)


def _support_rank(value: str) -> int:
    return {
        "profile_supported": 4,
        "profile_supported_without_db_hits": 3,
        "latency_supported": 2,
        "plan_only": 1,
        "none": 0,
    }.get(value, 0)


def _root_cause_rank(labels: tuple[str, ...]) -> int:
    if not labels:
        return 0
    if labels[0] in {"PredicatePushdownBlocked", "AccessPathRegression", "WorkInflation"}:
        return 3
    if labels[0] != "UnknownPlanAnomaly":
        return 2
    return 1


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    items = tuple(payload.get("items") or ())
    lines = [
        f"# Triage Top {payload.get('top')}",
        "",
        "| Rank | Case | System | Support | Root Cause | Latency Ratio | PMC New Units |",
        "|---:|---|---|---|---|---:|---:|",
    ]
    for item in items:
        lines.append(
            "| {rank} | {case} | {system} | {support} | {root} | {ratio} | {pmc} |".format(
                rank=item.get("rank"),
                case=item.get("representative_case_id"),
                system=item.get("system"),
                support=item.get("support_level"),
                root=",".join(item.get("root_cause_labels") or ()),
                ratio=item.get("latency_ratio") or "",
                pmc=item.get("pmc_new_units") or 0,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_issue_draft(item: Mapping[str, Any], confirmation, reducer_summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"# Issue Draft: {item.get('representative_case_id')}",
            "",
            f"- System: {item.get('system')}",
            f"- Root cause labels: {', '.join(item.get('root_cause_labels') or ())}",
            f"- Support level after validation: {confirmation.support_level}",
            f"- Latency ratio: {confirmation.latency_ratio}",
            f"- Latency delta ms: {confirmation.latency_delta_ms}",
            f"- Reducer: {reducer_summary.get('mode')}",
        ]
    ) + "\n"
