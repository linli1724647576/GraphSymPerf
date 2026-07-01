"""Run-level PMC state."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from graph_sym_perf.coverage.model import CoverageUnit, PMCEvidence, PMCSummary


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
        for unit in evidence.units:
            self.seen_unit_keys.add(unit.key)
            self.unit_counts[unit.key] += 1
            self.unit_key_counts[unit.key] += 1
            self.unit_kind_counts[unit.kind] += 1
            self.transform_rule_counts[evidence.transform_rule] += 1
            self.transform_family_counts[evidence.transform_family] += 1
            self.unit_registry.setdefault(unit.key, unit)
            self.tier_unit_kind_counts.setdefault(evidence.safety_tier, Counter())[unit.kind] += 1
            if bool(evidence.metadata.get("is_candidate")):
                self.candidate_unit_kind_counts[unit.kind] += 1
        for unit in new_units:
            self.new_unit_kind_counts[unit.kind] += 1
        novelty = sum(float(self.novelty_weights.get(unit.kind, unit.weight)) for unit in new_units)
        if new_units and evidence.structural_hash_changed:
            novelty += max(0.0, float(self.novelty_weights.get("structural", 0.0)) * 0.25)
        return replace(
            evidence,
            new_units=new_units,
            unit_count=len(evidence.units),
            new_unit_count=len(new_units),
            novelty_score=round(novelty, 4),
        )

    def snapshot(self, run_id: str) -> PMCSummary:
        top_units = tuple(
            {
                "key": key,
                "kind": self.unit_registry[key].kind,
                "label": self.unit_registry[key].label,
                "count": count,
            }
            for key, count in sorted(self.unit_key_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        )
        metadata: dict[str, Any] = {
            "distinct_unit_keys": len(self.seen_unit_keys),
            "tier_units_by_kind": {
                tier: dict(sorted(counter.items()))
                for tier, counter in sorted(self.tier_unit_kind_counts.items())
            },
        }
        return PMCSummary(
            run_id=run_id,
            total_units_seen=sum(self.unit_counts.values()),
            total_new_units=sum(self.new_unit_kind_counts.values()),
            units_by_kind=dict(sorted(self.unit_kind_counts.items())),
            new_units_by_kind=dict(sorted(self.new_unit_kind_counts.items())),
            units_by_transform_family=dict(sorted(self.transform_family_counts.items())),
            units_by_transform_rule=dict(sorted(self.transform_rule_counts.items())),
            candidate_units_by_kind=dict(sorted(self.candidate_unit_kind_counts.items())),
            top_units=top_units,
            metadata=metadata,
        )
