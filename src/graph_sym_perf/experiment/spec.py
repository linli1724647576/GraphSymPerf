"""Strict loader for the one formal runtime-first experiment schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.plan_models import (
    ExperimentSpec,
    GenerationSpec,
    MatrixSpec,
    SystemDefinition,
    WorkloadSpec,
)


def load_experiment_spec(path: str | Path) -> ExperimentSpec:
    source_path = Path(path)
    raw = _load_yaml(source_path, "experiment configuration")
    _only_keys(
        raw,
        {
            "schema_version", "experiment", "targets", "matrix", "workload", "generation", "scheduling",
            "runtime", "validation", "reducer", "rules", "artifacts",
        },
        "root",
    )
    if raw.get("schema_version") != 2:
        raise ConfigError("formal experiment configuration must use schema_version: 2")

    experiment = _mapping(raw.get("experiment"), "experiment")
    _only_keys(experiment, {"name", "seed", "feedback_observation_days"}, "experiment")
    experiment_name = _nonempty_string(experiment.get("name"), "experiment.name")
    seed = _positive_int(experiment.get("seed"), "experiment.seed", allow_zero=True)
    feedback_observation_days = _positive_int(experiment.get("feedback_observation_days"), "experiment.feedback_observation_days")

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("targets must be a non-empty list")
    targets = tuple(TargetSystem.parse(str(item)) for item in targets_raw)
    if len(set(targets)) != len(targets):
        raise ConfigError("targets must not contain duplicates")

    matrix_raw = _mapping(raw.get("matrix"), "matrix")
    _only_keys(matrix_raw, {"shards_per_system"}, "matrix")
    matrix = MatrixSpec(shards_per_system=_positive_int(matrix_raw.get("shards_per_system"), "matrix.shards_per_system"))

    workload_raw = _mapping(raw.get("workload"), "workload")
    _only_keys(
        workload_raw,
        {
            "accepted_query_pairs_per_shard", "max_generated_seeds_per_shard", "max_shard_runtime_hours",
            "deduplicate_by_ast_hash", "max_variants_per_seed",
        },
        "workload",
    )
    workload = WorkloadSpec(
        accepted_query_pairs_per_shard=_positive_int(workload_raw.get("accepted_query_pairs_per_shard"), "workload.accepted_query_pairs_per_shard"),
        max_generated_seeds_per_shard=_positive_int(workload_raw.get("max_generated_seeds_per_shard"), "workload.max_generated_seeds_per_shard"),
        max_shard_runtime_hours=_positive_int(workload_raw.get("max_shard_runtime_hours"), "workload.max_shard_runtime_hours"),
        deduplicate_by_ast_hash=_bool(workload_raw.get("deduplicate_by_ast_hash"), "workload.deduplicate_by_ast_hash"),
        max_variants_per_seed=_positive_int(workload_raw.get("max_variants_per_seed"), "workload.max_variants_per_seed"),
    )
    if workload.max_generated_seeds_per_shard < workload.accepted_query_pairs_per_shard:
        raise ConfigError("workload.max_generated_seeds_per_shard must be at least accepted_query_pairs_per_shard")

    generation_raw = _mapping(raw.get("generation"), "generation")
    _only_keys(generation_raw, {"backend", "commit_source", "checkout", "source"}, "generation")
    backend = _nonempty_string(generation_raw.get("backend"), "generation.backend")
    if backend != "gdsmith" or generation_raw.get("source", "gdsmith") != "gdsmith":
        raise ConfigError("generation.backend/source must be gdsmith; LDBC and alternate generators are unsupported")
    commit_source = _nonempty_string(generation_raw.get("commit_source"), "generation.commit_source")
    if commit_source != "vendored_checkout":
        raise ConfigError("generation.commit_source must be vendored_checkout")
    checkout = Path(_nonempty_string(generation_raw.get("checkout"), "generation.checkout"))
    if not checkout.is_absolute():
        checkout = (source_path.parent / checkout).resolve()
    generation = GenerationSpec(backend=backend, commit_source=commit_source, checkout=checkout)

    policies = {
        name: _strict_policy(raw.get(name), name)
        for name in ("scheduling", "runtime", "validation", "reducer", "rules", "artifacts")
    }
    systems = _load_systems(source_path.parent / "systems.yaml", targets)
    return ExperimentSpec(
        schema_version=2,
        name=experiment_name,
        seed=seed,
        feedback_observation_days=feedback_observation_days,
        targets=targets,
        matrix=matrix,
        workload=workload,
        generation=generation,
        scheduling=policies["scheduling"],
        runtime=policies["runtime"],
        validation=policies["validation"],
        reducer=policies["reducer"],
        rules=policies["rules"],
        artifacts=policies["artifacts"],
        systems=systems,
        source_path=source_path.resolve(),
    )


def _load_systems(path: Path, targets: tuple[TargetSystem, ...]) -> Mapping[TargetSystem, SystemDefinition]:
    raw = _load_yaml(path, "systems configuration")
    _only_keys(raw, {"schema_version", "systems"}, "systems root")
    if raw.get("schema_version") != 1:
        raise ConfigError("systems.yaml must use schema_version: 1")
    systems_raw = _mapping(raw.get("systems"), "systems")
    definitions: dict[TargetSystem, SystemDefinition] = {}
    for target in targets:
        item = _mapping(systems_raw.get(target.value), f"systems.{target.value}")
        _only_keys(item, {"required_env", "generation_required_env", "dialect", "graph_isolation", "default_timeout_seconds"}, f"systems.{target.value}")
        env = _environment_names(item.get("required_env"), f"systems.{target.value}.required_env")
        generation_env = _environment_names(item.get("generation_required_env", ()), f"systems.{target.value}.generation_required_env")
        definitions[target] = SystemDefinition(
            target=target,
            required_env=tuple(env),
            generation_required_env=tuple(generation_env),
            dialect=_nonempty_string(item.get("dialect"), f"systems.{target.value}.dialect"),
            graph_isolation=_nonempty_string(item.get("graph_isolation"), f"systems.{target.value}.graph_isolation"),
            default_timeout_seconds=_positive_int(item.get("default_timeout_seconds"), f"systems.{target.value}.default_timeout_seconds"),
        )
    return definitions


def _environment_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(name, str) and name for name in value):
        raise ConfigError(f"{label} must be a list of non-empty environment variable names")
    return tuple(value)


def _strict_policy(raw: Any, name: str) -> Mapping[str, object]:
    value = _mapping(raw, name)
    if not value:
        raise ConfigError(f"{name} must not be empty")
    return dict(value)


def _load_yaml(path: Path, label: str) -> Mapping[str, Any]:
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


def _only_keys(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(f"unknown field(s) in {label}: {', '.join(sorted(str(item) for item in unknown))}")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (value < 0 if allow_zero else value <= 0):
        bound = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"{label} must be a {bound} integer")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be boolean")
    return value
