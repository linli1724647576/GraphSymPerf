from __future__ import annotations

import hashlib
from dataclasses import dataclass

from graph_sym_perf.sampling.yield_store import YieldSnapshot


@dataclass(frozen=True)
class RuleSelection:
    rule_id: str
    phase: str
    epoch: int
    snapshot_hash: str


class AdaptiveRuleSampler:
    def __init__(self, rule_ids: tuple[str, ...], *, exploration_floor: int, epoch_size: int) -> None:
        if not rule_ids or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("sampler requires unique rule ids")
        if exploration_floor <= 0 or epoch_size <= 0:
            raise ValueError("exploration floor and epoch size must be positive")
        self.rule_ids = rule_ids
        self.exploration_floor = exploration_floor
        self.epoch_size = epoch_size

    def select(
        self,
        dbms: str,
        shard: int,
        case_index: int,
        yield_snapshot: YieldSnapshot,
    ) -> RuleSelection:
        if not isinstance(yield_snapshot, YieldSnapshot):
            raise ValueError("sampler accepts only the versioned accepted/validation-passed snapshot")
        if not dbms or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (shard, case_index)):
            raise ValueError("sampler identity must be non-negative and DBMS-bound")
        epoch = yield_snapshot.total_accepted // self.epoch_size
        under_floor = [
            rule_id
            for rule_id in self.rule_ids
            if yield_snapshot.counts.get(rule_id, (0, 0))[0] < self.exploration_floor
        ]
        if under_floor:
            minimum = min(yield_snapshot.counts.get(rule_id, (0, 0))[0] for rule_id in under_floor)
            choices = [
                rule_id
                for rule_id in under_floor
                if yield_snapshot.counts.get(rule_id, (0, 0))[0] == minimum
            ]
            selected = choices[case_index % len(choices)]
            return RuleSelection(selected, "exploration_floor", epoch, yield_snapshot.content_hash)

        weights = tuple(yield_snapshot.validated_yield(rule_id) for rule_id in self.rule_ids)
        selected = _deterministic_weighted_choice(
            self.rule_ids,
            weights,
            f"{dbms}:{shard}:{epoch}:{case_index}:{yield_snapshot.content_hash}",
        )
        return RuleSelection(selected, "validated_runtime_yield", epoch, yield_snapshot.content_hash)


def _deterministic_weighted_choice(
    rule_ids: tuple[str, ...],
    weights: tuple[float, ...],
    seed_material: str,
) -> str:
    total = sum(weights)
    if total <= 0:
        raise ValueError("sampler weights must have positive mass")
    raw = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    point = (raw / (2**64 - 1)) * total
    cumulative = 0.0
    for rule_id, weight in zip(rule_ids, weights, strict=True):
        cumulative += weight
        if point <= cumulative:
            return rule_id
    return rule_ids[-1]


__all__ = ["AdaptiveRuleSampler", "RuleSelection"]
