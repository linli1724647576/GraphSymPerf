"""Preflight for the runtime-first formal matrix."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import GdsmithConfig, TargetSystem
from graph_sym_perf.experiment.planner import build_run_plan
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.gdsmith.runner import check_gdsmith_tooling
from graph_sym_perf.experiment.provenance import verified_execution_provenance


@dataclass(frozen=True)
class SystemDoctorResult:
    system: str
    required_env_present: bool
    generation_env_present: bool
    generation_connectivity: str
    validation_provenance_present: bool
    execution_provenance_mode: str | None
    execution_artifact_digest: str | None
    driver_available: bool
    connectivity: str
    connectivity_reason: str
    graph_isolation: str


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    gdsmith_ready: bool
    gdsmith_dirty: bool | None
    gdsmith_commit_matches: bool
    gdsmith_jar_sha256: str | None
    gdsmith_build_manifest_sha256: str | None
    disk_free_bytes: int
    disk_required_bytes: int
    disk_ready: bool
    systems: tuple[SystemDoctorResult, ...]


_DRIVERS = {
    TargetSystem.NEO4J: "neo4j",
    TargetSystem.MEMGRAPH: "neo4j",
    TargetSystem.FALKORDB: "falkordb",
    TargetSystem.AGENSGRAPH: "psycopg",
    TargetSystem.APACHE_AGE: "psycopg",
    TargetSystem.ARCADEDB: "requests",
    TargetSystem.LADYBUGDB: "ladybug",
}


def run_doctor(
    config_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
    output_root: str | Path = ".",
) -> DoctorReport:
    spec = load_experiment_spec(config_path)
    return run_doctor_spec(spec, environment=environment, output_root=output_root)


def run_doctor_spec(
    spec,
    *,
    environment: Mapping[str, str] | None = None,
    output_root: str | Path = ".",
) -> DoctorReport:
    env = dict(os.environ if environment is None else environment)
    output_path = Path(output_root)
    output_path.mkdir(parents=True, exist_ok=True)
    plan = build_run_plan(spec, environment=env)
    gdsmith = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit=spec.generation.expected_commit),
        env,
    )
    results = tuple(_check_system(target, definition, spec, env) for target, definition in spec.systems.items())
    disk_free = shutil.disk_usage(output_path).free
    disk_required = len(plan.tasks) * int(spec.artifacts["shard_budget_mb"]) * 1024 * 1024
    disk_ready = disk_free >= disk_required
    source_ready = (
        plan.source_revision.provenance_complete
        and plan.source_revision.repository_dirty is False
        and plan.source_revision.gdsmith_dirty is False
        and plan.source_revision.gdsmith_commit == spec.generation.expected_commit
    )
    ready = gdsmith.ready and source_ready and disk_ready and all(
        result.required_env_present
        and result.generation_env_present
        and result.generation_connectivity in {"ready", "not_required"}
        and result.validation_provenance_present
        and result.driver_available
        and result.connectivity == "ready"
        and result.graph_isolation
        for result in results
    )
    report = DoctorReport(
        ready,
        gdsmith.ready,
        plan.source_revision.gdsmith_dirty,
        plan.source_revision.gdsmith_commit == spec.generation.expected_commit,
        gdsmith.jar_sha256,
        gdsmith.build_manifest_sha256,
        disk_free,
        disk_required,
        disk_ready,
        results,
    )
    destination = output_path / "doctor.json"
    destination.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report


def _check_system(target, definition, spec, env: Mapping[str, str]) -> SystemDoctorResult:
    required_env_present = all(env.get(name) for name in definition.required_env)
    generation_env_present = all(env.get(name) for name in definition.generation_required_env)
    generation_connectivity = _generation_connectivity(definition, env)
    provenance = verified_execution_provenance(env, target)
    validation_provenance_present = provenance is not None
    driver_available = importlib.util.find_spec(_DRIVERS[target]) is not None
    connectivity = "not_attempted"
    connectivity_reason = "required environment missing" if not required_env_present else "driver unavailable"
    if required_env_present and driver_available:
        config = migrate_experiment_spec_to_run_config(spec, target)
        adapter = create_adapter(config.systems[0], config, env)
        try:
            dependency_ok, dependency_reason = adapter.check_dependency()
            environment_ok, missing_environment = adapter.check_environment()
            connection_ok, connection_reason = (
                adapter.connectivity_check()
                if dependency_ok and environment_ok
                else (
                    False,
                    dependency_reason
                    if not dependency_ok
                    else "missing " + ", ".join(missing_environment),
                )
            )
            connectivity = "ready" if connection_ok else "failed"
            connectivity_reason = _redact_reason(connection_reason, env)
        finally:
            adapter.close()
    return SystemDoctorResult(
        target.value,
        required_env_present,
        generation_env_present,
        generation_connectivity,
        validation_provenance_present,
        provenance.mode if provenance is not None else None,
        provenance.digest if provenance is not None else None,
        driver_available,
        connectivity,
        connectivity_reason,
        definition.graph_isolation,
    )


def _generation_connectivity(definition, env: Mapping[str, str]) -> str:
    if not definition.generation_required_env:
        return "not_required"
    if not all(env.get(name) for name in definition.generation_required_env):
        return "not_attempted"
    for name in definition.generation_required_env:
        value = env.get(name, "")
        if not name.endswith("_URI"):
            continue
        parsed = urlparse(value)
        if not parsed.hostname:
            return "failed"
        default_port = 5432 if parsed.scheme in {"postgres", "postgresql"} else 7687
        try:
            with socket.create_connection((parsed.hostname, parsed.port or default_port), timeout=3):
                pass
        except OSError:
            return "failed"
    return "ready"


def _redact_reason(reason: object, env: Mapping[str, str]) -> str:
    rendered = str(reason)
    for name, value in env.items():
        if not value:
            continue
        if "PASSWORD" in name or "TOKEN" in name:
            rendered = rendered.replace(value, "<redacted>")
            continue
        if name.endswith("_URI"):
            parsed = urlparse(value)
            if parsed.password:
                rendered = rendered.replace(parsed.password, "<redacted>")
    return rendered[:1024]


__all__ = ["DoctorReport", "SystemDoctorResult", "run_doctor", "run_doctor_spec"]
