"""Preflight for the runtime-first formal matrix."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import GdsmithConfig, TargetSystem
from graph_sym_perf.experiment.planner import build_run_plan
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.gdsmith.runner import check_gdsmith_tooling


@dataclass(frozen=True)
class SystemDoctorResult:
    system: str
    required_env_present: bool
    generation_env_present: bool
    driver_available: bool
    connectivity: str
    graph_isolation: str


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    gdsmith_ready: bool
    gdsmith_dirty: bool | None
    gdsmith_commit_matches: bool
    disk_free_bytes: int
    disk_required_bytes: int
    disk_ready: bool
    systems: tuple[SystemDoctorResult, ...]


_DRIVERS = {
    TargetSystem.NEO4J: "neo4j",
    TargetSystem.MEMGRAPH: "neo4j",
    TargetSystem.FALKORDB: "FalkorDB",
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
    plan = build_run_plan(spec)
    env = dict(os.environ if environment is None else environment)
    gdsmith = check_gdsmith_tooling(GdsmithConfig(), env)
    results = tuple(_check_system(target, definition, spec, env) for target, definition in spec.systems.items())
    disk_free = shutil.disk_usage(Path(output_root)).free
    disk_required = len(plan.tasks) * int(spec.artifacts["shard_budget_mb"]) * 1024 * 1024
    disk_ready = disk_free >= disk_required
    source_ready = (
        plan.source_revision.provenance_complete
        and plan.source_revision.repository_dirty is False
        and plan.source_revision.gdsmith_dirty is False
        and plan.source_revision.gdsmith_commit == spec.generation.expected_commit
    )
    ready = gdsmith.ready and source_ready and disk_ready and all(
        result.required_env_present and result.generation_env_present and result.driver_available and result.connectivity == "ready" and result.graph_isolation for result in results
    )
    report = DoctorReport(
        ready,
        gdsmith.ready,
        plan.source_revision.gdsmith_dirty,
        plan.source_revision.gdsmith_commit == spec.generation.expected_commit,
        disk_free,
        disk_required,
        disk_ready,
        results,
    )
    destination = Path(output_root) / "doctor.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report


def _check_system(target, definition, spec, env: Mapping[str, str]) -> SystemDoctorResult:
    required_env_present = all(env.get(name) for name in definition.required_env)
    generation_env_present = all(env.get(name) for name in definition.generation_required_env)
    driver_available = importlib.util.find_spec(_DRIVERS[target]) is not None
    connectivity = "not_attempted"
    if required_env_present and driver_available:
        config = migrate_experiment_spec_to_run_config(spec, target)
        adapter = create_adapter(config.systems[0], config, env)
        try:
            dependency_ok, _ = adapter.check_dependency()
            environment_ok, _ = adapter.check_environment()
            connection_ok, _ = adapter.connectivity_check() if dependency_ok and environment_ok else (False, "preflight failed")
            connectivity = "ready" if connection_ok else "failed"
        finally:
            adapter.close()
    return SystemDoctorResult(target.value, required_env_present, generation_env_present, driver_available, connectivity, definition.graph_isolation)
