"""Cross-process scheduling context passed to one immutable shard attempt."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from graph_sym_perf.experiment.process_control import FileLeasePool
from graph_sym_perf.experiment.state import ShardJournal, ShardState


@dataclass(frozen=True)
class AttemptContext:
    attempt: int
    attempt_dir: Path
    shard_dir: Path
    manifest_hash: str
    target_key: str
    accepted_pair_hashes: frozenset[str]
    candidate_hashes: frozenset[str]
    lease_root: Path
    max_parallel_generation_systems: int
    max_parallel_measurement_systems: int
    max_parallel_shards_per_system: int

    def transition(self, state: ShardState) -> None:
        journal = ShardJournal(self.shard_dir, manifest_hash=self.manifest_hash)
        if journal.state is state:
            return
        journal.transition(state, attempt=self.attempt)

    @contextlib.contextmanager
    def generation_lease(self) -> Iterator[None]:
        system = FileLeasePool(
            self.lease_root,
            f"generation-system-{self.target_key}",
            self.max_parallel_shards_per_system,
        )
        global_pool = FileLeasePool(
            self.lease_root,
            "generation-global",
            self.max_parallel_generation_systems,
        )
        with system.acquire(), global_pool.acquire():
            yield

    @contextlib.contextmanager
    def measurement_lease(self) -> Iterator[None]:
        system = FileLeasePool(
            self.lease_root,
            f"measurement-system-{self.target_key}",
            self.max_parallel_shards_per_system,
        )
        global_pool = FileLeasePool(
            self.lease_root,
            "measurement-global",
            self.max_parallel_measurement_systems,
        )
        with system.acquire(), global_pool.acquire():
            yield


__all__ = ["AttemptContext"]
