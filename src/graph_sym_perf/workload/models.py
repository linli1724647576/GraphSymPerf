from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AcceptedPairRequest:
    accepted_pairs: int
    max_generated_seeds: int


@dataclass(frozen=True)
class AcceptedPairCorpus:
    pairs: tuple[tuple[str, str], ...]
    generated_seeds: int
    reject_reasons: tuple[str, ...]

    @property
    def accepted_count(self) -> int:
        return len(self.pairs)
