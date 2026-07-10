from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.reducer.models import ReductionReport
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot


class PipelineStage(str, Enum):
    DETECTED = "detected"
    SCREENED = "screened"
    VALIDATION_PASSED = "validation_passed"
    REDUCED_VALID = "reduced_valid"
    SUBMIT_READY = "submit_ready"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class DeveloperStatus(str, Enum):
    WAITING_REPLY = "waiting_reply"
    ACKNOWLEDGED = "acknowledged"
    CONFIRMED = "confirmed"
    FIXED = "fixed"
    DUPLICATE = "duplicate"
    INTENDED_BEHAVIOR = "intended_behavior"
    UNINTERESTED = "uninterested"
    NEEDS_INFORMATION = "needs_information"


@dataclass(frozen=True)
class FrozenMapping(Mapping[str, object]):
    """Small pickle-safe immutable mapping for evidence-bound inputs."""

    items_tuple: tuple[tuple[str, object], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "FrozenMapping":
        return cls(tuple(sorted((str(key), _freeze_value(item)) for key, item in value.items())))

    def __getitem__(self, key: str) -> object:
        for item_key, value in self.items_tuple:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.items_tuple)

    def __len__(self) -> int:
        return len(self.items_tuple)


@dataclass(frozen=True)
class ValidationPolicy:
    noise_floor_ms: float = 0.0
    timer_resolution_ms: float = 0.001
    min_ratio: float = 3.0
    min_ci_low: float = 2.0
    max_result_rows: int = 100_000
    query_timeout_seconds: float = 30.0
    session_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class CandidateBundle:
    """Discovery candidate with every validation input bound by content hash."""

    pair: EquivalentQueryPair
    graph_snapshot: GraphSnapshot
    dbms_version: str
    resolved_config: Mapping[str, object]
    discovery_slow_query: str
    discovery_ratio: float | None
    validation_policy: ValidationPolicy
    content_hashes: Mapping[str, str]

    @classmethod
    def create(
        cls,
        pair: EquivalentQueryPair,
        *,
        graph_snapshot: GraphSnapshot,
        dbms_version: str,
        resolved_config: Mapping[str, object],
        discovery_slow_query: str,
        discovery_ratio: float | None,
        validation_policy: ValidationPolicy | None = None,
    ) -> "CandidateBundle":
        frozen_pair = replace(pair, parameters=FrozenMapping.from_mapping(pair.parameters))
        frozen_config = FrozenMapping.from_mapping(resolved_config)
        bound_policy = validation_policy or ValidationPolicy()
        content = {
            "query_a": frozen_pair.query_a,
            "query_b": frozen_pair.query_b,
            "parameters": frozen_pair.parameters,
            "graph_snapshot": graph_snapshot.statements,
            "dbms_fingerprint": frozen_pair.dbms_fingerprint,
            "dbms_version": dbms_version,
            "resolved_config": frozen_config,
            "discovery_slow_query": discovery_slow_query,
            "discovery_ratio": discovery_ratio,
            "validation_policy": bound_policy,
        }
        return cls(
            pair=frozen_pair,
            graph_snapshot=graph_snapshot,
            dbms_version=dbms_version,
            resolved_config=frozen_config,
            discovery_slow_query=discovery_slow_query,
            discovery_ratio=discovery_ratio,
            validation_policy=bound_policy,
            content_hashes=FrozenMapping.from_mapping({name: _content_hash(value) for name, value in content.items()}),
        )

    def verify_integrity(self) -> tuple[str, ...]:
        current = {
            "query_a": self.pair.query_a,
            "query_b": self.pair.query_b,
            "parameters": self.pair.parameters,
            "graph_snapshot": self.graph_snapshot.statements,
            "dbms_fingerprint": self.pair.dbms_fingerprint,
            "dbms_version": self.dbms_version,
            "resolved_config": self.resolved_config,
            "discovery_slow_query": self.discovery_slow_query,
            "discovery_ratio": self.discovery_ratio,
            "validation_policy": self.validation_policy,
        }
        reasons = [
            f"{name}_hash_mismatch"
            for name, value in current.items()
            if self.content_hashes.get(name) != _content_hash(value)
        ]
        if self.graph_snapshot.content_hash != GraphSnapshot.from_statements(self.graph_snapshot.statements).content_hash:
            reasons.append("graph_snapshot_content_hash_mismatch")
        if self.pair.graph_snapshot_hash != self.graph_snapshot.content_hash:
            reasons.append("pair_graph_snapshot_mismatch")
        if not self.pair.query_a.strip() or not self.pair.query_b.strip():
            reasons.append("query_text_unavailable")
        if not self.graph_snapshot.statements:
            reasons.append("graph_snapshot_unavailable")
        if not self.pair.dbms_fingerprint.strip():
            reasons.append("dbms_fingerprint_unavailable")
        if not self.dbms_version.strip():
            reasons.append("dbms_version_unavailable")
        if not self.resolved_config:
            reasons.append("resolved_config_unavailable")
        if self.discovery_slow_query not in {"a", "b"}:
            reasons.append("invalid_discovery_slow_query")
        return tuple(reasons)


