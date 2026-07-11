"""Immutable contracts for schema-v2 experiment planning."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.core import TargetSystem


class FrozenMapping(Mapping[Any, Any]):
    """Small recursively immutable mapping used by resolved configuration."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[Any, Any]) -> None:
        object.__setattr__(self, "_items", tuple((key, freeze_value(value)) for key, value in values.items()))

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("FrozenMapping is immutable")

    def __getitem__(self, key: Any) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[Any]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenMapping({dict(self._items)!r})"

    def __reduce__(self):
        """Reconstruct through the public constructor under multiprocessing spawn."""

        return (type(self), (dict(self._items),))


def freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, list | tuple):
        return tuple(freeze_value(item) for item in value)
    return value


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
    expected_commit: str
    checkout: Path
    required_env: tuple[str, ...]


@dataclass(frozen=True)
class SystemDefinition:
    target: TargetSystem
    required_env: tuple[str, ...]
    generation_required_env: tuple[str, ...]
    generation_backend: str
    capabilities: tuple[str, ...]
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
    rules_catalog: Mapping[str, object]
    rules_schema_sha256: str
    rules_source_path: Path
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
    generation_backend: str = "unspecified"


@dataclass(frozen=True)
class SourceRevision:
    repository_commit: str | None
    gdsmith_commit: str | None
    gdsmith_dirty: bool | None
    rules_schema_sha256: str | None = None
    repository_dirty: bool | None = None
    gdsmith_jar_sha256: str | None = None
    gdsmith_build_manifest_sha256: str | None = None

    @property
    def provenance_complete(self) -> bool:
        return (
            bool(self.repository_commit)
            and self.repository_dirty is not None
            and bool(self.gdsmith_commit)
            and self.gdsmith_dirty is not None
            and bool(self.rules_schema_sha256)
            and bool(self.gdsmith_jar_sha256)
            and bool(self.gdsmith_build_manifest_sha256)
        )


@dataclass(frozen=True)
class EnvironmentVariableState:
    name: str
    status: str


@dataclass(frozen=True)
class RunPlan:
    spec_name: str
    schema_version: int
    tasks: tuple[ShardTask, ...]
    total_accepted_pair_budget: int
    source_revision: SourceRevision
    validator_protocol_version: str = "runtime-first-v3"
    reducer_protocol_version: str = "paired-ast-ddmin-v1"
    resolved_spec: ExperimentSpec | None = None
    environment: tuple[EnvironmentVariableState, ...] = ()
    rule_yield_snapshot: Mapping[str, object] = field(default_factory=lambda: FrozenMapping({}))
