from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from graph_sym_perf.generation.corpus import PortableQuery
from graph_sym_perf.generation.gdsmith import derive_generation_seed
from graph_sym_perf.transforms.safety_points import SafetyCertificate


class AcceptanceStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class CertifiedQueryPair:
    pair_hash: str
    query_a: str
    query_b: str
    parameters: Mapping[str, object]
    certificate: SafetyCertificate
    transform_metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.pair_hash or not self.query_a or not self.query_b or self.query_a == self.query_b:
            raise ValueError("certified pair identity and distinct query texts are required")
        if not isinstance(self.certificate, SafetyCertificate):
            raise TypeError("certified pair requires a typed SafetyCertificate")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "transform_metadata", MappingProxyType(dict(self.transform_metadata)))


@dataclass(frozen=True)
class PairBuildOutcome:
    pair: CertifiedQueryPair | None
    reject_code: str | None = None
    quarantine: bool = False

    def __post_init__(self) -> None:
        if self.pair is None and not self.reject_code:
            raise ValueError("rejected pair build requires a reason code")
        if self.pair is not None and (self.reject_code is not None or self.quarantine):
            raise ValueError("successful pair build cannot also be rejected or quarantined")
        if self.quarantine and self.pair is not None:
            raise ValueError("quarantine cannot carry an accepted pair")


@dataclass(frozen=True)
class SeedContext:
    ordinal: int
    seed: int
    target: str
    shard_index: int


@dataclass(frozen=True)
class PairProposal:
    query_a: PortableQuery
    query_b: PortableQuery
    rule_id: str
    safety_certificate: object | None
    seed: int

    def __post_init__(self) -> None:
        if not self.rule_id or not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("pair proposal requires a rule id and non-negative seed")
        certificate = self.safety_certificate
        if certificate is not None:
            required = (
                "rule_id", "ast_path", "variables_in_scope", "expression_type", "nullability",
                "clause_context", "preconditions", "proof_id", "capability_hash",
            )
            if any(not hasattr(certificate, field) for field in required):
                raise TypeError("pair proposal requires a typed SafetyCertificate")
            if certificate.rule_id != self.rule_id:
                raise ValueError("pair proposal rule and SafetyCertificate rule differ")

    @property
    def ast_hashes(self) -> frozenset[str]:
        return frozenset((self.query_a.ast_hash, self.query_b.ast_hash))

    @property
    def pair_hash(self) -> str:
        ordered = sorted((self.query_a.ast_hash, self.query_b.ast_hash))
        return hashlib.sha256(":".join(ordered).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PairValidation:
    accepted: bool
    reason_code: str
    evidence: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("pair validation reason code must be non-empty")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class AcceptedPair:
    proposal: PairProposal
    validation: PairValidation


@dataclass(frozen=True)
class AcceptedPairRequest:
    accepted_pairs: int
    max_generated_seeds: int = 10_000
    max_variants_per_seed: int = 1
    max_seconds: float = 6 * 3600
    experiment_seed: int = 0
    target: str = "unspecified"
    shard_index: int = 0
    cross_shard_ast_hashes: frozenset[str] = frozenset()
    cross_shard_pair_hashes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name, value in (
            ("accepted_pairs", self.accepted_pairs),
            ("max_generated_seeds", self.max_generated_seeds),
            ("max_variants_per_seed", self.max_variants_per_seed),
            ("experiment_seed", self.experiment_seed),
            ("shard_index", self.shard_index),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
        if self.accepted_pairs <= 0:
            raise ValueError("accepted_pairs must be positive")
        if not 0 < self.max_generated_seeds <= 10_000:
            raise ValueError("max_generated_seeds must be in [1, 10000]")
        if self.max_generated_seeds < self.accepted_pairs:
            raise ValueError("max_generated_seeds must cover accepted_pairs")
        if not 0 < self.max_variants_per_seed <= 2:
            raise ValueError("max_variants_per_seed must be in [1, 2]")
        if not 0 < float(self.max_seconds) <= 6 * 3600:
            raise ValueError("max_seconds must be in (0, 21600]")
        if self.experiment_seed < 0 or self.shard_index < 0 or not self.target:
            raise ValueError("seed/shard must be non-negative and target non-empty")

    def seed_context(self, ordinal: int) -> SeedContext:
        return SeedContext(
            ordinal=ordinal,
            seed=derive_generation_seed(self.experiment_seed, self.target, self.shard_index, ordinal - 1),
            target=self.target,
            shard_index=self.shard_index,
        )


@dataclass(frozen=True)
class AcceptedPairCorpus:
    pairs: tuple[AcceptedPair, ...]
    generated_seeds: int
    reject_counts: Mapping[str, int]
    status: AcceptanceStatus
    stop_reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reject_counts", MappingProxyType(dict(self.reject_counts)))

    @property
    def accepted_count(self) -> int:
        return len(self.pairs)

    @property
    def ast_hashes(self) -> frozenset[str]:
        return frozenset(value for pair in self.pairs for value in pair.proposal.ast_hashes)

    @property
    def pair_hashes(self) -> frozenset[str]:
        return frozenset(pair.proposal.pair_hash for pair in self.pairs)


__all__ = [
    "AcceptanceStatus",
    "AcceptedPair",
    "AcceptedPairCorpus",
    "AcceptedPairRequest",
    "CertifiedQueryPair",
    "PairBuildOutcome",
    "PairProposal",
    "PairValidation",
    "SeedContext",
]
