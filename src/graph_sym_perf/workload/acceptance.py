from __future__ import annotations

from collections.abc import Callable

from graph_sym_perf.workload.models import AcceptedPairCorpus, AcceptedPairRequest


class AcceptedPairBuilder:
    def __init__(self, provider: Callable[[int], tuple[str, str] | None]) -> None:
        self.provider = provider

    def build(self, request: AcceptedPairRequest) -> AcceptedPairCorpus:
        pairs: list[tuple[str, str]] = []
        rejected: list[str] = []
        for seed in range(1, request.max_generated_seeds + 1):
            pair = self.provider(seed)
            if pair is None:
                rejected.append("not_accepted")
                continue
            pairs.append(pair)
            if len(pairs) == request.accepted_pairs:
                return AcceptedPairCorpus(tuple(pairs), seed, tuple(rejected))
        return AcceptedPairCorpus(tuple(pairs), request.max_generated_seeds, tuple(rejected))
