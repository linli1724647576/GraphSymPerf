"""Run-level PMC state."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from graph_sym_perf.coverage.model import (
    CoverageUnit,
    PMCEvidence,
    PMCSummary,
    filter_context_units,
    filter_core_units,
)

CONTEXT_KIND = "transformation_context"


@dataclass
class PMCCoverageState:
    seen_unit_keys: set[str] = field(default_factory=set)
    unit_counts: Counter[str] = field(default_factory=Counter)
    unit_kind_counts: Counter[str] = field(default_factory=Counter)
    transform_rule_counts: Counter[str] = field(default_factory=Counter)
    transform_family_counts: Counter[str] = field(default_factory=Counter)
    novelty_weights: Mapping[str, float] = field(default_factory=dict)
    new_unit_kind_counts: Counter[str] = field(default_factory=Counter)
    candidate_unit_kind_counts: Counter[str] = field(default_factory=Counter)
    tier_unit_kind_counts: dict[str, Counter[str]] = field(default_factory=dict)
    unit_key_counts: Counter[str] = field(default_factory=Counter)
    unit_registry: dict[str, CoverageUnit] = field(default_factory=dict)

    def observe(self, evidence: PMCEvidence) -> PMCEvidence:
        new_units = tuple(unit for unit in evidence.units if unit.key not in self.seen_unit_keys)
        core_units = filter_core_units(evidence.units)
        core_new_units = filter_core_units(new_units)
        for unit in evidence.units:
            self.seen_unit_keys.add(unit.key)
            self.unit_counts[unit.key] += 1
            self.unit_key_counts[unit.key] += 1
            self.unit_kind_counts[unit.kind] += 1
            self.unit_registry.setdefault(unit.key, unit)
            self.tier_unit_kind_counts.setdefault(evidence.safety_tier, Counter())[unit.kind] += 1
        for unit in core_units:
            self.transform_rule_counts[evidence.transform_rule] += 1
            self.transform_family_counts[evidence.transform_family] += 1
            if bool(evidence.metadata.get("is_candidate")):
                self.candidate_unit_kind_counts[unit.kind] += 1
        for unit in new_units:
            self.new_unit_kind_counts[unit.kind] += 1
        novelty = sum(float(self.novelty_weights.get(unit.kind, unit.weight)) for unit in core_new_units)
        if core_new_units and evidence.structural_hash_changed:
            novelty += max(0.0, float(self.novelty_weights.get("structural", 0.0)) * 0.25)
        return replace(
            evidence,
            new_units=new_units,
            unit_count=len(evidence.units),
            new_unit_count=len(core_new_units),
            novelty_score=round(novelty, 4),
        )

    def snapshot(self, run_id: str) -> PMCSummary:
        core_keys = {unit.key for unit in filter_core_units(self.unit_registry.values())}
        context_keys = {unit.key for unit in filter_context_units(self.unit_registry.values())}
        core_unit_key_counts = Counter({key: count for key, count in self.unit_key_counts.items() if key in core_keys})
        context_unit_key_counts = Counter({key: count for key, count in self.unit_key_counts.items() if key in context_keys})
        top_units = tuple(
            {
                "key": key,
                "kind": self.unit_registry[key].kind,
                "label": self.unit_registry[key].label,
                "count": count,
            }
            for key, count in sorted(core_unit_key_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        )
        top_context_units = tuple(
            {
                "key": key,
                "kind": self.unit_registry[key].kind,
                "label": self.unit_registry[key].label,
                "count": count,
            }
            for key, count in sorted(context_unit_key_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        )
        core_kind_counts = Counter(
            {kind: count for kind, count in self.unit_kind_counts.items() if kind != CONTEXT_KIND}
        )
        core_new_kind_counts = Counter(
            {kind: count for kind, count in self.new_unit_kind_counts.items() if kind != CONTEXT_KIND}
        )
        context_new_kind_counts = Counter(
            {kind: count for kind, count in self.new_unit_kind_counts.items() if kind == CONTEXT_KIND}
        )
        metadata: dict[str, Any] = {
            "distinct_unit_keys": len(self.seen_unit_keys),
            "distinct_core_unit_keys": len(core_unit_key_counts),
            "distinct_context_unit_keys": len(context_unit_key_counts),
            "tier_units_by_kind": {
                tier: dict(sorted(counter.items()))
                for tier, counter in sorted(self.tier_unit_kind_counts.items())
            },
            "all_units_by_kind": dict(sorted(self.unit_kind_counts.items())),
            "all_new_units_by_kind": dict(sorted(self.new_unit_kind_counts.items())),
        }
        total_all = sum(self.unit_counts.values())
        total_new_all = sum(self.new_unit_kind_counts.values())
        total_core = sum(core_kind_counts.values())
        total_new_core = sum(core_new_kind_counts.values())
        return PMCSummary(
            run_id=run_id,
            total_units_seen=total_core,
            total_new_units=total_new_core,
            units_by_kind=dict(sorted(core_kind_counts.items())),
            new_units_by_kind=dict(sorted(core_new_kind_counts.items())),
            units_by_transform_family=dict(sorted(self.transform_family_counts.items())),
            units_by_transform_rule=dict(sorted(self.transform_rule_counts.items())),
            candidate_units_by_kind=dict(sorted(self.candidate_unit_kind_counts.items())),
            top_units=top_units,
            total_units_seen_all=total_all,
            total_units_seen_core=total_core,
            total_new_units_all=total_new_all,
            total_new_units_core=total_new_core,
            core_units=dict(sorted(core_kind_counts.items())),
            core_new_units=dict(sorted(core_new_kind_counts.items())),
            context_units=dict(sorted({CONTEXT_KIND: self.unit_kind_counts.get(CONTEXT_KIND, 0)}.items())),
            context_new_units=dict(sorted(context_new_kind_counts.items())),
            top_context_units=top_context_units,
            metadata=metadata,
        )


def replay_pmc_evidence(
    run_id: str,
    evidences: Iterable[PMCEvidence],
    novelty_weights: Mapping[str, float] | None = None,
) -> tuple[PMCSummary, tuple[PMCEvidence, ...]]:
    state = PMCCoverageState(novelty_weights=novelty_weights or {})
    observed: list[PMCEvidence] = []
    for evidence in evidences:
        observed.append(state.observe(evidence))
    return state.snapshot(run_id), tuple(observed)


def summarize_pmc_evidence(
    run_id: str,
    evidences: Iterable[PMCEvidence],
    novelty_weights: Mapping[str, float] | None = None,
) -> PMCSummary:
    summary, _ = replay_pmc_evidence(run_id, evidences, novelty_weights)
    return summary
