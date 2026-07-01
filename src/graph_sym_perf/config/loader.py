"""Configuration loading for real database mining runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from graph_sym_perf.core import (
    ConfigError,
    ConfirmationConfig,
    DatasetConfig,
    EngineCapabilities,
    EquivalenceConfig,
    GdsmithConfig,
    OracleConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)


def load_run_config(path: str | Path) -> RunConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"configuration file does not exist: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML configuration: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError("configuration root must be a mapping")

    return RunConfig(
        source_path=str(config_path),
        paths=_load_paths(data.get("paths", {})),
        systems=_load_systems(data.get("systems", {})),
        dataset=_load_dataset(data.get("dataset", {})),
        query_generation=_load_query_generation(data.get("query_generation", {})),
        equivalence=_load_equivalence(data.get("equivalence", {})),
        confirmation=_load_confirmation(data.get("confirmation", {})),
        oracle=_load_oracle(data.get("oracle", {})),
        transforms=_load_transforms(data.get("transforms", {})),
    )


def _load_paths(raw: Any) -> RunPaths:
    mapping = _mapping(raw, "paths")
    return RunPaths(artifact_dir=str(mapping.get("artifact_dir", "logs/{run_id}")))


def _load_systems(raw: Any) -> tuple[SystemConfig, ...]:
    mapping = _mapping(raw, "systems")
    targets_raw = mapping.get("targets", ["neo4j", "memgraph", "kuzu"])
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("systems.targets must be a non-empty list")
    env_mapping = _mapping(mapping.get("env", {}), "systems.env")
    settings_mapping = _mapping(mapping.get("settings", {}), "systems.settings")

    systems: list[SystemConfig] = []
    for item in targets_raw:
        target = _target(item)
        target_env = _mapping(env_mapping.get(target.value, {}), f"systems.env.{target.value}")
        required = target_env.get("required", _default_required_env(target))
        optional = target_env.get("optional", _default_optional_env(target))
        if not isinstance(required, list):
            raise ConfigError(f"systems.env.{target.value}.required must be a list")
        if not isinstance(optional, Mapping):
            raise ConfigError(f"systems.env.{target.value}.optional must be a mapping")
        systems.append(
            SystemConfig(
                target=target,
                required_env=tuple(str(name) for name in required),
                optional_env={str(key): str(value) for key, value in optional.items()},
                settings=dict(_mapping(settings_mapping.get(target.value, {}), f"systems.settings.{target.value}")),
                capabilities=_default_capabilities(target),
            )
        )
    return tuple(systems)


def _load_dataset(raw: Any) -> DatasetConfig:
    mapping = _mapping(raw, "dataset")
    counts = _mapping(mapping.get("counts", {}), "dataset.counts")
    return DatasetConfig(
        kind=str(mapping.get("kind", "ldbc_snb_mini")),
        dataset_id=str(mapping.get("dataset_id", "graph_sym_perf_ldbc_mini")),
        seed=_int(mapping.get("seed", 20260513), "dataset.seed"),
        person_count=_positive_int(counts.get("persons", 8), "dataset.counts.persons"),
        post_count=_positive_int(counts.get("posts", 6), "dataset.counts.posts"),
        forum_count=_positive_int(counts.get("forums", 2), "dataset.counts.forums"),
        tag_count=_positive_int(counts.get("tags", 4), "dataset.counts.tags"),
        allow_load_env=str(mapping.get("allow_load_env", "GRAPHSYMPERF_ALLOW_LOAD")),
    )


def _load_query_generation(raw: Any) -> QueryGenerationConfig:
    mapping = _mapping(raw, "query_generation")
    source = str(mapping.get("source", "gdsmith"))
    if source != "gdsmith":
        raise ConfigError("query_generation.source must be gdsmith for GraphSymPerf v1")
    return QueryGenerationConfig(
        max_cases=_int(mapping.get("max_cases", 500), "query_generation.max_cases"),
        result_limit=_int(mapping.get("result_limit", 100), "query_generation.result_limit"),
        include_variable_length=bool(mapping.get("include_variable_length", False)),
        source=source,
        gdsmith=_load_gdsmith_config(mapping.get("gdsmith", {})),
    )


def _load_gdsmith_config(raw: Any) -> GdsmithConfig:
    mapping = _mapping(raw, "query_generation.gdsmith")
    return GdsmithConfig(
        jar_env=str(mapping.get("jar_env", "GRAPHSYMPERF_GDSMITH_JAR")),
        algorithm=str(mapping.get("algorithm", "compared3")),
        num_tries=_positive_int(mapping.get("num_tries", 1), "query_generation.gdsmith.num_tries"),
        num_queries=_positive_int(mapping.get("num_queries", 500), "query_generation.gdsmith.num_queries"),
        random_seed=_int(mapping.get("random_seed", 20260522), "query_generation.gdsmith.random_seed"),
        allow_destructive_env=str(
            mapping.get("allow_destructive_env", "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE")
        ),
        no_proxy_launcher_env=str(
            mapping.get("no_proxy_launcher_env", "GRAPHSYMPERF_GDSMITH_NO_PROXY_LAUNCHER")
        ),
        backend_extra_args=_load_backend_extra_args(
            mapping.get("backend_extra_args", {}),
            "query_generation.gdsmith.backend_extra_args",
        ),
        timeout_seconds=_float(mapping.get("timeout_seconds", 180.0), "query_generation.gdsmith.timeout_seconds"),
    )


def _load_equivalence(raw: Any) -> EquivalenceConfig:
    mapping = _mapping(raw, "equivalence")
    return EquivalenceConfig(
        max_result_rows=_int(mapping.get("max_result_rows", 1000), "equivalence.max_result_rows"),
        timeout_seconds=_float(mapping.get("timeout_seconds", 30.0), "equivalence.timeout_seconds"),
    )


def _load_confirmation(raw: Any) -> ConfirmationConfig:
    mapping = _mapping(raw, "confirmation")
    return ConfirmationConfig(
        warmup=_int(mapping.get("warmup", 1), "confirmation.warmup"),
        repeats=_int(mapping.get("repeats", 5), "confirmation.repeats"),
        timeout_seconds=_float(mapping.get("timeout_seconds", 30.0), "confirmation.timeout_seconds"),
        min_latency_ratio=_float(mapping.get("min_latency_ratio", 10.0), "confirmation.min_latency_ratio"),
        min_latency_delta_ms=_float(
            mapping.get("min_latency_delta_ms", 50.0), "confirmation.min_latency_delta_ms"
        ),
        min_db_hits_ratio=_float(mapping.get("min_db_hits_ratio", 5.0), "confirmation.min_db_hits_ratio"),
        min_rows_ratio=_float(mapping.get("min_rows_ratio", 5.0), "confirmation.min_rows_ratio"),
    )


def _load_oracle(raw: Any) -> OracleConfig:
    mapping = _mapping(raw, "oracle")
    return OracleConfig(
        enable_structural_diff=bool(mapping.get("enable_structural_diff", True)),
        enable_profile_metrics=bool(mapping.get("enable_profile_metrics", True)),
    )


def _load_transforms(raw: Any) -> TransformConfig:
    mapping = _mapping(raw, "transforms")
    enabled_families = mapping.get("enabled_families", TransformConfig().enabled_families)
    if not isinstance(enabled_families, list | tuple) or not enabled_families:
        raise ConfigError("transforms.enabled_families must be a non-empty list")
    enabled = mapping.get("enabled_rules", TransformConfig().enabled_rules)
    if not isinstance(enabled, list | tuple) or not enabled:
        raise ConfigError("transforms.enabled_rules must be a non-empty list")
    return TransformConfig(
        enabled_families=tuple(str(name) for name in enabled_families),
        enabled_rules=tuple(str(rule) for rule in enabled),
        max_expr_complexity=str(mapping.get("max_expr_complexity", "medium")),
        gdsmith_safe_only=bool(mapping.get("gdsmith_safe_only", True)),
        max_variants_per_case=(
            _int(mapping.get("max_variants_per_case"), "transforms.max_variants_per_case")
            if mapping.get("max_variants_per_case") is not None
            else None
        ),
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _load_backend_extra_args(raw: Any, label: str) -> Mapping[str, tuple[str, ...]]:
    mapping = _mapping(raw, label)
    parsed: dict[str, tuple[str, ...]] = {}
    for backend, values in mapping.items():
        if not isinstance(values, list | tuple):
            raise ConfigError(f"{label}.{backend} must be a list")
        parsed[str(backend)] = tuple(str(value) for value in values)
    return parsed


def _target(value: Any) -> TargetSystem:
    try:
        return TargetSystem.parse(str(value))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _int(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc


def _positive_int(value: Any, label: str) -> int:
    parsed = _int(value, label)
    if parsed <= 0:
        raise ConfigError(f"{label} must be greater than 0")
    return parsed


def _float(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number") from exc


def _default_required_env(target: TargetSystem) -> list[str]:
    if target is TargetSystem.NEO4J:
        return [
            "GRAPHSYMPERF_NEO4J_URI",
            "GRAPHSYMPERF_NEO4J_USER",
            "GRAPHSYMPERF_NEO4J_PASSWORD",
        ]
    if target is TargetSystem.MEMGRAPH:
        return ["GRAPHSYMPERF_MEMGRAPH_HOST", "GRAPHSYMPERF_MEMGRAPH_PORT"]
    if target is TargetSystem.FALKORDB:
        return [
            "GRAPHSYMPERF_FALKORDB_HOST",
            "GRAPHSYMPERF_FALKORDB_PORT",
            "GRAPHSYMPERF_FALKORDB_GRAPH",
        ]
    if target is TargetSystem.AGENSGRAPH:
        return [
            "GRAPHSYMPERF_AGENSGRAPH_URI",
            "GRAPHSYMPERF_AGENSGRAPH_GRAPH",
        ]
    if target is TargetSystem.APACHE_AGE:
        return [
            "GRAPHSYMPERF_APACHE_AGE_URI",
            "GRAPHSYMPERF_APACHE_AGE_GRAPH",
        ]
    if target is TargetSystem.ARCADEDB:
        return [
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL",
            "GRAPHSYMPERF_ARCADEDB_DATABASE",
            "GRAPHSYMPERF_ARCADEDB_USER",
            "GRAPHSYMPERF_ARCADEDB_PASSWORD",
        ]
    if target is TargetSystem.KUZU:
        return ["GRAPHSYMPERF_KUZU_DATABASE_PATH"]
    raise ConfigError(f"unsupported target: {target.value}")


def _default_optional_env(target: TargetSystem) -> Mapping[str, str]:
    if target is TargetSystem.NEO4J:
        return {"database": "GRAPHSYMPERF_NEO4J_DATABASE"}
    if target is TargetSystem.MEMGRAPH:
        return {
            "username": "GRAPHSYMPERF_MEMGRAPH_USER",
            "password": "GRAPHSYMPERF_MEMGRAPH_PASSWORD",
        }
    if target is TargetSystem.FALKORDB:
        return {
            "username": "GRAPHSYMPERF_FALKORDB_USER",
            "password": "GRAPHSYMPERF_FALKORDB_PASSWORD",
        }
    if target is TargetSystem.AGENSGRAPH:
        return {
            "username": "GRAPHSYMPERF_AGENSGRAPH_USER",
            "password": "GRAPHSYMPERF_AGENSGRAPH_PASSWORD",
            "database": "GRAPHSYMPERF_AGENSGRAPH_DATABASE",
        }
    if target is TargetSystem.APACHE_AGE:
        return {
            "username": "GRAPHSYMPERF_APACHE_AGE_USER",
            "password": "GRAPHSYMPERF_APACHE_AGE_PASSWORD",
            "database": "GRAPHSYMPERF_APACHE_AGE_DATABASE",
        }
    if target is TargetSystem.ARCADEDB:
        return {
            "bolt_host": "GRAPHSYMPERF_ARCADEDB_BOLT_HOST",
            "bolt_port": "GRAPHSYMPERF_ARCADEDB_BOLT_PORT",
        }
    return {}


def _default_capabilities(target: TargetSystem) -> EngineCapabilities:
    if target is TargetSystem.NEO4J:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_db_hits=True,
            has_actual_rows=True,
            has_estimated_rows=True,
            has_page_cache_metrics=True,
            has_memory_metrics=True,
            plan_tree_reliable=True,
            profile_text_reliable=True,
            supports_destructive_load=True,
            dialect="cypher",
        )
    if target is TargetSystem.MEMGRAPH:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            has_estimated_rows=True,
            profile_text_reliable=True,
            supports_destructive_load=True,
            dialect="cypher",
        )
    if target is TargetSystem.KUZU:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            has_estimated_rows=True,
            plan_tree_reliable=True,
            profile_text_reliable=True,
            dialect="cypher",
        )
    if target is TargetSystem.FALKORDB:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            dialect="cypher",
        )
    if target is TargetSystem.AGENSGRAPH:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            has_page_cache_metrics=True,
            profile_text_reliable=True,
            dialect="cypher",
        )
    if target is TargetSystem.APACHE_AGE:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            has_page_cache_metrics=True,
            profile_text_reliable=True,
            dialect="cypher",
        )
    if target is TargetSystem.ARCADEDB:
        return EngineCapabilities(
            supports_explain=True,
            supports_profile=True,
            has_actual_rows=True,
            profile_text_reliable=True,
            dialect="opencypher",
        )
    raise ConfigError(f"unsupported target: {target.value}")
