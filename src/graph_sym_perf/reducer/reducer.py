"""Paired, dependency-aware AST-clause reducer."""

from __future__ import annotations

from graph_sym_perf.reducer.models import ReductionReport, ReductionStatus, ReductionStep, SymptomPredicate
from graph_sym_perf.reducer.proposals import clause_removal_proposals
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
            proposals = _paired_proposals(current)
            progressed = False
            for proposal_name, candidate in proposals:
                if attempts >= self.max_attempts:
                    break
                attempts += 1
                evidence = self.predicate.evaluate(candidate)
                if evidence.verdict is RuntimeVerdict.PASS:
                    accepted.append(ReductionStep(proposal_name, True, "runtime_predicate_passed"))
                    current = candidate
                    progressed = True
                    break
                rejected.append(ReductionStep(proposal_name, False, "runtime_predicate_failed"))
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


def _paired_proposals(pair: EquivalentQueryPair) -> tuple[tuple[str, EquivalentQueryPair], ...]:
    proposals: list[tuple[str, EquivalentQueryPair]] = []
    for proposal in clause_removal_proposals(pair.query_a):
        proposals.append((f"a:{proposal.name}", EquivalentQueryPair(proposal.query, pair.query_b, pair.parameters, pair.graph_snapshot_hash, pair.dbms_fingerprint)))
    for proposal in clause_removal_proposals(pair.query_b):
        proposals.append((f"b:{proposal.name}", EquivalentQueryPair(pair.query_a, proposal.query, pair.parameters, pair.graph_snapshot_hash, pair.dbms_fingerprint)))
    return tuple(proposals)
