from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from collections.abc import Mapping
from typing import Protocol

from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence
from graph_sym_perf.core import stable_json_dumps


class ReductionStatus(str, Enum):
    REDUCED_VALID = "reduced_valid"
    REDUCTION_PARTIAL = "reduction_partial"
    REDUCTION_INVALIDATED = "reduction_invalidated"


class SymptomPredicate(Protocol):
    protocol_id: str

    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence: ...


class CompleteEquivalenceGate(Protocol):
    protocol_id: str

    def evaluate(self, pair: EquivalentQueryPair) -> object: ...


class FinalValidationRunner(Protocol):
    protocol_id: str

    def validate(self, candidate: object) -> object: ...


@dataclass(frozen=True)
class RuleWitness:
    """Content-addressed facts that must survive every paired deletion."""

    rule_id: str
    proof_version: str
    relation: str
    required_fragments_a: tuple[str, ...]
    required_fragments_b: tuple[str, ...]
    required_variables: tuple[str, ...]
    output_schema: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        rule_id: str,
        proof_version: str,
        relation: str,
        required_fragments_a: tuple[str, ...],
        required_fragments_b: tuple[str, ...],
        required_variables: tuple[str, ...] = (),
        output_schema: tuple[str, ...],
    ) -> "RuleWitness":
        values = (rule_id, proof_version, relation, *required_fragments_a, *required_fragments_b, *output_schema)
        if any(not value.strip() for value in values):
            raise ValueError("rule witness identity, fragments, and output schema must be non-empty")
        return cls(
            rule_id,
            proof_version,
            relation,
            tuple(required_fragments_a),
            tuple(required_fragments_b),
            tuple(sorted(set(required_variables))),
            tuple(output_schema),
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(stable_json_dumps(self).encode("utf-8")).hexdigest()

    def verify(self, pair: EquivalentQueryPair, certificate: object) -> bool:
        from graph_sym_perf.reducer.dependencies import analyze_query

        if (
            getattr(certificate, "rule_id", None) != self.rule_id
            or getattr(certificate, "proof_id", None) != self.proof_version
            or not self.relation.strip()
            or any(fragment not in pair.query_a for fragment in self.required_fragments_a)
            or any(fragment not in pair.query_b for fragment in self.required_fragments_b)
        ):
            return False
        graph_a = analyze_query(pair.query_a)
        graph_b = analyze_query(pair.query_b)
        variables_a = frozenset().union(*(clause.outgoing_scope for clause in graph_a.clauses))
        variables_b = frozenset().union(*(clause.outgoing_scope for clause in graph_b.clauses))
        required = frozenset(self.required_variables)
        return bool(
            graph_a.valid
            and graph_b.valid
            and graph_a.output_schema == self.output_schema == graph_b.output_schema
            and required <= variables_a
            and required <= variables_b
        )


@dataclass(frozen=True)
class ReductionBundle:
    """Sealed-in-practice reducer input rooted in an original validation report."""

    original_candidate: object
    original_validation: object
    pair: EquivalentQueryPair
    graph_snapshot: object
    resolved_config: Mapping[str, object]
    validation_policy: object
    certificate: object
    rule_witness: RuleWitness
    equivalence_gate: CompleteEquivalenceGate
    runtime_screen: SymptomPredicate
    final_validator: FinalValidationRunner
    content_hashes: tuple[tuple[str, str], ...]
    bundle_hash: str

    @classmethod
    def create(
        cls,
        *,
        original_candidate: object,
        original_validation: object,
        rule_witness: RuleWitness,
        equivalence_gate: CompleteEquivalenceGate,
        runtime_screen: SymptomPredicate,
        final_validator: FinalValidationRunner,
    ) -> "ReductionBundle":
        from graph_sym_perf.validation.evidence import issue_validation_report_is_complete

        pair = getattr(original_candidate, "pair", None)
        if not isinstance(pair, EquivalentQueryPair):
            raise TypeError("ReductionBundle requires a CandidateBundle-like original candidate")
        integrity = tuple(getattr(original_candidate, "verify_integrity")())
        report_integrity = tuple(getattr(original_validation, "verify_integrity")())
        stage = getattr(getattr(original_validation, "stage", None), "value", None)
        if (
            integrity
            or report_integrity
            or stage != "validation_passed"
            or not issue_validation_report_is_complete(original_validation)
        ):
            raise ValueError("ReductionBundle requires an intact VALIDATION_PASSED root")
        sessions = tuple(getattr(original_validation, "sessions", ()))
        if any(
            session.evidence.measurement_source != "ordinary_execute"
            or session.evidence.measurement_protocol != "ordinary-execute-20-pair-v1"
            for session in sessions
        ):
            raise ValueError("ReductionBundle requires ordinary execute validation evidence")
        if (
            getattr(original_validation, "candidate_hash", None) != getattr(original_candidate, "candidate_hash", None)
            or getattr(original_validation, "query_a_hash", None)
            != hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest()
            or getattr(original_validation, "query_b_hash", None)
            != hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest()
        ):
            raise ValueError("original validation is not bound to the candidate pair")
        certificate = getattr(original_candidate, "certificate", None)
        if not rule_witness.verify(pair, certificate):
            raise ValueError("original rule witness is not valid for the candidate")
        protocols = {
            "equivalence_protocol": getattr(equivalence_gate, "protocol_id", ""),
            "runtime_protocol": getattr(runtime_screen, "protocol_id", ""),
            "final_validation_protocol": getattr(final_validator, "protocol_id", ""),
        }
        if any(not isinstance(value, str) or not value.strip() for value in protocols.values()):
            raise ValueError("every reducer gate requires a stable protocol_id")
        candidate_hashes = dict(getattr(original_candidate, "content_hashes"))
        content = {
            "original_candidate": str(getattr(original_candidate, "candidate_hash")),
            "original_validation": _object_hash(original_validation),
            "pair": reduction_pair_hash(pair),
            "graph_snapshot": str(candidate_hashes["graph_snapshot"]),
            "resolved_config": str(candidate_hashes["resolved_config"]),
            "validation_policy": str(candidate_hashes["validation_policy"]),
            "certificate": str(candidate_hashes["certificate"]),
            "rule_witness": rule_witness.content_hash,
            **{name: hashlib.sha256(value.encode("utf-8")).hexdigest() for name, value in protocols.items()},
        }
        frozen_hashes = tuple(sorted(content.items()))
        return cls(
            original_candidate=original_candidate,
            original_validation=original_validation,
            pair=pair,
            graph_snapshot=getattr(original_candidate, "graph_snapshot"),
            resolved_config=getattr(original_candidate, "resolved_config"),
            validation_policy=getattr(original_candidate, "validation_policy"),
            certificate=certificate,
            rule_witness=rule_witness,
            equivalence_gate=equivalence_gate,
            runtime_screen=runtime_screen,
            final_validator=final_validator,
            content_hashes=frozen_hashes,
            bundle_hash=hashlib.sha256(stable_json_dumps(frozen_hashes).encode("utf-8")).hexdigest(),
        )

    def content_hash(self, name: str) -> str:
        return dict(self.content_hashes)[name]

    def verify_integrity(self) -> tuple[str, ...]:
        reasons: list[str] = []
        try:
            rebuilt = ReductionBundle.create(
                original_candidate=self.original_candidate,
                original_validation=self.original_validation,
                rule_witness=self.rule_witness,
                equivalence_gate=self.equivalence_gate,
                runtime_screen=self.runtime_screen,
                final_validator=self.final_validator,
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return ("bundle_root_invalid",)
        if rebuilt.content_hashes != self.content_hashes:
            reasons.append("bundle_content_hash_mismatch")
        if rebuilt.bundle_hash != self.bundle_hash:
            reasons.append("bundle_hash_mismatch")
        return tuple(reasons)


@dataclass(frozen=True)
class ReductionStep:
    proposal: str
    accepted: bool
    reason: str
    attempt_id: str = ""
    index: int = 0
    layer: str = ""
    candidate_hash: str = ""
    gate_trace: tuple[str, ...] = ()
    evidence_hash: str = ""


def reduction_pair_hash(pair: EquivalentQueryPair) -> str:
    query_a_hash = hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest()
    query_b_hash = hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest()
    payload = {
        "query_a_hash": query_a_hash,
        "query_b_hash": query_b_hash,
        "parameters": _parameter_content(pair.parameters),
        "graph_snapshot_hash": pair.graph_snapshot_hash,
        "dbms_fingerprint": pair.dbms_fingerprint,
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def _parameter_content(value: object) -> object:
    """Remove immutable-container implementation details from pair identity."""

    if isinstance(value, Mapping):
        return {str(key): _parameter_content(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return tuple(_parameter_content(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_parameter_content(item) for item in value), key=repr))
    return value


@dataclass(frozen=True)
class MinimalityDeletion:
    canonical_id: str
    proposal_hash: str


@dataclass(frozen=True)
class MinimalityUniverse:
    reduced_pair_hash: str
    deletions: tuple[MinimalityDeletion, ...]
    universe_hash: str

    @classmethod
    def recompute(cls, pair: EquivalentQueryPair) -> "MinimalityUniverse":
        from graph_sym_perf.reducer.proposals import paired_reduction_proposals

        deletions = tuple(
            MinimalityDeletion(proposal.canonical_id, reduction_pair_hash(proposal.candidate))
            for proposal in paired_reduction_proposals(pair)
        )
        pair_hash = reduction_pair_hash(pair)
        universe_hash = hashlib.sha256(
            stable_json_dumps(
                {
                    "reduced_pair_hash": pair_hash,
                    "deletions": deletions,
                }
            ).encode("utf-8")
        ).hexdigest()
        return cls(pair_hash, deletions, universe_hash)


@dataclass(frozen=True)
class MinimalityAttempt:
    deletion_id: str
    proposal_hash: str
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
    universe_hash: str
    enumerated_deletions: tuple[MinimalityDeletion, ...]
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
        universe = MinimalityUniverse.recompute(pair)
        pair_hash = universe.reduced_pair_hash
        previous = _minimality_seed(pair_hash, protocol_version)
        attempts: list[MinimalityAttempt] = []
        for deletion_id, outcome, evidence_hash in results:
            deletion = next(
                (item for item in universe.deletions if item.canonical_id == deletion_id),
                MinimalityDeletion(deletion_id, ""),
            )
            attempt_hash = _attempt_hash(
                deletion_id,
                deletion.proposal_hash,
                outcome,
                evidence_hash,
                previous,
            )
            attempts.append(
                MinimalityAttempt(
                    deletion_id,
                    deletion.proposal_hash,
                    outcome,
                    evidence_hash,
                    previous,
                    attempt_hash,
                )
            )
            previous = attempt_hash
        return cls(
            reduced_pair_hash=pair_hash,
            protocol="exhaustive-one-deletion",
            protocol_version=protocol_version,
            universe_hash=universe.universe_hash,
            enumerated_deletions=universe.deletions,
            attempts=tuple(attempts),
            chain_hash=previous,
        )

    def verify(self, pair: EquivalentQueryPair) -> bool:
        universe = MinimalityUniverse.recompute(pair)
        if (
            self.protocol != "exhaustive-one-deletion"
            or not self.protocol_version.strip()
            or self.reduced_pair_hash != universe.reduced_pair_hash
            or self.universe_hash != universe.universe_hash
            or self.enumerated_deletions != universe.deletions
            or tuple(
                (attempt.deletion_id, attempt.proposal_hash)
                for attempt in self.attempts
            )
            != tuple(
                (deletion.canonical_id, deletion.proposal_hash)
                for deletion in universe.deletions
            )
        ):
            return False
        previous = _minimality_seed(self.reduced_pair_hash, self.protocol_version)
        for attempt in self.attempts:
            if (
                attempt.outcome != "symptom_not_preserved"
                or not attempt.proposal_hash
                or len(attempt.evidence_hash) != 64
                or any(char not in "0123456789abcdef" for char in attempt.evidence_hash.lower())
                or attempt.previous_hash != previous
                or attempt.attempt_hash
                != _attempt_hash(
                    attempt.deletion_id,
                    attempt.proposal_hash,
                    attempt.outcome,
                    attempt.evidence_hash,
                    previous,
                )
            ):
                return False
            previous = attempt.attempt_hash
        return self.chain_hash == previous


def _minimality_seed(pair_hash: str, version: str) -> str:
    return hashlib.sha256(f"minimality\0{version}\0{pair_hash}".encode("utf-8")).hexdigest()


def _attempt_hash(
    deletion_id: str,
    proposal_hash: str,
    outcome: str,
    evidence_hash: str,
    previous: str,
) -> str:
    return hashlib.sha256(
        stable_json_dumps(
            {
                "deletion_id": deletion_id,
                "proposal_hash": proposal_hash,
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
    screen_evidence: RuntimeEvidence | None = None
    minimality_proof: MinimalityProof | None = None
    final_validation: object | None = None
    reduced_candidate_hash: str = ""
    validation_policy_hash: str = ""
    certificate_hash: str = ""
    original_ast_hash: str = ""
    reduced_ast_hash: str = ""
    original_char_count: int = 0
    reduced_char_count: int = 0
    original_ast_node_count: int = 0
    reduced_ast_node_count: int = 0
    attempts: tuple[object, ...] = ()
    stop_reason: str = ""
    bundle_hash: str = ""
    rule_witness_hash: str = ""


@dataclass(frozen=True)
class PartialManualReview:
    """Audit receipt only; it is deliberately not a promotion credential."""

    reviewer: str
    reviewed_at: str
    reason: str
    report_hash: str

    @classmethod
    def create(
        cls,
        report: ReductionReport,
        *,
        reviewer: str,
        reviewed_at: str,
        reason: str,
    ) -> "PartialManualReview":
        if report.status is not ReductionStatus.REDUCTION_PARTIAL:
            raise ValueError("manual review audit requires a partial reduction report")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("manual review requires reviewer and reason")
        try:
            timestamp = datetime.fromisoformat(reviewed_at)
        except ValueError as exc:
            raise ValueError("manual review requires an ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("manual review timestamp must include a timezone")
        return cls(reviewer.strip(), reviewed_at, reason.strip(), _object_hash(report))

    def verify(self, report: ReductionReport) -> bool:
        return bool(
            report.status is ReductionStatus.REDUCTION_PARTIAL
            and self.reviewer
            and self.reason
            and self.report_hash == _object_hash(report)
        )


def _object_hash(value: object) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()
