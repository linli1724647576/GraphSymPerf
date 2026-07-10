"""AST-like clause proposals guarded by dependency analysis.

The current Cypher parser cannot model every dialect extension, so this module
uses only top-level clause boundaries and rejects any unproven removal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from graph_sym_perf.reducer.dependencies import dependency_safe_removal
from graph_sym_perf.runtime.models import EquivalentQueryPair


@dataclass(frozen=True)
class ReductionProposal:
    name: str
    query: str


@dataclass(frozen=True)
class PairedReductionProposal:
    canonical_id: str
    candidate: EquivalentQueryPair


_CLAUSE = re.compile(r"\s+(?=(?:MATCH|OPTIONAL MATCH|WHERE|WITH|RETURN|ORDER BY|LIMIT|SKIP)\b)", re.IGNORECASE)


def split_clauses(query: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _CLAUSE.split(query.strip()) if part.strip())


def clause_removal_proposals(query: str) -> tuple[ReductionProposal, ...]:
    clauses = split_clauses(query)
    proposals: list[ReductionProposal] = []
    for index, clause in enumerate(clauses):
        if clause.upper().startswith("RETURN") or not dependency_safe_removal(clauses, index):
            continue
        candidate = clauses[:index] + clauses[index + 1 :]
        proposals.append(ReductionProposal(f"drop_clause_{index}", " ".join(candidate)))
    return tuple(proposals)


def paired_reduction_proposals(pair: EquivalentQueryPair) -> tuple[PairedReductionProposal, ...]:
    proposals: list[PairedReductionProposal] = []
    for proposal in clause_removal_proposals(pair.query_a):
        proposals.append(
            PairedReductionProposal(
                f"a:{proposal.name}",
                EquivalentQueryPair(
                    proposal.query,
                    pair.query_b,
                    pair.parameters,
                    pair.graph_snapshot_hash,
                    pair.dbms_fingerprint,
                ),
            )
        )
    for proposal in clause_removal_proposals(pair.query_b):
        proposals.append(
            PairedReductionProposal(
                f"b:{proposal.name}",
                EquivalentQueryPair(
                    pair.query_a,
                    proposal.query,
                    pair.parameters,
                    pair.graph_snapshot_hash,
                    pair.dbms_fingerprint,
                ),
            )
        )
    return tuple(proposals)
