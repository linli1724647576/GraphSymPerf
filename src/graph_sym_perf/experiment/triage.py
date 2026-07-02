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
from graph_sym_perf.core import ConfigError, MetricDelta, ProfileRecord, is_profile_supported_level, support_rank
from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.issues import build_issue_tracking_artifacts
from graph_sym_perf.reducer import reduce_candidate


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
    issue_search: bool = False,
) -> Mapping[str, Any]:
    triage_file = Path(triage_path)
    payload = json.loads(triage_file.read_text(encoding="utf-8"))
    root = triage_file.parent
    manifest_path = root / "aggregate_experiment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing {manifest_path}")
    manifest = load_experiment_manifest(manifest_path)
    env = environment_with_dotenv(None)
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
                manifest=manifest,
            )
            results.append(result)
        finally:
            adapter.close()
    summary = {
        "triage_path": str(triage_file),
        "validated": len(results),
        "passed": sum(1 for item in results if item.get("equivalence_passed")),
        "runtime_supported": sum(1 for item in results if is_profile_supported_level(item.get("support_level"))),
        "results": results,
    }
    issue_tracking = build_issue_tracking_artifacts(
        root,
        payload,
        summary,
        issue_search=issue_search,
    )
    summary["issue_tracking"] = issue_tracking
    _write_json(root / "validation_summary.json", summary)
    return summary


def _validate_one(
    adapter,
    case_dir: Path,
    *,
    runtime_repeats: int,
    reduce: bool,
    top_item: Mapping[str, Any],
    run_config,
    manifest,
) -> Mapping[str, Any]:
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
    reducer_summary = _run_reducer(
        adapter,
        case_dir,
        top_item=top_item,
        fast_query=fast_query,
        slow_query=slow_query,
        parameters=parameters,
        run_config=run_config,
        manifest=manifest,
        runtime_repeats=runtime_repeats,
        requested=reduce,
    )
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
        "issue_url": _artifact_issue_url(top_item),
        "developer_status": _developer_status(top_item),
    }


def _run_reducer(
    adapter,
    case_dir: Path,
    *,
    top_item: Mapping[str, Any],
    fast_query: str,
    slow_query: str,
    parameters: Mapping[str, Any],
    run_config,
    manifest,
    runtime_repeats: int,
    requested: bool,
) -> Mapping[str, Any]:
    if not requested:
        return {
            "mode": "not_requested",
            "query_length": min(len(fast_query), len(slow_query)),
            "requested": False,
        }
    reducer_config = manifest.reducer or {}
    if not _reducer_enabled(reducer_config):
        return {
            "mode": "disabled",
            "query_length": min(len(fast_query), len(slow_query)),
            "requested": True,
            "config": dict(reducer_config),
        }
    reduce_graph_requested = bool(reducer_config.get("reduce_graph", True))
    try:
        setup_statements, reload_graph, graph_status = _reducer_graph_setup(
            adapter,
            manifest,
            top_item,
            reduce_graph=reduce_graph_requested,
        )
    except ConfigError as exc:
        setup_statements = ()
        reload_graph = None
        reduce_graph_requested = False
        graph_status = {"status": "blocked_destructive_guard", "error": str(exc)}
    try:
        summary = reduce_candidate(
            adapter=adapter,
            case_id=str(top_item.get("representative_case_id") or top_item.get("case_id") or case_dir.name),
            case_dir=case_dir,
            fast_query=fast_query,
            slow_query=slow_query,
            parameters=parameters,
            confirmation_config=run_config.confirmation,
            max_result_rows=run_config.equivalence.max_result_rows,
            timeout_seconds=run_config.confirmation.timeout_seconds,
            runtime_repeats=runtime_repeats,
            preserve_metric_ratio=float(reducer_config.get("preserve_metric_ratio", 1.5)),
            max_query_attempts=int(reducer_config.get("max_query_attempts", 32)),
            setup_statements=setup_statements,
            reload_graph=reload_graph,
            reduce_graph=reduce_graph_requested,
        )
    except Exception as exc:
        return {
            "mode": "validated_reducer",
            "requested": True,
            "status": "failed",
            "error": str(exc),
            "graph_reduction": graph_status,
        }
    if graph_status.get("status") != "available" and (summary.get("graph_reduction") or {}).get("status") in {
        "not_requested",
        "unavailable_no_setup_artifact",
    }:
        summary = dict(summary)
        summary["graph_reduction"] = graph_status
    return summary


