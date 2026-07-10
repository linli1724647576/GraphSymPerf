"""Preflight for the runtime-first formal matrix."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping

from graph_sym_perf.core import (
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
)
from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.experiment.planner import build_run_plan
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.gdsmith.runner import check_gdsmith_tooling


@dataclass(frozen=True)
class SystemDoctorResult:
    system: str
    required_env_present: bool
    driver_available: bool
    connectivity: str
    graph_isolation: str


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    gdsmith_ready: bool
    gdsmith_dirty: bool | None
    disk_free_bytes: int
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
    ready = gdsmith.ready and plan.source_revision.gdsmith_dirty is False and all(
        result.required_env_present and result.driver_available and result.connectivity == "ready" and result.graph_isolation for result in results
    )
    report = DoctorReport(ready, gdsmith.ready, plan.source_revision.gdsmith_dirty, disk_free, results)
    destination = Path(output_root) / "doctor.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report


def _check_system(target, definition, spec, env: Mapping[str, str]) -> SystemDoctorResult:
    required_env_present = all(env.get(name) for name in definition.required_env)
    driver_available = importlib.util.find_spec(_DRIVERS[target]) is not None
    connectivity = "not_attempted"
    if required_env_present and driver_available:
        config = RunConfig(
            source_path=str(spec.source_path),
            paths=RunPaths(),
            systems=(SystemConfig(target, definition.required_env, _optional_env(target), {}),),
            dataset=DatasetConfig(),
            query_generation=QueryGenerationConfig(gdsmith=GdsmithConfig()),
            equivalence=EquivalenceConfig(),
            confirmation=ConfirmationConfig(),
        )
        adapter = create_adapter(config.systems[0], config, env)
        try:
            dependency_ok, _ = adapter.check_dependency()
            environment_ok, _ = adapter.check_environment()
            connection_ok, _ = adapter.connectivity_check() if dependency_ok and environment_ok else (False, "preflight failed")
            connectivity = "ready" if connection_ok else "failed"
        finally:
            adapter.close()
    return SystemDoctorResult(target.value, required_env_present, driver_available, connectivity, definition.graph_isolation)


def _optional_env(target: TargetSystem) -> Mapping[str, str]:
    return {
        TargetSystem.NEO4J: {"database": "GRAPHSYMPERF_NEO4J_DATABASE"},
        TargetSystem.MEMGRAPH: {"username": "GRAPHSYMPERF_MEMGRAPH_USER", "password": "GRAPHSYMPERF_MEMGRAPH_PASSWORD"},
        TargetSystem.FALKORDB: {"username": "GRAPHSYMPERF_FALKORDB_USER", "password": "GRAPHSYMPERF_FALKORDB_PASSWORD"},
        TargetSystem.AGENSGRAPH: {"database": "GRAPHSYMPERF_AGENSGRAPH_DATABASE"},
        TargetSystem.APACHE_AGE: {"database": "GRAPHSYMPERF_APACHE_AGE_DATABASE"},
    }.get(target, {})
