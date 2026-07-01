"""Experiment manifest loading and RunConfig projection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from graph_sym_perf.config.loader import (
    _default_capabilities,
    _default_optional_env,
    _default_required_env,
    _load_backend_extra_args,
)
from graph_sym_perf.core import (
    ClusteringConfig,
    ConfigError,
    ConfirmationConfig,
    CoverageConfig,
    FeedbackConfig,
    GdsmithConfig,
    OracleConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)


@dataclass(frozen=True)
class ExecutionConfig:
    shards: int = 1
    shard_index: int | None = None
    workers_per_shard: int = 1
    cases_per_shard: int = 1000
    per_query_timeout_seconds: int = 60
    per_case_timeout_seconds: int = 180
    resume: bool = True
    fail_fast: bool = False
    isolate_system_per_worker: bool = True
    write_every_n_cases: int = 50


@dataclass(frozen=True)
class ExperimentManifest:
    experiment_id: str
    description: str
    seed: int
    output_root: Path
    systems: tuple[SystemConfig, ...]
    source: Mapping[str, Any]
    transforms: Mapping[str, Any]
    coverage: CoverageConfig
    feedback: FeedbackConfig
    oracle: Mapping[str, Any]
    clustering: ClusteringConfig
    reducer: Mapping[str, Any]
    execution: ExecutionConfig
    artifacts: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def source_kind(self) -> str:
        return str(self.source.get("kind", "gdsmith"))

    @property
    def base_run_config_path(self) -> Path:
        raw = self.source.get("base_run_config") or self.source.get("run_config") or "config/graph_sym_perf.yaml"
        return Path(str(raw))

    def total_case_budget(self, num_shards: int | None = None) -> int:
        explicit = self.source.get("max_cases")
        if explicit is not None:
            return max(0, int(explicit))
        shards = num_shards if num_shards is not None else self.execution.shards
        return max(0, int(self.execution.cases_per_shard) * max(1, int(shards)))

    def to_run_config(
        self,
        *,
        shard_index: int | None = None,
        num_shards: int | None = None,
        output_root: Path | None = None,
    ) -> RunConfig:
        from graph_sym_perf.config import load_run_config

        base = load_run_config(self.base_run_config_path)
        run_root = Path(output_root) if output_root is not None else self.output_root
        total_shards = max(1, int(num_shards if num_shards is not None else self.execution.shards))
        shard_seed = self.seed + int(shard_index or 0)
        query_timeout = float(self.execution.per_query_timeout_seconds)

        transforms = _merge_transforms(base.transforms, self.transforms, seed=shard_seed)
        feedback = _merge_feedback(base.feedback, self.feedback, seed=shard_seed)
        coverage = self.coverage
        clustering = self.clustering
        oracle = _merge_oracle(base.oracle, self.oracle)
        query_generation = _merge_query_generation(
            base.query_generation,
            self.source,
            total_case_budget=self.total_case_budget(total_shards),
            seed=shard_seed,
        )
        confirmation = replace(base.confirmation, timeout_seconds=query_timeout)
        equivalence = replace(base.equivalence, timeout_seconds=query_timeout)
        systems = self.systems or base.systems
        return replace(
            base,
            source_path=str(self.metadata.get("manifest_path") or self.base_run_config_path),
            paths=RunPaths(artifact_dir=str(run_root / "{run_id}")),
            systems=systems,
            query_generation=query_generation,
            equivalence=equivalence,
            confirmation=confirmation,
            oracle=oracle,
            coverage=coverage,
            feedback=feedback,
            clustering=clustering,
            transforms=transforms,
        )


def load_experiment_manifest(path: str | Path) -> ExperimentManifest:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ConfigError(f"experiment manifest does not exist: {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid experiment YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("experiment manifest root must be a mapping")

    source = _mapping(raw.get("source"), "source")
    systems = _load_manifest_systems(raw.get("systems"))
    execution = _load_execution_config(raw.get("execution"))
    manifest = ExperimentManifest(
        experiment_id=str(raw.get("experiment_id") or manifest_path.stem),
        description=str(raw.get("description") or ""),
        seed=int(raw.get("seed", 20260522)),
        output_root=Path(str(raw.get("output_root") or f"runs/{manifest_path.stem}")),
        systems=systems,
        source=source,
        transforms=_mapping(raw.get("transforms"), "transforms"),
        coverage=_load_coverage_config(raw.get("coverage")),
        feedback=_load_feedback_config(raw.get("feedback")),
        oracle=_mapping(raw.get("oracle"), "oracle"),
        clustering=_load_clustering_config(raw.get("clustering")),
        reducer=_mapping(raw.get("reducer"), "reducer"),
        execution=execution,
        artifacts=_mapping(raw.get("artifacts"), "artifacts"),
        metadata=_mapping(raw.get("metadata"), "metadata")
        | {"manifest_path": str(manifest_path.resolve())},
    )
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: ExperimentManifest) -> None:
    if not manifest.experiment_id.strip():
        raise ConfigError("experiment_id must not be empty")
    if manifest.execution.shards <= 0:
        raise ConfigError("execution.shards must be greater than 0")
    if manifest.execution.cases_per_shard <= 0:
        raise ConfigError("execution.cases_per_shard must be greater than 0")
    if not manifest.systems:
        raise ConfigError("systems.targets must contain at least one target")
    if manifest.source_kind not in {"gdsmith", "ldbc", "cases_jsonl"}:
        raise ConfigError("source.kind must be one of: gdsmith, ldbc, cases_jsonl")


def _load_manifest_systems(raw: Any) -> tuple[SystemConfig, ...]:
    mapping = _mapping(raw, "systems")
    targets_raw = mapping.get("targets") or ["neo4j"]
    if not isinstance(targets_raw, list | tuple) or not targets_raw:
        raise ConfigError("systems.targets must be a non-empty list")
    env_mapping = _mapping(mapping.get("env"), "systems.env")
    settings_mapping = _mapping(mapping.get("settings"), "systems.settings")
    systems: list[SystemConfig] = []
    for item in targets_raw:
        target = TargetSystem.parse(str(item))
        target_env = _mapping(env_mapping.get(target.value), f"systems.env.{target.value}")
        required = target_env.get("required") or _default_required_env(target)
        optional = target_env.get("optional") or _default_optional_env(target)
        if not isinstance(required, list | tuple):
            raise ConfigError(f"systems.env.{target.value}.required must be a list")
        if not isinstance(optional, Mapping):
            raise ConfigError(f"systems.env.{target.value}.optional must be a mapping")
        systems.append(
            SystemConfig(
                target=target,
                required_env=tuple(str(name) for name in required),
                optional_env={str(key): str(value) for key, value in optional.items()},
                settings=dict(_mapping(settings_mapping.get(target.value), f"systems.settings.{target.value}")),
                capabilities=_default_capabilities(target),
            )
        )
    return tuple(systems)


def _load_execution_config(raw: Any) -> ExecutionConfig:
    mapping = _mapping(raw, "execution")
    return ExecutionConfig(
        shards=int(mapping.get("shards", 1)),
        shard_index=(int(mapping["shard_index"]) if mapping.get("shard_index") is not None else None),
        workers_per_shard=int(mapping.get("workers_per_shard", 1)),
        cases_per_shard=int(mapping.get("cases_per_shard", 1000)),
        per_query_timeout_seconds=int(mapping.get("per_query_timeout_seconds", 60)),
        per_case_timeout_seconds=int(mapping.get("per_case_timeout_seconds", 180)),
        resume=bool(mapping.get("resume", True)),
        fail_fast=bool(mapping.get("fail_fast", False)),
        isolate_system_per_worker=bool(mapping.get("isolate_system_per_worker", True)),
        write_every_n_cases=int(mapping.get("write_every_n_cases", 50)),
    )


def _load_coverage_config(raw: Any) -> CoverageConfig:
    mapping = _mapping(raw, "coverage")
    defaults = CoverageConfig()
    novelty_weights = dict(defaults.novelty_weights)
    novelty_weights.update({str(key): float(value) for key, value in _mapping(mapping.get("novelty_weights"), "coverage.novelty_weights").items()})
    ratio_buckets = mapping.get("ratio_buckets", defaults.ratio_buckets)
    return CoverageConfig(
        enabled=bool(mapping.get("enabled", defaults.enabled)),
        include_transformation_context=bool(
            mapping.get("include_transformation_context", defaults.include_transformation_context)
        ),
        write_per_case_evidence=bool(mapping.get("write_per_case_evidence", defaults.write_per_case_evidence)),
        ratio_buckets=tuple(float(value) for value in ratio_buckets),
        novelty_weights=novelty_weights,
    )


def _load_feedback_config(raw: Any) -> FeedbackConfig:
    mapping = _mapping(raw, "feedback")
    defaults = FeedbackConfig()
    reward_weights = dict(defaults.reward_weights)
    reward_weights.update({str(key): float(value) for key, value in _mapping(mapping.get("reward_weights"), "feedback.reward_weights").items()})
    return FeedbackConfig(
        enabled=bool(mapping.get("enabled", defaults.enabled)),
        exploration_rate=float(mapping.get("exploration_rate", defaults.exploration_rate)),
        seed=int(mapping.get("seed", defaults.seed)),
        min_rules_per_case=int(mapping.get("min_rules_per_case", defaults.min_rules_per_case)),
        max_rules_per_case=(
            int(mapping["max_rules_per_case"]) if mapping.get("max_rules_per_case") is not None else defaults.max_rules_per_case
        ),
        write_artifacts=bool(mapping.get("write_artifacts", defaults.write_artifacts)),
        reward_weights=reward_weights,
    )


def _load_clustering_config(raw: Any) -> ClusteringConfig:
    mapping = _mapping(raw, "clustering")
    defaults = ClusteringConfig()
    return ClusteringConfig(
        enabled=bool(mapping.get("enabled", defaults.enabled)),
        write_signatures=bool(mapping.get("write_signatures", defaults.write_signatures)),
        representative_policy=str(mapping.get("representative_policy", defaults.representative_policy)),
        include_plan_only=bool(mapping.get("include_plan_only", defaults.include_plan_only)),
        include_exploratory=bool(mapping.get("include_exploratory", defaults.include_exploratory)),
    )


def _merge_query_generation(
    base: QueryGenerationConfig,
    source: Mapping[str, Any],
    *,
    total_case_budget: int,
    seed: int,
) -> QueryGenerationConfig:
    kind = str(source.get("kind", base.source))
    gdsmith_mapping = _mapping(source.get("gdsmith"), "source.gdsmith")
    gdsmith = replace(
        base.gdsmith,
        algorithm=str(gdsmith_mapping.get("algorithm", base.gdsmith.algorithm)),
        num_tries=int(gdsmith_mapping.get("num_tries", base.gdsmith.num_tries)),
        num_queries=int(gdsmith_mapping.get("num_queries", total_case_budget)),
        random_seed=int(gdsmith_mapping.get("random_seed", seed)),
        allow_destructive_env=str(
            gdsmith_mapping.get("allow_destructive_env", base.gdsmith.allow_destructive_env)
        ),
        no_proxy_launcher_env=str(
            gdsmith_mapping.get("no_proxy_launcher_env", base.gdsmith.no_proxy_launcher_env)
        ),
        jar_env=str(gdsmith_mapping.get("jar_env", base.gdsmith.jar_env)),
        backend_extra_args=_load_backend_extra_args(
            gdsmith_mapping.get("backend_extra_args"), "source.gdsmith.backend_extra_args"
        ),
        timeout_seconds=float(gdsmith_mapping.get("timeout_seconds", base.gdsmith.timeout_seconds)),
    )
    return replace(
        base,
        max_cases=int(source.get("max_cases", total_case_budget)),
        result_limit=int(source.get("result_limit", base.result_limit)),
        include_variable_length=bool(source.get("include_variable_length", base.include_variable_length)),
        source=kind,
        gdsmith=gdsmith,
    )


def _merge_oracle(base: OracleConfig, raw: Mapping[str, Any]) -> OracleConfig:
    return replace(
        base,
        enable_structural_diff=bool(raw.get("enable_structural_diff", base.enable_structural_diff)),
        enable_profile_metrics=bool(raw.get("enable_profile_metrics", base.enable_profile_metrics)),
    )


def _merge_feedback(base: FeedbackConfig, raw: FeedbackConfig, *, seed: int) -> FeedbackConfig:
    return replace(raw, seed=seed if raw.seed == FeedbackConfig().seed else raw.seed, max_rules_per_case=raw.max_rules_per_case)


def _merge_transforms(base: TransformConfig, raw: Mapping[str, Any], *, seed: int) -> TransformConfig:
    enabled_families = raw.get("enabled_families", base.enabled_families)
    enabled_rules = raw.get("enabled_rules", base.enabled_rules)
    return TransformConfig(
        enabled_families=tuple(str(item) for item in enabled_families),
        enabled_rules=tuple(str(item) for item in enabled_rules),
        max_expr_complexity=str(raw.get("max_expr_complexity", base.max_expr_complexity)),
        gdsmith_safe_only=bool(raw.get("gdsmith_safe_only", base.gdsmith_safe_only)),
        max_variants_per_case=(
            int(raw["max_variants_per_case"]) if raw.get("max_variants_per_case") is not None else base.max_variants_per_case
        ),
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return value
