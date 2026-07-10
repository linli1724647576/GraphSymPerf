from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence


class ReductionStatus(str, Enum):
    REDUCED_VALID = "reduced_valid"
    REDUCTION_PARTIAL = "reduction_partial"
    REDUCTION_INVALIDATED = "reduction_invalidated"


class SymptomPredicate(Protocol):
    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence: ...


@dataclass(frozen=True)
class ReductionStep:
    proposal: str
    accepted: bool
    reason: str


@dataclass(frozen=True)
class ReductionReport:
    status: ReductionStatus
    original_pair: EquivalentQueryPair
    reduced_pair: EquivalentQueryPair
    accepted_steps: tuple[ReductionStep, ...]
    rejected_steps: tuple[ReductionStep, ...]
    final_evidence: RuntimeEvidence
