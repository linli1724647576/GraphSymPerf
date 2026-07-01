"""Shard selection helpers."""

from __future__ import annotations

from dataclasses import dataclass

from graph_sym_perf.core import QueryCase, TargetSystem


@dataclass(frozen=True)
class ShardSpec:
    index: int
    total: int

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError("shard total must be greater than 0")
        if self.index < 0 or self.index >= self.total:
            raise ValueError("shard index out of range")

    @property
    def run_id(self) -> str:
        return f"shard_{self.index:03d}"

    def selects(self, global_index: int) -> bool:
        return global_index % self.total == self.index

    def slice_cases(self, cases: tuple[QueryCase, ...]) -> tuple[tuple[int, QueryCase], ...]:
        selected: list[tuple[int, QueryCase]] = []
        local_index = 0
        for global_index, case in enumerate(cases):
            if not self.selects(global_index):
                continue
            selected.append((local_index, case))
            local_index += 1
        return tuple(selected)


def stable_case_id(
    experiment_id: str,
    system: TargetSystem | str,
    shard: ShardSpec,
    local_index: int,
    seed: int,
) -> str:
    system_name = system.value if isinstance(system, TargetSystem) else str(system)
    return (
        f"{experiment_id}__{system_name}"
        f"__shard{shard.index:03d}"
        f"__case{local_index:05d}"
        f"__seed{seed}"
    )
