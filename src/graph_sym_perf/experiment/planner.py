"""Deterministic, DBMS-free planning for schema-v2 experiments."""

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
    EnvironmentVariableState,
    ExperimentSpec,
    RunPlan,
    ShardTask,
    SourceRevision,
    freeze_value,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


MAX_TOTAL_ACCEPTED_PAIR_BUDGET = 112_000


def build_run_plan(
    spec: ExperimentSpec,
    *,
    repository_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> RunPlan:
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


def write_resolved_manifest(plan: RunPlan, path: str | Path) -> Path:
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


def resolved_manifest_payload(plan: RunPlan) -> dict[str, Any]:
    payload = to_jsonable(plan)
    payload["environment"] = {state.name: state.status for state in plan.environment}
    if plan.resolved_spec is not None:
        payload["resolved_spec"]["systems"] = {
            target.value: to_jsonable(definition)
            for target, definition in plan.resolved_spec.systems.items()
        }
    return payload


def resolved_manifest_hash(plan: RunPlan) -> str:
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
