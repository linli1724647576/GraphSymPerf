from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class YieldSnapshot:
    counts: Mapping[str, tuple[int, int]]

    def validated_yield(self, rule_id: str) -> float:
        accepted, passed = self.counts.get(rule_id, (0, 0))
        return (passed + 1) / (accepted + 2)
