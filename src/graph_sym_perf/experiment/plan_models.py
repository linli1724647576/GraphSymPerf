"""Immutable contracts for schema-v2 experiment planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from graph_sym_perf.core import TargetSystem


@dataclass(frozen=True)
class MatrixSpec:
    shards_per_system: int


@dataclass(frozen=True)
class WorkloadSpec:
    accepted_query_pairs_per_shard: int
    max_generated_seeds_per_shard: int
    max_shard_runtime_hours: int
    deduplicate_by_ast_hash: bool
    max_variants_per_seed: int


@dataclass(frozen=True)
class GenerationSpec:
    backend: str
    commit_source: str
    checkout: Path
    algorithm: str
    state_model: str
    max_node_num: int
    label_num: int
    bootstrap_queries: int


@dataclass(frozen=True)
class SystemDefinition:
    target: TargetSystem
    required_env: tuple[str, ...]
    generation_required_env: tuple[str, ...]
    dialect: str
    graph_isolation: str
    default_timeout_seconds: int


@dataclass(frozen=True)
class ExperimentSpec:
    schema_version: int
    name: str
    seed: int
    feedback_observation_days: int
    targets: tuple[TargetSystem, ...]
    matrix: MatrixSpec
    workload: WorkloadSpec
    generation: GenerationSpec
    scheduling: Mapping[str, object]
    runtime: Mapping[str, object]
    validation: Mapping[str, object]
    reducer: Mapping[str, object]
    rules: Mapping[str, object]
    artifacts: Mapping[str, object]
    systems: Mapping[TargetSystem, SystemDefinition]
    source_path: Path

    @property
    def total_accepted_pair_budget(self) -> int:
        return len(self.targets) * self.matrix.shards_per_system * self.workload.accepted_query_pairs_per_shard


@dataclass(frozen=True)
class ShardTask:
    target: TargetSystem
    shard_index: int
    seed: int
    accepted_query_pairs: int
    max_generated_seeds: int
    max_runtime_seconds: int


@dataclass(frozen=True)
class SourceRevision:
    repository_commit: str | None
    gdsmith_commit: str | None
    gdsmith_dirty: bool | None


@dataclass(frozen=True)
class RunPlan:
    spec_name: str
    schema_version: int
    tasks: tuple[ShardTask, ...]
    total_accepted_pair_budget: int
    source_revision: SourceRevision
    validator_protocol_version: str = "runtime-first-v3"
    reducer_protocol_version: str = "paired-ast-ddmin-v1"
