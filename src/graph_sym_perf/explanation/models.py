from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from graph_sym_perf.core import NormalizedPlan
from graph_sym_perf.validation.models import PipelineStage


@dataclass(frozen=True)
class ExplanationEvidence:
    issue_id: str
    issue_stage: PipelineStage
    query_a_hash: str
    query_b_hash: str
    dbms_fingerprint_before: str
    dbms_fingerprint_after: str
    plan_a: NormalizedPlan | None
    plan_b: NormalizedPlan | None
    profile_metrics_a: Mapping[str, float]
    profile_metrics_b: Mapping[str, float]
    structural_changes: tuple[Mapping[str, object], ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_metrics_a", MappingProxyType(dict(self.profile_metrics_a)))
        object.__setattr__(self, "profile_metrics_b", MappingProxyType(dict(self.profile_metrics_b)))
        object.__setattr__(
            self,
            "structural_changes",
            tuple(MappingProxyType(dict(item)) for item in self.structural_changes),
        )

    @property
    def available(self) -> bool:
        return not self.reason_codes and self.plan_a is not None and self.plan_b is not None


__all__ = ["ExplanationEvidence"]
