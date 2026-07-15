"""Deterministic, DBMS-free planning for schema-v2 and schema-v3 experiments."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.core import ConfigError, GdsmithConfig, TargetSystem, to_jsonable
from graph_sym_perf.gdsmith.runner import check_gdsmith_tooling
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    EnvironmentVariableState,
    ExperimentSpec,
    RunPlan,
    RulePlan,
    RulePlanEntry,
    RuleStream,
    ShardTask,
    SourceRevision,
    ValidatedYieldExperimentSpec,
    freeze_value,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier
from graph_sym_perf.validation.regression import (
    RegressionConfigurationError,
    RegressionManifest,
)


MAX_TOTAL_ACCEPTED_PAIR_BUDGET = 112_000
CONTROL_ACCEPTED_PAIR_QUOTA = 50
MAX_VARIANTS_PER_SEED = 2
MEASUREMENT_PROTOCOL_ID = "validated-yield-v1"
MAX_P2_RULE_SCALE_CELLS = 6


def build_rule_plan(
    spec: ValidatedYieldExperimentSpec,
    phase: CampaignPhase,
    selected_cells: tuple[tuple[str, int], ...] = (),
    *,
    environment: Mapping[str, str] | None = None,
) -> RulePlan:
    if not isinstance(spec, ValidatedYieldExperimentSpec):
        raise ConfigError("schema-v3 validated-yield spec is required for RulePlan")
    if not isinstance(phase, CampaignPhase):
        raise ConfigError("phase must be a CampaignPhase")
    if phase is not CampaignPhase.P2 and selected_cells:
        raise ConfigError("selected_cells are only valid for P2")

    registry = RuleRegistry.default()
    if phase is CampaignPhase.P2:
        selected = _validated_selected_cells(spec, selected_cells, registry)
        discovery_cells = selected
        control_cells: tuple[tuple[str, int], ...] = ()
        phase_budget = spec.campaign.p2
    elif phase is CampaignPhase.P1:
        discovery_cells = tuple(
            (rule_id, graph_scale)
            for rule_id in spec.rules.discovery_rule_ids
            for graph_scale in spec.matrix.graph_scales
        )
        control_cells = tuple(
            (rule_id, graph_scale)
            for rule_id in spec.rules.control_rule_ids
            for graph_scale in spec.matrix.graph_scales
        )
        phase_budget = spec.campaign.p1
    else:
        graph_scale = spec.matrix.graph_scales[0]
        discovery_cells = tuple(
            (rule_id, graph_scale) for rule_id in spec.rules.discovery_rule_ids
        )
        control_cells = tuple(
            (rule_id, graph_scale) for rule_id in spec.rules.control_rule_ids
        )
        phase_budget = spec.campaign.p0

    entries: list[RulePlanEntry] = []
    for target in spec.targets:
        definition = spec.systems[target]
        active_core = {
            rule.rule_id: rule
            for rule in registry.rules_for(
                target.value,
                SafetyTier.CORE,
                frozenset(definition.rule_capabilities),
            )
        }
        active_control = {
            rule.rule_id: rule
            for rule in registry.rules_for(
                target.value,
                SafetyTier.CONTROL,
                frozenset(definition.rule_capabilities),
            )
        }
        entries.extend(
            _expand_rule_cells(
                spec,
                phase,
                target,
                discovery_cells,
                active_core,
                RuleStream.DISCOVERY,
                phase_budget.accepted_pairs,
                phase_budget.shards,
                phase_budget.max_entry_runtime_seconds,
            )
        )
        entries.extend(
            _expand_rule_cells(
                spec,
                phase,
                target,
                control_cells,
                active_control,
                RuleStream.CONTROL,
                CONTROL_ACCEPTED_PAIR_QUOTA,
                phase_budget.shards,
                phase_budget.max_entry_runtime_seconds,
            )
        )

    if phase is CampaignPhase.P0:
        regression_manifest = spec.regression["manifest"]
        if not isinstance(regression_manifest, Path) or not regression_manifest.is_file():
            raise ConfigError(f"regression manifest not found: {regression_manifest}")
        entries.append(
            _p0_regression_entry(
                spec,
                regression_manifest,
                registry,
                max_entry_runtime_seconds=phase_budget.max_entry_runtime_seconds,
            )
        )

    if len({entry.shard_seed for entry in entries}) != len(entries):
        raise ConfigError("deterministic RulePlan seed collision")

    source_revision = _rule_plan_source_revision(spec, environment=environment)
    resolved_entries = tuple(entries)
    return RulePlan(
        spec_name=spec.name,
        schema_version=spec.schema_version,
        phase=phase,
        entries=resolved_entries,
        discovery_accepted_pair_budget=sum(
            entry.accepted_pair_quota
            for entry in resolved_entries
            if entry.stream is RuleStream.DISCOVERY
        ),
        control_accepted_pair_budget=sum(
            entry.accepted_pair_quota
            for entry in resolved_entries
            if entry.stream is RuleStream.CONTROL
        ),
        source_revision=source_revision,
    )


def _p0_regression_entry(
    spec: ValidatedYieldExperimentSpec,
    manifest_path: Path,
    registry: RuleRegistry,
    *,
    max_entry_runtime_seconds: int,
) -> RulePlanEntry:
    try:
        manifest = RegressionManifest.load(manifest_path)
    except RegressionConfigurationError as exc:
        raise ConfigError(str(exc)) from exc
    case_id = manifest.case_id
    rule_id = manifest.canonical_rule_id
    graph_nodes = manifest.expected_graph_nodes
    if manifest.dbms != TargetSystem.NEO4J.value or TargetSystem.NEO4J not in spec.targets:
        raise ConfigError("P0 regression manifest target must be configured Neo4j")
    try:
        rule = registry.get(rule_id)
    except KeyError as exc:
        raise ConfigError(f"regression manifest contains unknown rule: {rule_id}") from exc
    if rule.rule_id not in spec.rules.discovery_rule_ids:
        raise ConfigError("regression manifest rule must be an approved discovery rule")
    seed = _entry_seed(
        spec.seed,
        CampaignPhase.P0,
        RuleStream.REGRESSION,
        case_id,
        graph_nodes,
        0,
    )
    return RulePlanEntry(
        entry_id=f"p0:neo4j:regression:{case_id}",
        phase=CampaignPhase.P0,
        target=TargetSystem.NEO4J,
        rule_id=rule.rule_id,
        family=rule.family.value,
        tier=rule.tier.value,
        graph_scale=graph_nodes,
        shard_index=0,
        shard_seed=seed,
        accepted_pair_quota=0,
        max_generated_seed_budget=0,
        max_variants_per_seed=0,
        proof_version=rule.proof_version,
        query_timeout_seconds=spec.systems[TargetSystem.NEO4J].default_timeout_seconds,
        max_entry_runtime_seconds=max_entry_runtime_seconds,
        measurement_protocol_id="known-regression-gate-v1",
        stream=RuleStream.REGRESSION,
        regression_case_id=case_id,
    )


def _validated_selected_cells(
    spec: ValidatedYieldExperimentSpec,
    selected_cells: tuple[tuple[str, int], ...],
    registry: RuleRegistry,
) -> tuple[tuple[str, int], ...]:
    maximum = spec.campaign.p2.max_rule_scale_cells
    if maximum is None:
        raise ConfigError("campaign.p2.max_rule_scale_cells is required")
    if not selected_cells:
        raise ConfigError("P2 selected_cells must be explicit and non-empty")
    if len(selected_cells) > MAX_P2_RULE_SCALE_CELLS:
        raise ConfigError(
            f"P2 selected_cells must contain at most {MAX_P2_RULE_SCALE_CELLS} cells"
        )
    if maximum > MAX_P2_RULE_SCALE_CELLS:
        raise ConfigError(
            "campaign.p2.max_rule_scale_cells exceeds the planner hard limit "
            f"{MAX_P2_RULE_SCALE_CELLS}"
        )
    if len(selected_cells) > maximum:
        raise ConfigError(f"P2 selected_cells must contain at most {maximum} cells")
    normalized: list[tuple[str, int]] = []
    for index, cell in enumerate(selected_cells):
        if not isinstance(cell, tuple) or len(cell) != 2:
            raise ConfigError(f"selected_cells[{index}] must be a (rule_id, graph_scale) tuple")
        rule_id, graph_scale = cell
        if not isinstance(rule_id, str):
            raise ConfigError(f"selected_cells[{index}].rule_id must be a string")
        try:
            canonical_rule_id = registry.resolve_id(rule_id)
        except KeyError as exc:
            raise ConfigError(f"selected_cells[{index}] contains unknown rule: {rule_id}") from exc
        if canonical_rule_id not in spec.rules.discovery_rule_ids:
            raise ConfigError(
                f"selected_cells[{index}] is not an approved discovery rule: {canonical_rule_id}"
            )
        if (
            not isinstance(graph_scale, int)
            or isinstance(graph_scale, bool)
            or graph_scale not in spec.matrix.graph_scales
        ):
            raise ConfigError(
                f"selected_cells[{index}] graph scale is not in matrix.graph_scales: {graph_scale}"
            )
        normalized.append((canonical_rule_id, graph_scale))
    if len(set(normalized)) != len(normalized):
        raise ConfigError("P2 selected_cells must not contain duplicate canonical cells")
    return tuple(normalized)


def _expand_rule_cells(
    spec: ValidatedYieldExperimentSpec,
    phase: CampaignPhase,
    target: TargetSystem,
    cells: tuple[tuple[str, int], ...],
    active_rules: Mapping[str, Any],
    stream: RuleStream,
    accepted_pair_quota: int,
    shards: int,
    max_entry_runtime_seconds: int,
) -> tuple[RulePlanEntry, ...]:
    entries: list[RulePlanEntry] = []
    for rule_id, graph_scale in cells:
        if rule_id not in active_rules:
            raise ConfigError(
                f"configured {stream.value} rule is not active for {target.value}: {rule_id}"
            )
        rule = active_rules[rule_id]
        for shard_index in range(shards):
            shard_seed = _entry_seed(
                spec.seed,
                phase,
                stream,
                rule.rule_id,
                graph_scale,
                shard_index,
            )
            entries.append(
                RulePlanEntry(
                    entry_id=(
                        f"{phase.value}:{target.value}:{stream.value}:"
                        f"{rule.rule_id}:{graph_scale}:{shard_index}"
                    ),
                    phase=phase,
                    target=target,
                    rule_id=rule.rule_id,
                    family=rule.family.value,
                    tier=rule.tier.value,
                    graph_scale=graph_scale,
                    shard_index=shard_index,
                    shard_seed=shard_seed,
                    accepted_pair_quota=accepted_pair_quota,
                    max_generated_seed_budget=(
                        accepted_pair_quota * spec.campaign.generated_seed_multiplier
                    ),
                    max_variants_per_seed=MAX_VARIANTS_PER_SEED,
                    proof_version=rule.proof_version,
                    query_timeout_seconds=spec.systems[target].default_timeout_seconds,
                    max_entry_runtime_seconds=max_entry_runtime_seconds,
                    measurement_protocol_id=MEASUREMENT_PROTOCOL_ID,
                    stream=stream,
                )
            )
    return tuple(entries)


def _entry_seed(
    base: int,
    phase: CampaignPhase,
    stream: RuleStream,
    rule_id: str,
    scale: int,
    shard: int,
) -> int:
    payload = f"{base}:{phase.value}:{stream.value}:{rule_id}:{scale}:{shard}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1)


def _rule_plan_source_revision(
    spec: ValidatedYieldExperimentSpec,
    *,
    environment: Mapping[str, str] | None,
) -> SourceRevision:
    repository_root = spec.source_path.parents[1]
    gdsmith_commit = _git_output(spec.generation.checkout, "rev-parse", "HEAD")
    if gdsmith_commit != spec.generation.expected_commit:
        raise ConfigError(
            "GDsmith checkout does not match generation.expected_commit: "
            f"expected {spec.generation.expected_commit}, got {gdsmith_commit or 'unavailable'}"
        )
    tooling = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit=spec.generation.expected_commit),
        os.environ if environment is None else environment,
    )
    if (
        not tooling.build_manifest_ok
        or not tooling.jar_sha256
        or not tooling.build_manifest_sha256
    ):
        raise ConfigError(
            "schema-v3 RulePlan requires a verified GDsmith artifact and build manifest"
        )
    revision = SourceRevision(
        repository_commit=_git_output(repository_root, "rev-parse", "HEAD"),
        repository_dirty=_git_dirty(repository_root),
        gdsmith_commit=gdsmith_commit,
        gdsmith_dirty=_git_dirty(spec.generation.checkout),
        rules_schema_sha256=spec.rules_schema_sha256,
        gdsmith_jar_sha256=tooling.jar_sha256,
        gdsmith_build_manifest_sha256=tooling.build_manifest_sha256,
    )
    if not revision.provenance_complete:
        raise ConfigError("schema-v3 RulePlan source provenance is incomplete")
    return revision


def build_run_plan(
    spec: ExperimentSpec,
    *,
    repository_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> RunPlan:
    if not isinstance(spec, ExperimentSpec):
        raise ConfigError("schema-v2 ExperimentSpec is required for RunPlan")
    root = Path(repository_root) if repository_root is not None else spec.source_path.parents[1]
    if spec.total_accepted_pair_budget > MAX_TOTAL_ACCEPTED_PAIR_BUDGET:
        raise ConfigError(
            "total accepted-pair budget exceeds formal limit: "
            f"{spec.total_accepted_pair_budget} > {MAX_TOTAL_ACCEPTED_PAIR_BUDGET}"
        )
    exploration_floor = int(spec.rules_catalog["tiers"]["core"]["minimum_accepted_pairs_per_rule"])
    registry = RuleRegistry.default()
    for target in spec.targets:
        core_count = len(
            registry.rules_for(target.value, SafetyTier.CORE, frozenset(spec.systems[target].capabilities))
        )
        required = core_count * exploration_floor
        if required > spec.workload.accepted_query_pairs_per_shard:
            raise ConfigError(
                f"CORE exploration floor does not fit {target.value} shard budget: "
                f"{core_count} rules * {exploration_floor} = {required} > "
                f"{spec.workload.accepted_query_pairs_per_shard}"
            )
    tasks = tuple(
        ShardTask(
            target=target,
            shard_index=shard_index,
            seed=_shard_seed(spec.seed, target.value, shard_index),
            accepted_query_pairs=spec.workload.accepted_query_pairs_per_shard,
            max_generated_seeds=spec.workload.max_generated_seeds_per_shard,
            max_runtime_seconds=spec.workload.max_shard_runtime_hours * 3600,
            generation_backend=spec.systems[target].generation_backend,
        )
        for target in spec.targets
        for shard_index in range(spec.matrix.shards_per_system)
    )
    if len({task.seed for task in tasks}) != len(tasks):
        raise ConfigError("deterministic shard seed collision")
    env = os.environ if environment is None else environment
    gdsmith_tooling = check_gdsmith_tooling(
        GdsmithConfig(expected_source_commit=spec.generation.expected_commit),
        env,
    )
    environment_names = {
        *spec.generation.required_env,
        *(name for definition in spec.systems.values() for name in definition.required_env),
        *(name for definition in spec.systems.values() for name in definition.generation_required_env),
    }
    for target in spec.targets:
        prefix = f"GRAPHSYMPERF_{target.value.upper()}"
        if target is TargetSystem.LADYBUGDB:
            environment_names.add(f"{prefix}_PACKAGE_DIGEST")
        else:
            environment_names.update({f"{prefix}_CONTAINER", f"{prefix}_IMAGE_DIGEST"})
    return RunPlan(
        spec_name=spec.name,
        schema_version=spec.schema_version,
        tasks=tasks,
        total_accepted_pair_budget=spec.total_accepted_pair_budget,
        source_revision=SourceRevision(
            repository_commit=_git_output(root, "rev-parse", "HEAD"),
            gdsmith_commit=_git_output(spec.generation.checkout, "rev-parse", "HEAD"),
            gdsmith_dirty=_git_dirty(spec.generation.checkout),
            rules_schema_sha256=spec.rules_schema_sha256,
            repository_dirty=_git_dirty(root),
            gdsmith_jar_sha256=gdsmith_tooling.jar_sha256,
            gdsmith_build_manifest_sha256=gdsmith_tooling.build_manifest_sha256,
        ),
        resolved_spec=spec,
        environment=tuple(
            EnvironmentVariableState(name, "present" if env.get(name) else "missing")
            for name in sorted(environment_names)
        ),
        rule_yield_snapshot=freeze_value(
            {
                target.value: {
                    rule.rule_id: {"accepted": 0, "validation_passed": 0}
                    for rule in registry.rules_for(
                        target.value,
                        SafetyTier.CORE,
                        frozenset(spec.systems[target].capabilities),
                    )
                }
                for target in spec.targets
            }
        ),
    )


def write_resolved_manifest(plan: RunPlan | RulePlan, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = resolved_manifest_payload(plan)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def resolved_manifest_payload(plan: RunPlan | RulePlan) -> dict[str, Any]:
    payload = to_jsonable(plan)
    if isinstance(plan, RunPlan):
        payload["environment"] = {state.name: state.status for state in plan.environment}
        if plan.resolved_spec is not None:
            payload["resolved_spec"]["systems"] = {
                target.value: to_jsonable(definition)
                for target, definition in plan.resolved_spec.systems.items()
            }
    return payload


def resolved_manifest_hash(plan: RunPlan | RulePlan) -> str:
    canonical = json.dumps(
        resolved_manifest_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=repr,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _shard_seed(experiment_seed: int, target: str, shard_index: int) -> int:
    digest = hashlib.sha256(f"{experiment_seed}:{target}:{shard_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) & ((1 << 63) - 1)


def _git_output(directory: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(directory), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else None


def _git_dirty(directory: Path) -> bool | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(directory), "status", "--porcelain"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())
