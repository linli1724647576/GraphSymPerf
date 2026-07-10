"""Strict loader for the one formal runtime-first experiment schema."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

import yaml

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.plan_models import (
    ExperimentSpec,
    FrozenMapping,
    GenerationSpec,
    MatrixSpec,
    SystemDefinition,
    WorkloadSpec,
    freeze_value,
)


_POLICY_SCHEMAS: Mapping[str, Mapping[str, object | None]] = {
    "scheduling": {
        "max_parallel_generation_systems": None,
        "max_parallel_measurement_systems": None,
        "max_parallel_shards_per_system": None,
        "shard_retry_limit": None,
        "heartbeat_seconds": None,
        "stale_after_seconds": None,
    },
    "runtime": {
        "cache_mode": None,
        "screen": {"warmups": None, "pairs": None, "ratio": None, "schedule": None},
        "noise": {"probe_queries": None, "aa_pairs_per_query": None, "absolute_delta_quantile": None},
    },
    "validation": {
        "sessions": None,
        "warmups_per_session": None,
        "pairs_per_session": None,
        "schedule": None,
        "reconnect_between_sessions": None,
        "reload_graph_between_sessions": None,
        "require_complete_equivalence": None,
        "min_ratio": None,
        "min_ci_lower_ratio": None,
        "min_absolute_delta": None,
        "bootstrap_resamples": None,
        "require_same_direction_in_both_sessions": None,
        "max_result_rows": None,
    },
    "reducer": {
        "enabled": None,
        "max_attempts": None,
        "max_seconds": None,
        "require_reduced_validation": None,
        "auto_submit_status": None,
        "partial_requires_manual_review": None,
    },
    "rules": {
        "safety_tier": None,
        "yield_update_epoch_accepted_pairs": None,
        "forbidden_feedback": None,
    },
    "artifacts": {
        "default_level": None,
        "promote_candidate_level": None,
        "promote_issue_level": None,
        "shard_budget_mb": None,
    },
}


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
    _exact_keys(generation_raw, {"backend", "commit_source", "checkout", "source", "required_env"}, "generation")
    backend = _nonempty_string(generation_raw.get("backend"), "generation.backend")
    if backend != "gdsmith" or generation_raw.get("source", "gdsmith") != "gdsmith":
        raise ConfigError("generation.backend/source must be gdsmith; LDBC and alternate generators are unsupported")
    commit_source = _nonempty_string(generation_raw.get("commit_source"), "generation.commit_source")
    if commit_source != "vendored_checkout":
        raise ConfigError("generation.commit_source must be vendored_checkout")
    checkout = Path(_nonempty_string(generation_raw.get("checkout"), "generation.checkout"))
    if not checkout.is_absolute():
        checkout = (source_path.parent / checkout).resolve()
    generation = GenerationSpec(
        backend=backend,
        commit_source=commit_source,
        checkout=checkout,
        required_env=_environment_names(generation_raw.get("required_env"), "generation.required_env"),
    )

    policies = {
        name: _strict_policy(raw.get(name), name)
        for name in ("scheduling", "runtime", "validation", "reducer", "rules", "artifacts")
    }
    systems = _load_systems(source_path.parent / "systems.yaml", targets)
    rules_path = source_path.parent / "rules.yaml"
    rules_catalog, rules_schema_sha256 = _load_rules(rules_path)
    safety_tier = str(policies["rules"]["safety_tier"])
    if safety_tier not in _mapping(rules_catalog["tiers"], "rules.tiers"):
        raise ConfigError(f"rules.safety_tier is not defined in rules.yaml: {safety_tier}")
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
        rules_catalog=rules_catalog,
        rules_schema_sha256=rules_schema_sha256,
        rules_source_path=rules_path.resolve(),
        source_path=source_path.resolve(),
    )


def _load_systems(path: Path, targets: tuple[TargetSystem, ...]) -> Mapping[TargetSystem, SystemDefinition]:
    raw = _load_yaml(path, "systems configuration")
    _only_keys(raw, {"schema_version", "systems"}, "systems root")
    if raw.get("schema_version") != 1:
        raise ConfigError("systems.yaml must use schema_version: 1")
    systems_raw = _mapping(raw.get("systems"), "systems")
    available: dict[TargetSystem, SystemDefinition] = {}
    for name, raw_item in systems_raw.items():
        try:
            target = TargetSystem.parse(str(name))
        except ValueError as exc:
            raise ConfigError(f"unknown system definition: {name}") from exc
        item = _mapping(raw_item, f"systems.{target.value}")
        _exact_keys(item, {"required_env", "generation_required_env", "dialect", "graph_isolation", "default_timeout_seconds"}, f"systems.{target.value}")
        env = _environment_names(item.get("required_env"), f"systems.{target.value}.required_env")
        generation_env = _environment_names(item.get("generation_required_env"), f"systems.{target.value}.generation_required_env")
        available[target] = SystemDefinition(
            target=target,
            required_env=tuple(env),
            generation_required_env=tuple(generation_env),
            dialect=_nonempty_string(item.get("dialect"), f"systems.{target.value}.dialect"),
            graph_isolation=_nonempty_string(item.get("graph_isolation"), f"systems.{target.value}.graph_isolation"),
            default_timeout_seconds=_positive_int(item.get("default_timeout_seconds"), f"systems.{target.value}.default_timeout_seconds"),
        )
    missing = [target.value for target in targets if target not in available]
    if missing:
        raise ConfigError(f"missing system definition(s): {', '.join(missing)}")
    return FrozenMapping({target: available[target] for target in targets})


def _load_rules(path: Path) -> tuple[Mapping[str, object], str]:
    raw_bytes = path.read_bytes() if path.exists() else b""
    raw = _load_yaml(path, "rules configuration")
    _exact_keys(raw, {"schema_version", "tiers"}, "rules root")
    if raw.get("schema_version") != 1:
        raise ConfigError("rules.yaml must use schema_version: 1")
    tiers = _mapping(raw.get("tiers"), "rules.tiers")
    _exact_keys(tiers, {"core", "experimental", "control"}, "rules.tiers")
    core = _mapping(tiers["core"], "rules.tiers.core")
    _exact_keys(core, {"minimum_accepted_pairs_per_rule", "scheduling"}, "rules.tiers.core")
    _positive_int(
        core["minimum_accepted_pairs_per_rule"],
        "rules.tiers.core.minimum_accepted_pairs_per_rule",
    )
    _enum(
        core["scheduling"],
        {"exploration_floor_then_validated_yield"},
        "rules.tiers.core.scheduling",
    )
    for name in ("experimental", "control"):
        item = _mapping(tiers[name], f"rules.tiers.{name}")
        _exact_keys(item, {"auto_submit"}, f"rules.tiers.{name}")
        _bool(item["auto_submit"], f"rules.tiers.{name}.auto_submit")
    return freeze_value(raw), hashlib.sha256(raw_bytes).hexdigest()


def _environment_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(name, str) and name for name in value):
        raise ConfigError(f"{label} must be a list of non-empty environment variable names")
    return tuple(value)


def _strict_policy(raw: Any, name: str) -> Mapping[str, object]:
    value = _mapping(raw, name)
    if not value:
        raise ConfigError(f"{name} must not be empty")
    _validate_policy(value, _POLICY_SCHEMAS[name], name)
    _validate_policy_values(name, value)
    return freeze_value(value)


def _validate_policy(value: Mapping[str, Any], schema: Mapping[str, object | None], label: str) -> None:
    _exact_keys(value, set(schema), label)
    for key, nested_schema in schema.items():
        if nested_schema is not None:
            nested = _mapping(value.get(key), f"{label}.{key}")
            _validate_policy(nested, nested_schema, f"{label}.{key}")


def _validate_policy_values(name: str, value: Mapping[str, Any]) -> None:
    if name == "scheduling":
        for key in (
            "max_parallel_generation_systems", "max_parallel_measurement_systems",
            "max_parallel_shards_per_system", "heartbeat_seconds", "stale_after_seconds",
        ):
            _positive_int(value[key], f"scheduling.{key}")
        _positive_int(value["shard_retry_limit"], "scheduling.shard_retry_limit", allow_zero=True)
        if value["stale_after_seconds"] <= value["heartbeat_seconds"]:
            raise ConfigError("scheduling.stale_after_seconds must exceed heartbeat_seconds")
        return
    if name == "runtime":
        _enum(value["cache_mode"], {"warm", "cold"}, "runtime.cache_mode")
        screen = _mapping(value["screen"], "runtime.screen")
        _positive_int(screen["warmups"], "runtime.screen.warmups", allow_zero=True)
        _positive_int(screen["pairs"], "runtime.screen.pairs")
        screen_ratio = _positive_number(screen["ratio"], "runtime.screen.ratio")
        if screen_ratio <= 1:
            raise ConfigError("runtime.screen.ratio must exceed 1")
        _enum(screen["schedule"], {"randomized_ab_ba"}, "runtime.screen.schedule")
        noise = _mapping(value["noise"], "runtime.noise")
        _positive_int(noise["probe_queries"], "runtime.noise.probe_queries")
        _positive_int(noise["aa_pairs_per_query"], "runtime.noise.aa_pairs_per_query")
        quantile = _positive_number(noise["absolute_delta_quantile"], "runtime.noise.absolute_delta_quantile")
        if quantile > 1:
            raise ConfigError("runtime.noise.absolute_delta_quantile must not exceed 1")
        return
    if name == "validation":
        for key in ("sessions", "pairs_per_session", "bootstrap_resamples", "max_result_rows"):
            _positive_int(value[key], f"validation.{key}")
        _positive_int(value["warmups_per_session"], "validation.warmups_per_session", allow_zero=True)
        _enum(value["schedule"], {"balanced_random"}, "validation.schedule")
        for key in (
            "reconnect_between_sessions", "reload_graph_between_sessions", "require_complete_equivalence",
            "require_same_direction_in_both_sessions",
        ):
            _bool(value[key], f"validation.{key}")
        minimum_ratio = _positive_number(value["min_ratio"], "validation.min_ratio")
        minimum_ci_ratio = _positive_number(value["min_ci_lower_ratio"], "validation.min_ci_lower_ratio")
        if minimum_ratio <= 1 or minimum_ci_ratio <= 1:
            raise ConfigError("validation ratio thresholds must exceed 1")
        if minimum_ratio < minimum_ci_ratio:
            raise ConfigError("validation.min_ratio must be at least min_ci_lower_ratio")
        minimum_delta = value["min_absolute_delta"]
        if minimum_delta != "aa_noise_p99":
            _positive_number(minimum_delta, "validation.min_absolute_delta")
        return
    if name == "reducer":
        for key in ("enabled", "require_reduced_validation", "partial_requires_manual_review"):
            _bool(value[key], f"reducer.{key}")
        _positive_int(value["max_attempts"], "reducer.max_attempts")
        _positive_int(value["max_seconds"], "reducer.max_seconds")
        _enum(value["auto_submit_status"], {"reduced_valid"}, "reducer.auto_submit_status")
        return
    if name == "rules":
        _nonempty_string(value["safety_tier"], "rules.safety_tier")
        _positive_int(value["yield_update_epoch_accepted_pairs"], "rules.yield_update_epoch_accepted_pairs")
        feedback = value["forbidden_feedback"]
        if not isinstance(feedback, list | tuple) or not feedback or not all(
            isinstance(item, str) and item for item in feedback
        ):
            raise ConfigError("rules.forbidden_feedback must be a non-empty list of names")
        return
    for key in ("default_level", "promote_candidate_level", "promote_issue_level"):
        _nonempty_string(value[key], f"artifacts.{key}")
    _positive_int(value["shard_budget_mb"], "artifacts.shard_budget_mb")


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


def _exact_keys(mapping: Mapping[str, Any], expected: set[str], label: str) -> None:
    _only_keys(mapping, expected, label)
    missing = expected - set(mapping)
    if missing:
        raise ConfigError(f"missing field(s) in {label}: {', '.join(sorted(missing))}")


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


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label} must be a positive number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ConfigError(f"{label} must be finite")
    if parsed <= 0:
        raise ConfigError(f"{label} must be a positive number")
    return parsed


def _enum(value: Any, allowed: set[str], label: str) -> str:
    parsed = _nonempty_string(value, label)
    if parsed not in allowed:
        raise ConfigError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return parsed
