"""Preflight checks for experiment manifests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf import __version__
from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.app.mining import _preflight
from graph_sym_perf.config import environment_with_dotenv
from graph_sym_perf.core import ConfigError, SystemStatus, to_jsonable
from graph_sym_perf.experiment.manifest import load_experiment_manifest

EXIT_OK = 0
EXIT_PREFLIGHT_FAILED = 4


def run_doctor(manifest_path: str | Path, *, strict: bool = False) -> int:
    env = environment_with_dotenv(None)
    manifest = load_experiment_manifest(manifest_path)
    run_config = manifest.to_run_config()
    preflight_dir = manifest.output_root / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)

    statuses: list[Mapping[str, Any]] = []
    blocking_issue = False
    for system in manifest.systems:
        adapter = create_adapter(system, run_config, env, allow_create=bool(manifest.source.get("load")))
        try:
            status = _preflight(adapter)
            checks = _capability_checks(adapter, manifest.execution.per_query_timeout_seconds, status)
            record = {
                "system": system.target.value,
                "status": to_jsonable(status),
                "capabilities": to_jsonable(system.capabilities),
                "checks": checks,
                "settings": dict(system.settings),
            }
            statuses.append(record)
            if status.state != "ready" or any(item.get("level") == "error" for item in checks):
                blocking_issue = True
        finally:
            adapter.close()

    disk = shutil.disk_usage(preflight_dir)
    package_info = {
        "graph_sym_perf_version": __version__,
        "git_commit": _git_commit(),
        "manifest_path": str(Path(manifest_path).resolve()),
        "output_root": str(manifest.output_root.resolve()),
    }
    payload = {
        "experiment_id": manifest.experiment_id,
        "description": manifest.description,
        "package": package_info,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "execution": to_jsonable(manifest.execution),
        "statuses": statuses,
    }
    (preflight_dir / "preflight.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (preflight_dir / "preflight.md").write_text(_render_preflight_markdown(payload), encoding="utf-8")
    if strict and blocking_issue:
        return EXIT_PREFLIGHT_FAILED
    return EXIT_OK


def _capability_checks(adapter, timeout_seconds: int, status: SystemStatus) -> list[Mapping[str, Any]]:
    checks: list[Mapping[str, Any]] = []
    if not status.ready:
        if status.missing_env:
            checks.append(
                {
                    "name": "environment",
                    "level": "warning",
                    "message": f"missing env: {', '.join(status.missing_env)}",
                }
            )
        return checks

    smoke_query = "RETURN 1 AS ok"
    checks.append(_run_check("execute", lambda: adapter.execute(smoke_query, {}, "doctor_execute", timeout_seconds=timeout_seconds)))
    if adapter.system_config.capabilities.supports_explain:
        checks.append(_run_check("explain", lambda: adapter.explain(smoke_query, {}, "doctor_explain")))
    else:
        checks.append({"name": "explain", "level": "warning", "message": "capability disabled"})
    if adapter.system_config.capabilities.supports_profile:
        checks.append(
            _run_check(
                "profile",
                lambda: adapter.profile(smoke_query, {}, "doctor_profile", timeout_seconds=timeout_seconds),
            )
        )
    else:
        checks.append({"name": "profile", "level": "warning", "message": "capability disabled"})
    return checks


def _run_check(name: str, fn) -> Mapping[str, Any]:
    try:
        result = fn()
    except Exception as exc:  # pragma: no cover - backend-specific failure text
        return {"name": name, "level": "error", "message": str(exc)}
    return {"name": name, "level": "ok", "message": "ok", "summary": _result_summary(result)}


def _result_summary(result: Any) -> Mapping[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "elapsed_ms"):
        return {"elapsed_ms": getattr(result, "elapsed_ms", None)}
    if hasattr(result, "available"):
        return {"available": getattr(result, "available", None)}
    return {"type": type(result).__name__}


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return completed.stdout.strip() or None


def _render_preflight_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# GraphSymPerf Preflight: {payload.get('experiment_id')}",
        "",
        f"- Output root: {((payload.get('package') or {}).get('output_root'))}",
        f"- GraphSymPerf version: {((payload.get('package') or {}).get('graph_sym_perf_version'))}",
        f"- Git commit: {((payload.get('package') or {}).get('git_commit') or 'unknown')}",
        f"- Free disk bytes: {((payload.get('disk') or {}).get('free_bytes'))}",
        "",
        "| System | State | Execute | Explain | Profile |",
        "|---|---|---|---|---|",
    ]
    for item in payload.get("statuses") or ():
        checks = {check.get("name"): check for check in item.get("checks") or ()}
        status = item.get("status") or {}
        lines.append(
            "| {system} | {state} | {execute} | {explain} | {profile} |".format(
                system=item.get("system"),
                state=status.get("state"),
                execute=(checks.get("execute") or {}).get("level", ""),
                explain=(checks.get("explain") or {}).get("level", ""),
                profile=(checks.get("profile") or {}).get("level", ""),
            )
        )
    return "\n".join(lines) + "\n"
