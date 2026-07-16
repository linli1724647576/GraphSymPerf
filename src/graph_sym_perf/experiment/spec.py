"""Strict loaders for historical schema-v2 and validated-yield schema-v3."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

import yaml

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.plan_models import (
    CampaignBudgetSpec,
    CampaignMatrixSpec,
    ExperimentSpec,
    FrozenMapping,
    GenerationSpec,
    MatrixSpec,
    PhaseBudgetSpec,
    RuleSelectionSpec,
    SystemDefinition,
    ValidatedYieldExperimentSpec,
    WorkloadSpec,
    freeze_value,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


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

_V3_POLICY_SCHEMAS: Mapping[str, Mapping[str, object | None]] = {
    "runtime": {
        "noise": {"probe_queries": None, "aa_pairs_per_query": None, "absolute_delta_quantile": None},
        "stage1": {"warmups": None, "pairs": None, "top_fraction": None, "min_top_k": None, "max_top_k": None},
        "stage2": {
            "warmups": None,
            "pairs": None,
            "relative_ratio": None,
            "relative_ci_low": None,
            "absolute_delta_ms": None,
            "direction_consistency": None,
        },
    },
    "validation": {
        "sessions": None,
        "warmups_per_session": None,
        "pairs_per_session": None,
        "bootstrap_resamples": None,
        "relative": {"session_ratio": None, "pooled_ratio": None, "ratio_ci_low": None},
        "absolute": {"noise_multiplier": None, "delta_ms": None, "pooled_ratio": None, "ratio_ci_low": None},
        "max_result_rows": None,
    },
    "reducer": {
        "enabled": None,
        "max_attempts": None,
        "max_seconds": None,
        "require_reduced_validation": None,
    },
    "artifacts": {"entry_budget_mb": None},
}

MAX_P2_RULE_SCALE_CELLS = 6

_SCHEMA_V3_NEO4J_CAPABILITY_CONTRACT: Mapping[str, object] = {
    "generation_backend": "neo4j",
    "capabilities": ["multiple_node_labels", "native_parameters"],
    "rule_capabilities": ["multi_label_intersection"],
    "dialect": "cypher",
    "graph_isolation": "database",
}


def load_experiment_spec(path: str | Path) -> ExperimentSpec | ValidatedYieldExperimentSpec:
    source_path = Path(path)
    raw = _load_yaml(source_path, "experiment configuration")
    if raw.get("schema_version") == 3:
        return _load_schema_v3(source_path, raw)
    return _load_schema_v2(source_path, raw)


def _load_schema_v2(source_path: Path, raw: Mapping[str, Any]) -> ExperimentSpec:
    _only_keys(
        raw,
        {
            "schema_version", "experiment", "targets", "matrix", "workload", "generation", "scheduling",
            "runtime", "validation", "reducer", "rules", "artifacts",
        },
        "root",
    )
    if raw.get("schema_version") != 2:
        raise ConfigError("formal experiment configuration must use schema_version: 2 or 3")

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
    if workload.max_generated_seeds_per_shard > 10_000:
        raise ConfigError("workload.max_generated_seeds_per_shard must not exceed the formal 10000-seed bound")
    if workload.max_shard_runtime_hours > 6:
        raise ConfigError("workload.max_shard_runtime_hours must not exceed the formal 6-hour bound")

    generation_raw = _mapping(raw.get("generation"), "generation")
    _exact_keys(
        generation_raw,
        {"backend", "commit_source", "expected_commit", "checkout", "source", "required_env"},
        "generation",
    )
    backend = _nonempty_string(generation_raw.get("backend"), "generation.backend")
    if backend != "gdsmith" or generation_raw.get("source", "gdsmith") != "gdsmith":
        raise ConfigError("generation.backend/source must be gdsmith; alternate generators are unsupported")
    commit_source = _nonempty_string(generation_raw.get("commit_source"), "generation.commit_source")
    if commit_source != "vendored_checkout":
        raise ConfigError("generation.commit_source must be vendored_checkout")
    expected_commit = _nonempty_string(generation_raw.get("expected_commit"), "generation.expected_commit")
    if len(expected_commit) != 40 or any(char not in "0123456789abcdef" for char in expected_commit):
        raise ConfigError("generation.expected_commit must be a full lowercase 40-hex Git commit")
    checkout = Path(_nonempty_string(generation_raw.get("checkout"), "generation.checkout"))
    if not checkout.is_absolute():
        checkout = (source_path.parent / checkout).resolve()
    generation = GenerationSpec(
        backend=backend,
        commit_source=commit_source,
        expected_commit=expected_commit,
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


def _load_schema_v3(
    source_path: Path,
    raw: Mapping[str, Any],
) -> ValidatedYieldExperimentSpec:
    _exact_keys(
        raw,
        {
            "schema_version",
            "experiment",
            "targets",
            "matrix",
            "campaign",
            "rules",
            "generation",
            "runtime",
            "validation",
            "reducer",
            "regression",
            "artifacts",
        },
        "root",
    )

    experiment = _mapping(raw["experiment"], "experiment")
    _exact_keys(experiment, {"name", "seed"}, "experiment")
    name = _nonempty_string(experiment["name"], "experiment.name")
    seed = _positive_int(experiment["seed"], "experiment.seed", allow_zero=True)

    targets_raw = raw["targets"]
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("targets must be a non-empty list")
    if targets_raw != [TargetSystem.NEO4J.value]:
        raise ConfigError(
            "schema-v3 targets must be exactly the canonical single target [neo4j]"
        )
    targets = (TargetSystem.NEO4J,)

    matrix_raw = _mapping(raw["matrix"], "matrix")
    _exact_keys(matrix_raw, {"graph_scales"}, "matrix")
    scales_raw = matrix_raw["graph_scales"]
    if not isinstance(scales_raw, list) or not scales_raw:
        raise ConfigError("matrix.graph_scales must be a non-empty list")
    graph_scales = tuple(
        _positive_int(item, f"matrix.graph_scales[{index}]")
        for index, item in enumerate(scales_raw)
    )
    if len(set(graph_scales)) != len(graph_scales):
        raise ConfigError("matrix.graph_scales must not contain duplicates")
    matrix = CampaignMatrixSpec(graph_scales=graph_scales)

    campaign_raw = _mapping(raw["campaign"], "campaign")
    _exact_keys(campaign_raw, {"p0", "p1", "p2", "generated_seed_multiplier"}, "campaign")
    p2_budget = _phase_budget(campaign_raw["p2"], "campaign.p2", allow_cell_limit=True)
    if p2_budget.max_rule_scale_cells != MAX_P2_RULE_SCALE_CELLS:
        raise ConfigError(
            "campaign.p2.max_rule_scale_cells must be exactly "
            f"{MAX_P2_RULE_SCALE_CELLS}"
        )
    campaign = CampaignBudgetSpec(
        p0=_phase_budget(campaign_raw["p0"], "campaign.p0", allow_cell_limit=False),
        p1=_phase_budget(campaign_raw["p1"], "campaign.p1", allow_cell_limit=False),
        p2=p2_budget,
        generated_seed_multiplier=_positive_int(
            campaign_raw["generated_seed_multiplier"],
            "campaign.generated_seed_multiplier",
        ),
    )

    generation_raw = _mapping(raw["generation"], "generation")
    _exact_keys(
        generation_raw,
        {
            "backend",
            "source",
            "algorithm",
            "commit_source",
            "expected_commit",
            "checkout",
            "required_env",
        },
        "generation",
    )
    backend = _nonempty_string(generation_raw["backend"], "generation.backend")
    if backend != "gdsmith" or generation_raw["source"] != "gdsmith":
        raise ConfigError("generation.backend/source must be gdsmith; alternate generators are unsupported")
    algorithm = _enum(generation_raw["algorithm"], {"COMPARED3"}, "generation.algorithm")
    commit_source = _enum(
        generation_raw["commit_source"],
        {"vendored_checkout"},
        "generation.commit_source",
    )
    expected_commit = _git_commit(generation_raw["expected_commit"], "generation.expected_commit")
    checkout = Path(_nonempty_string(generation_raw["checkout"], "generation.checkout"))
    if not checkout.is_absolute():
        checkout = (source_path.parent / checkout).resolve()
    generation = GenerationSpec(
        backend=backend,
        commit_source=commit_source,
        expected_commit=expected_commit,
        checkout=checkout,
        required_env=_environment_names(generation_raw["required_env"], "generation.required_env"),
        algorithm=algorithm,
    )

    policies = {
        policy_name: _strict_v3_policy(raw[policy_name], policy_name)
        for policy_name in ("runtime", "validation", "reducer", "artifacts")
    }

    regression_raw = _mapping(raw["regression"], "regression")
    _exact_keys(
        regression_raw,
        {"manifest", "max_entry_runtime_seconds"},
        "regression",
    )
    regression_manifest = Path(
        _nonempty_string(regression_raw["manifest"], "regression.manifest")
    )
    if not regression_manifest.is_absolute():
        regression_manifest = (source_path.parent.parent / regression_manifest).resolve()
    regression = FrozenMapping(
        {
            "manifest": regression_manifest,
            "max_entry_runtime_seconds": _positive_int(
                regression_raw["max_entry_runtime_seconds"],
                "regression.max_entry_runtime_seconds",
            ),
        }
    )

    rules_raw = _mapping(raw["rules"], "rules")
    _exact_keys(rules_raw, {"discovery_rule_ids", "control_rule_ids"}, "rules")
    systems_path = source_path.parent / "systems.yaml"
    systems_document = _load_yaml(systems_path, "systems configuration")
    _require_schema_v3_neo4j_capability_contract(systems_document)
    systems = _parse_systems(systems_document, targets)
    if (
        TargetSystem.NEO4J in systems
        and systems[TargetSystem.NEO4J].measurement_target_id is None
    ):
        raise ConfigError(
            "systems.neo4j.measurement_target_id is required for schema-v3 campaigns"
        )
    rules_path = source_path.parent / "rules.yaml"
    rules_catalog, rules_schema_sha256 = _load_rules(rules_path)
    registry = RuleRegistry.default()
    discovery_rule_ids = _canonical_rule_ids(
        rules_raw["discovery_rule_ids"],
        "rules.discovery_rule_ids",
        registry,
    )
    control_rule_ids = _canonical_rule_ids(
        rules_raw["control_rule_ids"],
        "rules.control_rule_ids",
        registry,
    )
    for target in targets:
        definition = systems[target]
        active_core = {
            item.rule_id
            for item in registry.rules_for(
                target.value,
                SafetyTier.CORE,
                frozenset(definition.rule_capabilities),
            )
        }
        active_control = {
            item.rule_id
            for item in registry.rules_for(
                target.value,
                SafetyTier.CONTROL,
                frozenset(definition.rule_capabilities),
            )
        }
        inactive = sorted(set(discovery_rule_ids) - active_core)
        if inactive:
            raise ConfigError(
                f"rules.discovery_rule_ids contains inactive CORE rules for "
                f"{target.value}: {inactive}"
            )
        invalid_controls = sorted(set(control_rule_ids) - active_control)
        if invalid_controls:
            raise ConfigError(
                f"rules.control_rule_ids contains inactive CONTROL rules for {target.value}: "
                f"{invalid_controls}"
            )

    return ValidatedYieldExperimentSpec(
        schema_version=3,
        name=name,
        seed=seed,
        targets=targets,
        matrix=matrix,
        campaign=campaign,
        rules=RuleSelectionSpec(
            discovery_rule_ids=discovery_rule_ids,
            control_rule_ids=control_rule_ids,
        ),
        generation=generation,
        runtime=policies["runtime"],
        validation=policies["validation"],
        reducer=policies["reducer"],
        regression=regression,
        artifacts=policies["artifacts"],
        systems=systems,
        rules_catalog=rules_catalog,
        rules_schema_sha256=rules_schema_sha256,
        rules_source_path=rules_path.resolve(),
        source_path=source_path.resolve(),
    )


def _require_schema_v3_neo4j_capability_contract(
    document: Mapping[str, Any],
) -> None:
    systems = _mapping(document.get("systems"), "systems")
    neo4j = _mapping(systems.get(TargetSystem.NEO4J.value), "systems.neo4j")
    if any(
        neo4j.get(field) != expected
        for field, expected in _SCHEMA_V3_NEO4J_CAPABILITY_CONTRACT.items()
    ):
        raise ConfigError(
            "schema-v3 requires the canonical Neo4j capability contract"
        )


def _phase_budget(value: Any, label: str, *, allow_cell_limit: bool) -> PhaseBudgetSpec:
    raw = _mapping(value, label)
    expected = {"accepted_pairs", "shards", "max_entry_runtime_seconds"}
    if allow_cell_limit:
        expected.add("max_rule_scale_cells")
    _exact_keys(raw, expected, label)
    return PhaseBudgetSpec(
        accepted_pairs=_positive_int(raw["accepted_pairs"], f"{label}.accepted_pairs"),
        shards=_positive_int(raw["shards"], f"{label}.shards"),
        max_entry_runtime_seconds=_positive_int(
            raw["max_entry_runtime_seconds"],
            f"{label}.max_entry_runtime_seconds",
        ),
        max_rule_scale_cells=(
            _positive_int(raw["max_rule_scale_cells"], f"{label}.max_rule_scale_cells")
            if allow_cell_limit
            else None
        ),
    )


def _canonical_rule_ids(value: Any, label: str, registry: RuleRegistry) -> tuple[str, ...]:
    raw_ids = _string_names(value, label)
    resolved: list[str] = []
    for rule_id in raw_ids:
        try:
            resolved.append(registry.resolve_id(rule_id))
        except KeyError as exc:
            raise ConfigError(f"{label} contains unknown rule: {rule_id}") from exc
    if len(set(resolved)) != len(resolved):
        raise ConfigError(f"{label} contains duplicate canonical rules")
    return tuple(resolved)


def _load_systems(path: Path, targets: tuple[TargetSystem, ...]) -> Mapping[TargetSystem, SystemDefinition]:
    raw = _load_yaml(path, "systems configuration")
    return _parse_systems(raw, targets)


def _parse_systems(
    raw: Mapping[str, Any],
    targets: tuple[TargetSystem, ...],
) -> Mapping[TargetSystem, SystemDefinition]:
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
        required_fields = {
            "required_env",
            "generation_required_env",
            "generation_backend",
            "capabilities",
            "dialect",
            "graph_isolation",
            "default_timeout_seconds",
        }
        _only_keys(
            item,
            required_fields | {"rule_capabilities", "measurement_target_id"},
            f"systems.{target.value}",
        )
        missing_fields = required_fields - set(item)
        if missing_fields:
            raise ConfigError(
                f"missing field(s) in systems.{target.value}: {', '.join(sorted(missing_fields))}"
            )
        env = _environment_names(item.get("required_env"), f"systems.{target.value}.required_env")
        generation_env = _environment_names(item.get("generation_required_env"), f"systems.{target.value}.generation_required_env")
        available[target] = SystemDefinition(
            target=target,
            required_env=tuple(env),
            generation_required_env=tuple(generation_env),
            generation_backend=_nonempty_string(item.get("generation_backend"), f"systems.{target.value}.generation_backend"),
            capabilities=_string_names(item.get("capabilities"), f"systems.{target.value}.capabilities"),
            dialect=_nonempty_string(item.get("dialect"), f"systems.{target.value}.dialect"),
            graph_isolation=_nonempty_string(item.get("graph_isolation"), f"systems.{target.value}.graph_isolation"),
            default_timeout_seconds=_positive_int(item.get("default_timeout_seconds"), f"systems.{target.value}.default_timeout_seconds"),
            rule_capabilities=(
                _string_names(item["rule_capabilities"], f"systems.{target.value}.rule_capabilities")
                if "rule_capabilities" in item
                else ()
            ),
            measurement_target_id=(
                _measurement_target_id(
                    item["measurement_target_id"],
                    f"systems.{target.value}.measurement_target_id",
                )
                if "measurement_target_id" in item
                else None
            ),
        )
    missing = [target.value for target in targets if target not in available]
    if missing:
        raise ConfigError(f"missing system definition(s): {', '.join(missing)}")
    return FrozenMapping({target: available[target] for target in targets})


def _measurement_target_id(value: Any, label: str) -> str:
    target_id = _nonempty_string(value, label)
    valid_characters = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    if (
        len(target_id) > 128
        or target_id[0] not in valid_characters - frozenset("._-")
        or any(character not in valid_characters for character in target_id)
    ):
        raise ConfigError(
            f"{label} must be an ASCII identifier of at most 128 characters"
        )
    return target_id


def _load_rules(path: Path) -> tuple[Mapping[str, object], str]:
    raw_bytes = path.read_bytes() if path.exists() else b""
    raw = _load_yaml(path, "rules configuration")
    _exact_keys(raw, {"schema_version", "tiers", "rules"}, "rules root")
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
    rules = _mapping(raw["rules"], "rules.rules")
    registry = RuleRegistry.default()
    active = {rule.rule_id: rule for rule in registry.rules if rule.tier is not SafetyTier.REMOVED}
    _exact_keys(rules, set(active), "rules.rules")
    minimum = int(core["minimum_accepted_pairs_per_rule"])
    for rule_id, spec in active.items():
        item = _mapping(rules[rule_id], f"rules.rules.{rule_id}")
        _exact_keys(
            item,
            {"tier", "version", "proof_id", "site_classes", "exploration_floor"},
            f"rules.rules.{rule_id}",
        )
        if item["tier"] != spec.tier.value or item["version"] != spec.version or item["proof_id"] != spec.proof_id:
            raise ConfigError(f"rules.rules.{rule_id} does not match the audited code registry")
        sites = item["site_classes"]
        if not isinstance(sites, list) or tuple(sites) != spec.site_classes:
            raise ConfigError(f"rules.rules.{rule_id}.site_classes does not match the audited code registry")
        floor = _positive_int(item["exploration_floor"], f"rules.rules.{rule_id}.exploration_floor", allow_zero=True)
        expected_floor = minimum if spec.tier is SafetyTier.CORE else 0
        if floor != expected_floor:
            raise ConfigError(
                f"rules.rules.{rule_id}.exploration_floor must be {expected_floor} for {spec.tier.value}"
            )
    return freeze_value(raw), hashlib.sha256(raw_bytes).hexdigest()


def _environment_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(name, str) and name for name in value):
        raise ConfigError(f"{label} must be a list of non-empty environment variable names")
    return tuple(value)


def _string_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value or not all(isinstance(name, str) and name for name in value):
        raise ConfigError(f"{label} must be a non-empty list of names")
    if len(set(value)) != len(value):
        raise ConfigError(f"{label} must not contain duplicates")
    return tuple(value)


def _strict_policy(raw: Any, name: str) -> Mapping[str, object]:
    value = _mapping(raw, name)
    if not value:
        raise ConfigError(f"{name} must not be empty")
    _validate_policy(value, _POLICY_SCHEMAS[name], name)
    _validate_policy_values(name, value)
    return freeze_value(value)


def _strict_v3_policy(raw: Any, name: str) -> Mapping[str, object]:
    value = _mapping(raw, name)
    if not value:
        raise ConfigError(f"{name} must not be empty")
    _validate_policy(value, _V3_POLICY_SCHEMAS[name], name)
    _validate_v3_policy_values(name, value)
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
        required = {"plan", "profile", "pmc", "developer_status"}
        if set(feedback) != required:
            raise ConfigError(
                "rules.forbidden_feedback must contain exactly plan, profile, pmc, and developer_status"
            )
        return
    for key in ("default_level", "promote_candidate_level", "promote_issue_level"):
        _nonempty_string(value[key], f"artifacts.{key}")
    _positive_int(value["shard_budget_mb"], "artifacts.shard_budget_mb")


def _validate_v3_policy_values(name: str, value: Mapping[str, Any]) -> None:
    if name == "runtime":
        noise = _mapping(value["noise"], "runtime.noise")
        _positive_int(noise["probe_queries"], "runtime.noise.probe_queries")
        _positive_int(noise["aa_pairs_per_query"], "runtime.noise.aa_pairs_per_query")
        quantile = _positive_number(
            noise["absolute_delta_quantile"],
            "runtime.noise.absolute_delta_quantile",
        )
        if quantile > 1:
            raise ConfigError("runtime.noise.absolute_delta_quantile must not exceed 1")
        stage1 = _mapping(value["stage1"], "runtime.stage1")
        _positive_int(stage1["warmups"], "runtime.stage1.warmups", allow_zero=True)
        _positive_int(stage1["pairs"], "runtime.stage1.pairs")
        top_fraction = _positive_number(stage1["top_fraction"], "runtime.stage1.top_fraction")
        if top_fraction > 1:
            raise ConfigError("runtime.stage1.top_fraction must not exceed 1")
        minimum_top_k = _positive_int(stage1["min_top_k"], "runtime.stage1.min_top_k")
        maximum_top_k = _positive_int(stage1["max_top_k"], "runtime.stage1.max_top_k")
        if minimum_top_k > maximum_top_k:
            raise ConfigError("runtime.stage1.min_top_k must not exceed max_top_k")
        stage2 = _mapping(value["stage2"], "runtime.stage2")
        _positive_int(stage2["warmups"], "runtime.stage2.warmups", allow_zero=True)
        _positive_int(stage2["pairs"], "runtime.stage2.pairs")
        for key in ("relative_ratio", "relative_ci_low"):
            if _positive_number(stage2[key], f"runtime.stage2.{key}") <= 1:
                raise ConfigError(f"runtime.stage2.{key} must exceed 1")
        _positive_number(stage2["absolute_delta_ms"], "runtime.stage2.absolute_delta_ms")
        consistency = _positive_number(
            stage2["direction_consistency"],
            "runtime.stage2.direction_consistency",
        )
        if consistency > 1:
            raise ConfigError("runtime.stage2.direction_consistency must not exceed 1")
        return
    if name == "validation":
        for key in ("sessions", "pairs_per_session", "bootstrap_resamples", "max_result_rows"):
            _positive_int(value[key], f"validation.{key}")
        _positive_int(value["warmups_per_session"], "validation.warmups_per_session", allow_zero=True)
        for group_name in ("relative", "absolute"):
            group = _mapping(value[group_name], f"validation.{group_name}")
            for key, item in group.items():
                threshold = _positive_number(item, f"validation.{group_name}.{key}")
                if "ratio" in key and threshold <= 1:
                    raise ConfigError(f"validation.{group_name}.{key} must exceed 1")
        return
    if name == "reducer":
        _bool(value["enabled"], "reducer.enabled")
        _positive_int(value["max_attempts"], "reducer.max_attempts")
        _positive_int(value["max_seconds"], "reducer.max_seconds")
        _bool(value["require_reduced_validation"], "reducer.require_reduced_validation")
        return
    _positive_int(value["entry_budget_mb"], "artifacts.entry_budget_mb")


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


def _git_commit(value: Any, label: str) -> str:
    commit = _nonempty_string(value, label)
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ConfigError(f"{label} must be a full lowercase 40-hex Git commit")
    return commit


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
