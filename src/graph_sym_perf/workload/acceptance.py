from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import Protocol

from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPair,
    AcceptedPairCorpus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
    SeedContext,
)


class RulePairProvider(Protocol):
    def __call__(self, context: SeedContext) -> PairProposal | None:
        ...


class PairAcceptanceValidator(Protocol):
    def __call__(self, proposal: PairProposal) -> PairValidation:
        ...


class AcceptedPairBuilder:
    """Top up until the exact accepted budget or a formal bound is exhausted."""

    def __init__(
        self,
        provider: RulePairProvider,
        validator: PairAcceptanceValidator,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.validator = validator
        self.clock = clock

    def build(self, request: AcceptedPairRequest) -> AcceptedPairCorpus:
        accepted: list[AcceptedPair] = []
        rejects: Counter[str] = Counter()
        seen_ast = set(request.cross_shard_ast_hashes)
        seen_pairs = set(request.cross_shard_pair_hashes)
        attempted_sources: Counter[str] = Counter()
        attempted_transformed: set[str] = set()
        attempted_pairs: set[str] = set()
        started = self.clock()
        generated = 0
        stop_reason = "MAX_GENERATED_SEEDS_EXHAUSTED"

        for ordinal in range(1, request.max_generated_seeds + 1):
            if self.clock() - started >= request.max_seconds:
                stop_reason = "TIME_BUDGET_EXHAUSTED"
                break
            generated = ordinal
            context = request.seed_context(ordinal)
            many = getattr(self.provider, "proposals", None)
            if callable(many):
                proposals = tuple(many(context, max_variants=request.max_variants_per_seed))
            else:
                proposal = self.provider(context)
                proposals = () if proposal is None else (proposal,)
            if not proposals:
                rejects["RULE_NOT_APPLICABLE"] += 1
                continue
            if len(proposals) > request.max_variants_per_seed:
                raise ValueError("rule provider exceeded max_variants_per_seed")
            for proposal in proposals:
                source_hash = proposal.query_a.ast_hash
                transformed_hash = proposal.query_b.ast_hash
                if proposal.safety_certificate is None:
                    rejects["SAFETY_CERTIFICATE_MISSING"] += 1
                    continue
                if proposal.ast_hashes & seen_ast:
                    rejects["CROSS_SHARD_DUPLICATE_AST"] += 1
                    continue
                if proposal.pair_hash in seen_pairs:
                    rejects["CROSS_SHARD_DUPLICATE_PAIR"] += 1
                    continue
                if (
                    transformed_hash in attempted_transformed
                    or transformed_hash in attempted_sources
                    or source_hash in attempted_transformed
                ):
                    rejects["DUPLICATE_AST"] += 1
                    continue
                if attempted_sources[source_hash] >= request.max_variants_per_seed:
                    rejects["MAX_VARIANTS_PER_SEED_REACHED"] += 1
                    continue
                if proposal.pair_hash in attempted_pairs:
                    rejects["DUPLICATE_PAIR"] += 1
                    continue
                # Record the attempt before validation so deterministic rejected
                # proposals cannot consume the seed/time budget repeatedly.
                attempted_sources[source_hash] += 1
                attempted_transformed.add(transformed_hash)
                attempted_pairs.add(proposal.pair_hash)
                validation = self.validator(proposal)
                if not validation.accepted:
                    rejects[validation.reason_code] += 1
                    continue
                accepted.append(AcceptedPair(proposal, validation))
                if len(accepted) == request.accepted_pairs:
                    stop_reason = "ACCEPTED_TARGET_REACHED"
                    break
            if len(accepted) == request.accepted_pairs:
                break

        status = AcceptanceStatus.COMPLETE if len(accepted) == request.accepted_pairs else AcceptanceStatus.INCOMPLETE
        if status is AcceptanceStatus.INCOMPLETE and self.clock() - started >= request.max_seconds:
            stop_reason = "TIME_BUDGET_EXHAUSTED"
        return AcceptedPairCorpus(tuple(accepted), generated, dict(sorted(rejects.items())), status, stop_reason)


__all__ = ["AcceptedPairBuilder", "PairAcceptanceValidator", "RulePairProvider"]
