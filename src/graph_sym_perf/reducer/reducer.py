"""Paired, dependency-aware AST-clause reducer."""

from __future__ import annotations

from graph_sym_perf.reducer.models import ReductionReport, ReductionStatus, ReductionStep, SymptomPredicate
from graph_sym_perf.reducer.proposals import paired_reduction_proposals
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeVerdict


class CandidateReducer:
    def __init__(self, predicate: SymptomPredicate, *, max_attempts: int = 500) -> None:
        self.predicate = predicate
        self.max_attempts = max_attempts

    def reduce(self, pair: EquivalentQueryPair) -> ReductionReport:
        current = pair
        accepted: list[ReductionStep] = []
        rejected: list[ReductionStep] = []
        attempts = 0
        while attempts < self.max_attempts:
            proposals = paired_reduction_proposals(current)
            progressed = False
            for proposal in proposals:
                if attempts >= self.max_attempts:
                    break
                attempts += 1
                evidence = self.predicate.evaluate(proposal.candidate)
                if evidence.verdict is RuntimeVerdict.PASS:
                    accepted.append(ReductionStep(proposal.canonical_id, True, "runtime_predicate_passed"))
                    current = proposal.candidate
                    progressed = True
                    break
                rejected.append(ReductionStep(proposal.canonical_id, False, "runtime_predicate_failed"))
            if not progressed:
                break
        final = self.predicate.evaluate(current)
        if final.verdict is not RuntimeVerdict.PASS:
            status = ReductionStatus.REDUCTION_INVALIDATED
        elif accepted:
            status = ReductionStatus.REDUCED_VALID
        else:
            status = ReductionStatus.REDUCTION_PARTIAL
        return ReductionReport(status, pair, current, tuple(accepted), tuple(rejected), final)
