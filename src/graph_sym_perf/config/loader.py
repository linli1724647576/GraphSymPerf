"""Explicit schema-v2 migration into the legacy adapter configuration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

import yaml

from graph_sym_perf.core import (
    ConfigError,
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)
if TYPE_CHECKING:
    from graph_sym_perf.experiment.plan_models import ExperimentSpec


_OPTIONAL_ENV: Mapping[TargetSystem, Mapping[str, str]] = MappingProxyType(
    {
        TargetSystem.NEO4J: MappingProxyType({"database": "GRAPHSYMPERF_NEO4J_DATABASE"}),
        TargetSystem.MEMGRAPH: MappingProxyType({
            "username": "GRAPHSYMPERF_MEMGRAPH_USER",
            "password": "GRAPHSYMPERF_MEMGRAPH_PASSWORD",
        }),
        TargetSystem.FALKORDB: MappingProxyType({
            "username": "GRAPHSYMPERF_FALKORDB_USER",
            "password": "GRAPHSYMPERF_FALKORDB_PASSWORD",
        }),
        TargetSystem.AGENSGRAPH: MappingProxyType({"database": "GRAPHSYMPERF_AGENSGRAPH_DATABASE"}),
        TargetSystem.APACHE_AGE: MappingProxyType({"database": "GRAPHSYMPERF_APACHE_AGE_DATABASE"}),
    }
)
_EMPTY_MAPPING: Mapping[str, str] = MappingProxyType({})


def migrate_experiment_spec_to_run_config(spec: ExperimentSpec, target: TargetSystem) -> RunConfig:
    """Map one resolved target without applying environment or runtime overlays."""

    definition = spec.systems.get(target)
    if definition is None:
        raise ConfigError(f"target is not part of experiment spec: {target.value}")
    timeout = float(definition.default_timeout_seconds)
    system = SystemConfig(
        target=target,
        required_env=definition.required_env,
        optional_env=_OPTIONAL_ENV.get(target, _EMPTY_MAPPING),
        settings=_EMPTY_MAPPING,
    )
    return RunConfig(
        source_path=str(spec.source_path),
        paths=RunPaths(),
        systems=(system,),
        dataset=DatasetConfig(seed=spec.seed),
        query_generation=QueryGenerationConfig(
            max_cases=spec.workload.max_generated_seeds_per_shard,
            source=spec.generation.backend,
            gdsmith=GdsmithConfig(
                num_queries=spec.workload.max_generated_seeds_per_shard,
                random_seed=spec.seed,
                timeout_seconds=float(spec.workload.max_shard_runtime_hours * 3600),
            ),
        ),
        equivalence=EquivalenceConfig(
            max_result_rows=int(spec.validation["max_result_rows"]),
            timeout_seconds=timeout,
        ),
        confirmation=ConfirmationConfig(
            warmup=int(spec.runtime["screen"]["warmups"]),
            repeats=int(spec.runtime["screen"]["pairs"]),
            timeout_seconds=timeout,
            min_latency_ratio=float(spec.runtime["screen"]["ratio"]),
        ),
        transforms=TransformConfig(max_variants_per_case=spec.workload.max_variants_per_seed),
    )


def migrate_legacy_config_to_v2(
    input_path: str | Path,
    output_path: str | Path,
    *,
    template_path: str | Path = "config/experiment.yaml",
) -> Path:
    """Offline, conservative conversion of explicit legacy shard semantics."""

    source_path = Path(input_path)
    destination = Path(output_path)
    template = Path(template_path)
    legacy = _yaml_mapping(source_path, "legacy configuration")
    if legacy.get("schema_version") not in (None, 1):
        raise ConfigError("migrate-config accepts only legacy schema_version 1 or unversioned input")

    source = _mapping(legacy.get("source"), "source")
    enabled = source.get("enabled")
    if enabled != ["gdsmith"]:
        raise ConfigError("legacy source.enabled must contain only gdsmith")
    systems = _mapping(legacy.get("systems"), "systems")
    targets_raw = systems.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("legacy systems.targets must be a non-empty list")
    try:
        targets = [TargetSystem.parse(str(item)).value for item in targets_raw]
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if len(set(targets)) != len(targets):
        raise ConfigError("legacy systems.targets must not contain duplicates")

    execution = _mapping(legacy.get("execution"), "execution")
    shards = _legacy_positive_int(execution.get("shards"), "execution.shards")
    accepted_pairs = _legacy_positive_int(
        execution.get("accepted_query_pairs_per_shard"),
        "execution.accepted_query_pairs_per_shard",
    )
    gdsmith = _mapping(source.get("gdsmith"), "source.gdsmith")
    max_generated = _legacy_positive_int(
        gdsmith.get("max_seed_queries_per_shard"),
        "source.gdsmith.max_seed_queries_per_shard",
    )
    if max_generated < accepted_pairs:
        raise ConfigError("legacy max generated seeds must be at least accepted query pairs")

    migrated = dict(_yaml_mapping(template, "schema-v2 template"))
    migrated["experiment"] = {
        **dict(_mapping(migrated.get("experiment"), "template.experiment")),
        "name": _legacy_nonempty_string(legacy.get("experiment_id"), "experiment_id"),
        "seed": _legacy_nonnegative_int(legacy.get("seed"), "seed"),
    }
    migrated["targets"] = targets
    migrated["matrix"] = {"shards_per_system": shards}
    migrated["workload"] = {
        **dict(_mapping(migrated.get("workload"), "template.workload")),
        "accepted_query_pairs_per_shard": accepted_pairs,
        "max_generated_seeds_per_shard": max_generated,
    }
    generation = dict(_mapping(migrated.get("generation"), "template.generation"))
    checkout = Path(str(generation["checkout"]))
    if not checkout.is_absolute():
        generation["checkout"] = str((template.parent / checkout).resolve())
    migrated["generation"] = generation

    systems_bytes = (template.parent / "systems.yaml").read_bytes()
    rules_bytes = (template.parent / "rules.yaml").read_bytes()
    serialized = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True)
    with tempfile.TemporaryDirectory(prefix="graphsymperf-migrate-") as temporary:
        bundle = Path(temporary)
        (bundle / "experiment.yaml").write_text(serialized, encoding="utf-8")
        (bundle / "systems.yaml").write_bytes(systems_bytes)
        (bundle / "rules.yaml").write_bytes(rules_bytes)
        from graph_sym_perf.experiment.planner import build_run_plan
        from graph_sym_perf.experiment.spec import load_experiment_spec

        build_run_plan(load_experiment_spec(bundle / "experiment.yaml"), repository_root=template.parents[1])

    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    bundle_destinations = (
        (destination, serialized.encode("utf-8")),
        (destination.parent / "systems.yaml", systems_bytes),
        (destination.parent / "rules.yaml", rules_bytes),
    )
    existing = [str(path) for path, _ in bundle_destinations if os.path.lexists(path)]
    if existing:
        raise FileExistsError(f"migration output already exists: {', '.join(existing)}")
    created: list[Path] = []
    try:
        for path, content in bundle_destinations:
            stream = path.open("xb")
            created.append(path)
            with stream:
                stream.write(content)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        if not parent_existed:
            try:
                destination.parent.rmdir()
            except OSError:
                pass
        raise
    return destination


def _yaml_mapping(path: Path, label: str) -> Mapping[str, Any]:
    if not path.exists():
        raise ConfigError(f"{label} does not exist: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid {label} YAML: {exc}") from exc
    return _mapping(value, label)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _legacy_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"legacy {label} must be a non-empty string")
    return value


def _legacy_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"legacy {label} must be a non-negative integer")
    return value


def _legacy_positive_int(value: Any, label: str) -> int:
    parsed = _legacy_nonnegative_int(value, label)
    if parsed == 0:
        raise ConfigError(f"legacy {label} must be positive")
    return parsed