def _content_hash(value: object) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenMapping.from_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze_value(item) for item in value), key=repr))
    return value


@dataclass(frozen=True)
class SafetyCertificate:
    rule_id: str
    tier: str
    proof_version: str
    site_class: str


@dataclass(frozen=True)
class DuplicateDecision:
    possible_duplicate: bool
    manually_resolved: bool
    evidence_url: str | None = None

    @property
    def cleared(self) -> bool:
        return not self.possible_duplicate or (
            self.manually_resolved and bool(self.evidence_url and self.evidence_url.strip())
        )


@dataclass(frozen=True)
class EquivalenceEvidence:
    equivalent: bool
    complete_result: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class SessionLifecycle:
    process_started: bool
    adapter_created: bool
    connection_opened: bool
    graph_reset: bool
    graph_loaded: bool
    graph_verified: bool
    connection_closed: bool
    process_exited: bool
    connection_id: str

    @property
    def complete(self) -> bool:
        return all(
            (
                self.process_started,
                self.adapter_created,
                self.connection_opened,
                self.graph_reset,
                self.graph_loaded,
                self.graph_verified,
                self.connection_closed,
                self.process_exited,
                bool(self.connection_id),
            )
        )


@dataclass(frozen=True)
class ValidationSession:
    session_id: str
    process_id: int
    graph_fingerprint: str
    dbms_fingerprint: str
    evidence: RuntimeEvidence
    negative_controls: tuple[RuntimeEvidence, ...]
    health_event: str | None = None
    lifecycle: SessionLifecycle | None = None


@dataclass(frozen=True)
class IssueValidationReport:
    stage: PipelineStage
    equivalence: EquivalenceEvidence
    sessions: tuple[ValidationSession, ...]
    discovery_ratio: float | None
    validated_ratio: float | None
    magnitude_stability: str
    reason_codes: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


_PROMOTION_TOKEN = object()


@dataclass(frozen=True, init=False)
class SubmitReadyIssue:
    stage: PipelineStage
    developer_status: DeveloperStatus
    validation: IssueValidationReport
    reduction: ReductionReport
    certificate: SafetyCertificate
    duplicate_decision: DuplicateDecision

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("SubmitReadyIssue can only be constructed by promote()")

    @classmethod
    def _from_promotion(
        cls,
        validation: IssueValidationReport,
        reduction: ReductionReport,
        certificate: SafetyCertificate,
        duplicate_decision: DuplicateDecision,
        *,
        token: object,
    ) -> "SubmitReadyIssue":
        if token is not _PROMOTION_TOKEN:
            raise TypeError("SubmitReadyIssue can only be constructed by promote()")
        instance = object.__new__(cls)
        object.__setattr__(instance, "stage", PipelineStage.SUBMIT_READY)
        object.__setattr__(instance, "developer_status", DeveloperStatus.WAITING_REPLY)
        object.__setattr__(instance, "validation", validation)
        object.__setattr__(instance, "reduction", reduction)
        object.__setattr__(instance, "certificate", certificate)
        object.__setattr__(instance, "duplicate_decision", duplicate_decision)
        return instance


@dataclass(frozen=True)
class VersionedIssueRecord:
    issue_id: str
    stage: PipelineStage
    developer_status: DeveloperStatus
    dbms: str
    dbms_version: str
    pair_hash: str
    direction: str
    rule_id: str
    detector_version: str
    validator_version: str
    reducer_version: str
    status_evidence_url: str | None = None
    rule_version: str = ""
    discovered_at: str = ""
    site_class: str = ""
    normalized_pattern: str = ""
    normalized_predicate: str = ""
    reduced_query_a: str | None = None
    reduced_query_b: str | None = None
    submitted_at: str | None = None


@dataclass(frozen=True)
class ReportedIssueReport:
    """A report counted by pipeline publication, independent of maintainer outcome."""

    issue_id: str
    dbms: str
    developer_status: DeveloperStatus

    @classmethod
    def from_record(cls, record: VersionedIssueRecord) -> "ReportedIssueReport":
        if record.stage is not PipelineStage.SUBMITTED:
            raise ValueError("ReportedIssueReport requires PipelineStage.SUBMITTED")
        return cls(
            issue_id=record.issue_id,
            dbms=record.dbms,
            developer_status=record.developer_status,
        )


@dataclass(frozen=True)
class DeveloperValidatedBug:
    """A maintainer-confirmed outcome counted independently of report publication."""

    issue_id: str
    dbms: str
    developer_status: DeveloperStatus

    @classmethod
    def from_record(cls, record: VersionedIssueRecord) -> "DeveloperValidatedBug":
        if record.developer_status not in {DeveloperStatus.CONFIRMED, DeveloperStatus.FIXED}:
            raise ValueError("DeveloperValidatedBug requires DeveloperStatus.CONFIRMED or FIXED")
        return cls(
            issue_id=record.issue_id,
            dbms=record.dbms,
            developer_status=record.developer_status,
        )