def _reducer_enabled(reducer_config: Mapping[str, Any]) -> bool:
    enabled = reducer_config.get("enabled", "selective")
    if isinstance(enabled, bool):
        if not enabled:
            return False
    elif str(enabled).strip().lower() in {"false", "0", "disabled", "never", "no", "no_op"}:
        return False
    strategy = str(reducer_config.get("strategy", "validated")).strip().lower()
    return strategy not in {"disabled", "none", "no_op"}


def _reducer_graph_setup(adapter, manifest, top_item: Mapping[str, Any], *, reduce_graph: bool):
    setup_path = _setup_artifact_path(manifest, top_item)
    if not reduce_graph:
        return (), None, {"status": "not_requested"}
    if setup_path is None:
        return (), None, {"status": "unavailable_no_setup_artifact"}
    if not setup_path.exists():
        return (), None, {"status": "unavailable_setup_artifact_missing", "setup_path": str(setup_path)}
    from graph_sym_perf.experiment.runner import (
        _require_destructive_replay_guard,
        _reset_adapter_for_known_issue_setup,
        _split_setup_statements,
    )

    _require_destructive_replay_guard()
    statements = _split_setup_statements(setup_path.read_text(encoding="utf-8"))

    def reload_graph(trial_statements: tuple[str, ...]) -> None:
        _reset_adapter_for_known_issue_setup(adapter)
        for statement in trial_statements:
            adapter.execute(
                statement,
                {},
                "reducer_graph_setup",
                timeout_seconds=float(manifest.execution.per_query_timeout_seconds),
            )

    return statements, reload_graph, {"status": "available", "setup_path": str(setup_path), "statement_count": len(statements)}


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
    raw_path = Path(str(artifact_path))
    path = raw_path if raw_path.is_absolute() else case_dir.parents[1] / raw_path
    return path.read_text(encoding="utf-8")


def _setup_artifact_path(manifest, top_item: Mapping[str, Any]) -> Path | None:
    artifacts = top_item.get("artifact_paths") or {}
    raw = artifacts.get("setup_path") or top_item.get("setup_path")
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    candidates = [
        Path.cwd() / path,
        Path(str(manifest.metadata.get("manifest_path") or ".")).parent / path,
    ]
    case_dir = top_item.get("case_dir")
    if case_dir:
        candidates.insert(0, Path(str(case_dir)).parents[1] / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _artifact_issue_url(top_item: Mapping[str, Any]) -> str | None:
    artifacts = top_item.get("artifact_paths") or {}
    value = artifacts.get("issue_url") or top_item.get("issue_url")
    return str(value) if value else None


def _developer_status(top_item: Mapping[str, Any]) -> str:
    artifacts = top_item.get("artifact_paths") or {}
    value = artifacts.get("developer_status") or top_item.get("developer_status") or top_item.get("claim_level")
    return str(value) if value else "unknown"


def _support_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


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
    return support_rank(value)


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
    query_reduction = reducer_summary.get("query_reduction") or {}
    graph_reduction = reducer_summary.get("graph_reduction") or {}
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
            f"- Reduced fast query: {query_reduction.get('fast_query_path') or ''}",
            f"- Reduced slow query: {query_reduction.get('slow_query_path') or ''}",
            f"- Reduced graph: {graph_reduction.get('graph_path') or graph_reduction.get('status') or ''}",
            f"- Existing issue URL: {_artifact_issue_url(item) or ''}",
        ]
    ) + "\n"
