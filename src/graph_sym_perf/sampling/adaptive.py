from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from graph_sym_perf.sampling.yield_store import YieldSnapshot


@dataclass(frozen=True)
class RuleSelection:
    rule_id: str


class AdaptiveRuleSampler:
    def __init__(self, rule_ids: tuple[str, ...], *, exploration_floor: int, epoch_size: int) -> None:
        self.rule_ids, self.exploration_floor, self.epoch_size = rule_ids, exploration_floor, epoch_size

    def select(self, dbms: str, shard: int, case_index: int, yield_snapshot: YieldSnapshot) -> RuleSelection:
        if not isinstance(yield_snapshot, YieldSnapshot):
            raise ValueError("sampler accepts YieldSnapshot with accepted and validation-passed counts only")
        for rule_id in self.rule_ids:
            accepted, _ = yield_snapshot.counts.get(rule_id, (0, 0))
            if accepted < self.exploration_floor:
                return RuleSelection(rule_id)
        return RuleSelection(max(self.rule_ids, key=yield_snapshot.validated_yield))
