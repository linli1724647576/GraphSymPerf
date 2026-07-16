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
from graph_sym_perf.experiment.plan_models import CampaignPhase, ValidatedYieldExperimentSpec
from graph_sym_perf.experiment.planner import build_rule_plan, build_run_plan
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
    phase: str | None = None
    entry_count: int = 0
    regression_environment_ready: bool = True


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
    phase: str | CampaignPhase | None = None,
) -> DoctorReport:
    spec = load_experiment_spec(config_path)
    return run_doctor_spec(
        spec,
        environment=environment,
        output_root=output_root,
        phase=phase,
    )


def run_doctor_spec(
    spec,
    *,
    environment: Mapping[str, str] | None = None,
    output_root: str | Path = ".",
    phase: str | CampaignPhase | None = None,
) -> DoctorReport:
    env = dict(os.environ if environment is None else environment)
    output_path = Path(output_root)
    output_path.mkdir(parents=True, exist_ok=True)
    if isinstance(spec, ValidatedYieldExperimentSpec):
        parsed_phase = CampaignPhase.P0 if phase is None else (
            phase if isinstance(phase, CampaignPhase) else CampaignPhase(str(phase))
        )
        if parsed_phase is CampaignPhase.P2:
            raise ValueError("schema-v3 P2 doctor requires a frozen selection through the run command")
        plan = build_rule_plan(spec, parsed_phase, environment=env)
        entries = plan.entries
        budget_mb = int(spec.artifacts["entry_budget_mb"])
        phase_value = parsed_phase.value
    else:
        if phase is not None:
            raise ValueError("--phase is only valid for schema-v3 doctor")
        plan = build_run_plan(spec, environment=env)
        entries = plan.tasks
        budget_mb = int(spec.artifacts["shard_budget_mb"])
        phase_value = None
    gdsmith = check_gdsmith_tooling(
        GdsmithConfig(
            expected_source_commit=spec.generation.expected_commit,
            compatibility_profile="neo4j" if spec.schema_version == 3 else "all",
        ),
        env,
    )
    results = tuple(_check_system(target, definition, spec, env) for target, definition in spec.systems.items())
    disk_free = shutil.disk_usage(output_path).free
    disk_required = len(entries) * budget_mb * 1024 * 1024
    disk_ready = disk_free >= disk_required
    source_ready = (
        plan.source_revision.provenance_complete
        and plan.source_revision.repository_dirty is False
        and plan.source_revision.gdsmith_dirty is False
        and plan.source_revision.gdsmith_commit == spec.generation.expected_commit
    )
    regression_ready = _regression_environment_ready(spec, env)
    ready = gdsmith.ready and source_ready and disk_ready and regression_ready and all(
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
        phase_value,
        len(entries),
        regression_ready,
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
        config = (
            _validated_yield_doctor_config(spec, target, definition)
            if isinstance(spec, ValidatedYieldExperimentSpec)
            else migrate_experiment_spec_to_run_config(spec, target)
        )
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


def _validated_yield_doctor_config(spec, target, definition) -> RunConfig:
    optional_env = {"database": "GRAPHSYMPERF_NEO4J_DATABASE"} if target is TargetSystem.NEO4J else {}
    system = SystemConfig(
        target=target,
        required_env=definition.required_env,
        optional_env=optional_env,
        settings={},
    )
    return RunConfig(
        source_path=str(spec.source_path),
        paths=RunPaths(),
        systems=(system,),
        dataset=DatasetConfig(seed=spec.seed),
        query_generation=QueryGenerationConfig(),
        equivalence=EquivalenceConfig(
            max_result_rows=int(spec.validation["max_result_rows"]),
            timeout_seconds=float(definition.default_timeout_seconds),
        ),
        confirmation=ConfirmationConfig(timeout_seconds=float(definition.default_timeout_seconds)),
    )


def _regression_environment_ready(spec, env: Mapping[str, str]) -> bool:
    if not isinstance(spec, ValidatedYieldExperimentSpec):
        return True
    manifest_path = Path(spec.regression["manifest"])
    try:
        import yaml

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(manifest, Mapping):
        return False
    names = tuple(
        manifest.get(key)
        for key in (
            "image_digest_env",
            "container_env",
            "uri_env",
            "user_env",
            "password_env",
        )
    )
    return all(isinstance(name, str) and bool(env.get(name)) for name in names)


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
