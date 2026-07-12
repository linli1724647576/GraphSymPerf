from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from graph_sym_perf.generation.corpus import PortableQuery
from graph_sym_perf.sampling.adaptive import AdaptiveRuleSampler
from graph_sym_perf.sampling.yield_store import RuleQuarantineStore, ValidatedYieldStore, YieldSnapshot
from graph_sym_perf.transforms.engine import RuleNotApplicable, apply_certified_rule
from graph_sym_perf.transforms.rule_models import RuleRegistry, RuleSpec, SafetyTier
from graph_sym_perf.transforms.safety_points import enumerate_safety_points
from graph_sym_perf.workload.models import PairProposal, SeedContext


class CoreRulePairProvider:
    """Select and apply certificate-bound CORE rules for one shard."""

    def __init__(
        self,
        queries: tuple[str, ...],
        *,
        dbms: str,
        shard_index: int,
        capability_hash: str,
        exploration_floor: int,
        epoch_size: int,
        quarantine_path: str | Path,
        yield_snapshot: YieldSnapshot | None = None,
        registry: RuleRegistry | None = None,
        capabilities: frozenset[str] | None = None,
        validated_yield_path: str | Path | None = None,
    ) -> None:
        self.queries = queries
        self.dbms = dbms
        self.shard_index = shard_index
        self.capability_hash = capability_hash
        self.exploration_floor = exploration_floor
        self.epoch_size = epoch_size
        self.registry = registry or RuleRegistry.default()
        self.rules = self.registry.rules_for(dbms, SafetyTier.CORE, capabilities)
        if not self.rules:
            raise ValueError(f"no applicable CORE rules for {dbms}")
        self.quarantine_store = RuleQuarantineStore(quarantine_path)
        self._quarantined = {
            rule.rule_id
            for rule in self.rules
            if self.quarantine_store.is_quarantined(dbms, rule.rule_id, rule.version)
        }
        self._accepted: Counter[str] = Counter()
        self._not_applicable: Counter[str] = Counter()
        if yield_snapshot is not None and validated_yield_path is not None:
            raise ValueError("provide either a yield snapshot or a validated-yield path")
        self._yield_store = ValidatedYieldStore(validated_yield_path) if validated_yield_path is not None else None
        self._yield_snapshot = (
            yield_snapshot
            or (
                self._yield_store.snapshot(tuple(rule.rule_id for rule in self.rules))
                if self._yield_store is not None
                else YieldSnapshot({rule.rule_id: (0, 0) for rule in self.rules})
            )
        )

    @property
    def active_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.rules if rule.rule_id not in self._quarantined)

    @property
    def accepted_counts(self) -> dict[str, int]:
        return dict(sorted(self._accepted.items()))

    @property
    def not_applicable_counts(self) -> dict[str, int]:
        return dict(sorted(self._not_applicable.items()))

    @property
    def yield_snapshot_hash(self) -> str:
        return self._yield_snapshot.content_hash

    def __call__(self, context: SeedContext) -> PairProposal | None:
        proposals = self.proposals(context, max_variants=1)
        return proposals[0] if proposals else None

    def proposals(self, context: SeedContext, *, max_variants: int) -> tuple[PairProposal, ...]:
        if context.target != self.dbms or context.shard_index != self.shard_index:
            raise ValueError("seed context does not belong to this rule provider")
        if not isinstance(max_variants, int) or isinstance(max_variants, bool) or max_variants <= 0:
            raise ValueError("max_variants must be a positive integer")
        if context.ordinal > len(self.queries):
            return ()
        active = self.active_rule_ids
        if not active:
            return ()
        snapshot = self._selection_snapshot(active)
        selection = AdaptiveRuleSampler(
            active,
            exploration_floor=self.exploration_floor,
            epoch_size=self.epoch_size,
        ).select(self.dbms, self.shard_index, context.ordinal - 1, snapshot)
        query = self.queries[context.ordinal - 1]
        rule_ids = (selection.rule_id, *(rule_id for rule_id in active if rule_id != selection.rule_id))
        proposals: list[PairProposal] = []
        for rule_id in rule_ids:
            rule = self.registry.get(rule_id)
            proposal = self._proposal_for_rule(query, rule, context)
            if proposal is not None:
                proposals.append(proposal)
                if len(proposals) == max_variants:
                    break
            else:
                self._not_applicable[rule.rule_id] += 1
        return tuple(proposals)

    def _proposal_for_rule(
        self,
        query: str,
        rule: RuleSpec,
        context: SeedContext,
    ) -> PairProposal | None:
        certificates = enumerate_safety_points(query, rule, self.capability_hash)
        if not certificates:
            return None
        offset = context.ordinal % len(certificates)
        for index in range(len(certificates)):
            certificate = certificates[(offset + index) % len(certificates)]
            try:
                transformed = apply_certified_rule(query, rule, certificate)
            except (RuleNotApplicable, ValueError):
                continue
            return PairProposal(
                PortableQuery(query),
                PortableQuery(transformed),
                rule.rule_id,
                certificate,
                context.seed,
            )
        return None

    def record_accepted(self, rule_id: str, pair_hash: str | None = None) -> None:
        if rule_id not in {rule.rule_id for rule in self.rules}:
            raise ValueError(f"unknown CORE rule: {rule_id}")
        self._accepted[rule_id] += 1
        if self._yield_store is not None:
            if pair_hash is None:
                raise ValueError("persisted accepted yield requires the accepted pair hash")
            self._yield_store.record_accepted_pair(rule_id=rule_id, accepted_pair_hash=pair_hash)
        if self._yield_store is not None and sum(self._accepted.values()) % self.epoch_size == 0:
            self._yield_snapshot = self._yield_store.snapshot(tuple(rule.rule_id for rule in self.rules))

    def quarantine_mismatch(self, proposal: PairProposal, *, reason: str) -> None:
        rule = self.registry.get(proposal.rule_id)
        self.quarantine_store.quarantine(
            dbms=self.dbms,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            counterexample={
                "query_a_hash": hashlib.sha256(proposal.query_a.text.encode("utf-8")).hexdigest(),
                "query_b_hash": hashlib.sha256(proposal.query_b.text.encode("utf-8")).hexdigest(),
                "query_a": proposal.query_a.text,
                "query_b": proposal.query_b.text,
                "reason": reason,
                "seed": proposal.seed,
            },
        )
        self._quarantined.add(rule.rule_id)

    def _selection_snapshot(self, active: tuple[str, ...]) -> YieldSnapshot:
        if any(self._accepted[rule_id] < self.exploration_floor for rule_id in active):
            return YieldSnapshot({rule_id: (self._accepted[rule_id], 0) for rule_id in active})
        counts: dict[str, tuple[int, int]] = {}
        for rule_id in active:
            accepted, passed = self._yield_snapshot.counts.get(rule_id, (0, 0))
            accepted = max(accepted, self.exploration_floor)
            counts[rule_id] = (accepted, min(passed, accepted))
        return YieldSnapshot(counts)


__all__ = ["CoreRulePairProvider"]
