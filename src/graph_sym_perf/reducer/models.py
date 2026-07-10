from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence
from graph_sym_perf.core import stable_json_dumps


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


def reduction_pair_hash(pair: EquivalentQueryPair) -> str:
    query_a_hash = hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest()
    query_b_hash = hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest()
    payload = {
        "query_a_hash": query_a_hash,
        "query_b_hash": query_b_hash,
        "parameters": pair.parameters,
        "graph_snapshot_hash": pair.graph_snapshot_hash,
        "dbms_fingerprint": pair.dbms_fingerprint,
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MinimalityAttempt:
    deletion_id: str
    outcome: str
    evidence_hash: str
    previous_hash: str
    attempt_hash: str


@dataclass(frozen=True)
class MinimalityProof:
    """Evidence contract only; Task 5 must produce the exhaustive attempts."""

    reduced_pair_hash: str
    protocol: str
    protocol_version: str
    enumerated_deletions: tuple[str, ...]
    attempts: tuple[MinimalityAttempt, ...]
    chain_hash: str

    @classmethod
    def from_results(
        cls,
        pair: EquivalentQueryPair,
        results: tuple[tuple[str, str, str], ...],
        *,
        protocol_version: str = "v1",
    ) -> "MinimalityProof":
        pair_hash = reduction_pair_hash(pair)
        previous = _minimality_seed(pair_hash, protocol_version)
        attempts: list[MinimalityAttempt] = []
        for deletion_id, outcome, evidence_hash in results:
            attempt_hash = _attempt_hash(deletion_id, outcome, evidence_hash, previous)
            attempts.append(MinimalityAttempt(deletion_id, outcome, evidence_hash, previous, attempt_hash))
            previous = attempt_hash
        return cls(
            reduced_pair_hash=pair_hash,
            protocol="exhaustive-one-deletion",
            protocol_version=protocol_version,
            enumerated_deletions=tuple(item[0] for item in results),
            attempts=tuple(attempts),
            chain_hash=previous,
        )

    def verify(self, pair: EquivalentQueryPair) -> bool:
        if (
            self.protocol != "exhaustive-one-deletion"
            or not self.protocol_version.strip()
            or self.reduced_pair_hash != reduction_pair_hash(pair)
            or not self.enumerated_deletions
            or len(set(self.enumerated_deletions)) != len(self.enumerated_deletions)
            or tuple(attempt.deletion_id for attempt in self.attempts) != self.enumerated_deletions
        ):
            return False
        previous = _minimality_seed(self.reduced_pair_hash, self.protocol_version)
        for attempt in self.attempts:
            if (
                attempt.outcome != "symptom_not_preserved"
                or len(attempt.evidence_hash) != 64
                or any(char not in "0123456789abcdef" for char in attempt.evidence_hash.lower())
                or attempt.previous_hash != previous
                or attempt.attempt_hash
                != _attempt_hash(attempt.deletion_id, attempt.outcome, attempt.evidence_hash, previous)
            ):
                return False
            previous = attempt.attempt_hash
        return self.chain_hash == previous


def _minimality_seed(pair_hash: str, version: str) -> str:
    return hashlib.sha256(f"minimality\0{version}\0{pair_hash}".encode("utf-8")).hexdigest()


def _attempt_hash(deletion_id: str, outcome: str, evidence_hash: str, previous: str) -> str:
    return hashlib.sha256(
        stable_json_dumps(
            {
                "deletion_id": deletion_id,
                "outcome": outcome,
                "evidence_hash": evidence_hash,
                "previous_hash": previous,
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ReductionReport:
    status: ReductionStatus
    original_pair: EquivalentQueryPair
    reduced_pair: EquivalentQueryPair
    accepted_steps: tuple[ReductionStep, ...]
    rejected_steps: tuple[ReductionStep, ...]
    final_evidence: RuntimeEvidence
    minimality_proof: MinimalityProof | None = None
    final_validation: object | None = None
    reduced_candidate_hash: str = ""
    validation_policy_hash: str = ""
    certificate_hash: str = ""
