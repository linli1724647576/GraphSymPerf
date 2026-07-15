from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.reducer.models import ReductionReport
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot

if TYPE_CHECKING:
    from graph_sym_perf.issues.dedup import IssueFamilyDecision


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


class SubmissionPath(str, Enum):
    RELATIVE = "relative"
    ABSOLUTE = "absolute"


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
    certificate: SafetyCertificate
    content_hashes: Mapping[str, str]
    candidate_hash: str

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
        certificate: SafetyCertificate,
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
            "certificate": certificate,
        }
        hashes = {
            name: (
                query_text_hash(value)
                if name in {"query_a", "query_b"}
                else graph_snapshot.content_hash
                if name == "graph_snapshot"
                else _content_hash(value)
            )
            for name, value in content.items()
        }
        return cls(
            pair=frozen_pair,
            graph_snapshot=graph_snapshot,
            dbms_version=dbms_version,
            resolved_config=frozen_config,
            discovery_slow_query=discovery_slow_query,
            discovery_ratio=discovery_ratio,
            validation_policy=bound_policy,
            certificate=certificate,
            content_hashes=FrozenMapping.from_mapping(hashes),
            candidate_hash=_content_hash(hashes),
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
            "certificate": self.certificate,
        }
        reasons = [
            f"{name}_hash_mismatch"
            for name, value in current.items()
            if self.content_hashes.get(name)
            != (
                query_text_hash(value)
                if name in {"query_a", "query_b"}
                else self.graph_snapshot.content_hash
                if name == "graph_snapshot"
                else _content_hash(value)
            )
        ]
        if self.candidate_hash != _content_hash(dict(self.content_hashes)):
            reasons.append("candidate_hash_mismatch")
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
        if (
            not self.certificate.rule_id.strip()
            or not self.certificate.proof_id.strip()
            or not self.certificate.proof_version.strip()
        ):
            reasons.append("certificate_identity_unavailable")
        return tuple(reasons)


@dataclass(frozen=True)
class CandidateManifest:
    pair: EquivalentQueryPair
    graph_snapshot: GraphSnapshot
    dbms_version: str
    resolved_config: Mapping[str, object]
    discovery_slow_query: str
    discovery_ratio: float | None
    certificate: SafetyCertificate
    content_hashes: Mapping[str, str]
    candidate_hash: str

    @classmethod
    def from_candidate(cls, candidate: CandidateBundle) -> "CandidateManifest":
        return cls(
            pair=replace(candidate.pair, parameters=FrozenMapping.from_mapping(candidate.pair.parameters)),
            graph_snapshot=candidate.graph_snapshot,
            dbms_version=candidate.dbms_version,
            resolved_config=FrozenMapping.from_mapping(candidate.resolved_config),
            discovery_slow_query=candidate.discovery_slow_query,
            discovery_ratio=candidate.discovery_ratio,
            certificate=candidate.certificate,
            content_hashes=FrozenMapping.from_mapping(candidate.content_hashes),
            candidate_hash=candidate.candidate_hash,
        )

    def verify_integrity(self, policy: ValidationPolicy) -> tuple[str, ...]:
        return CandidateBundle(
            pair=self.pair,
            graph_snapshot=self.graph_snapshot,
            dbms_version=self.dbms_version,
            resolved_config=self.resolved_config,
            discovery_slow_query=self.discovery_slow_query,
            discovery_ratio=self.discovery_ratio,
            validation_policy=policy,
            certificate=self.certificate,
            content_hashes=self.content_hashes,
            candidate_hash=self.candidate_hash,
        ).verify_integrity()


def _content_hash(value: object) -> str:
    return hashlib.sha256(stable_json_dumps(_hashable_value(value)).encode("utf-8")).hexdigest()


def canonical_content_hash(value: object) -> str:
    return _content_hash(value)


_VALIDATION_REPORT_PROJECTION_FIELDS = (
    "candidate_hash",
    "stage",
    "validation_protocol",
    "equivalence",
    "sessions",
    "dbms_fingerprint",
    "validated_ratio",
    "pooled_delta_ms",
    "pooled_ratio_ci_low",
    "pooled_delta_ci_low_ms",
    "submission_path",
    "magnitude_stability",
    "noise_floor_ms",
    "timer_resolution_ms",
    "metadata",
    "reason_codes",
)


def canonical_validation_payload_projection(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Canonical, disk-recomputable projection of a typed validation report."""

    projection = {name: payload.get(name) for name in _VALIDATION_REPORT_PROJECTION_FIELDS}
    projection["metadata"] = payload.get("metadata", {})
    projection["reason_codes"] = payload.get("reason_codes", ())
    return projection


def canonical_validation_payload_hash(payload: Mapping[str, object]) -> str:
    return _content_hash(canonical_validation_payload_projection(payload))


def canonical_validation_report_projection(report: "IssueValidationReport") -> dict[str, object]:
    if not isinstance(report, IssueValidationReport):
        raise TypeError("validation projection requires IssueValidationReport")
    return canonical_validation_payload_projection(
        {
            "candidate_hash": report.candidate_hash,
            "stage": report.stage.value,
            "validation_protocol": report.validation_protocol,
            "equivalence": asdict(report.equivalence),
            "sessions": tuple(
                validation_session_projection(session, index)
                for index, session in enumerate(report.sessions)
            ),
            "dbms_fingerprint": report.manifest.pair.dbms_fingerprint,
            "validated_ratio": report.validated_ratio,
            "pooled_delta_ms": report.pooled_delta_ms,
            "pooled_ratio_ci_low": report.pooled_ratio_ci_low,
            "pooled_delta_ci_low_ms": report.pooled_delta_ci_low_ms,
            "submission_path": (
                report.submission_path.value if report.submission_path is not None else None
            ),
            "magnitude_stability": report.magnitude_stability,
            "noise_floor_ms": report.validation_policy.noise_floor_ms,
            "timer_resolution_ms": report.validation_policy.timer_resolution_ms,
            "metadata": dict(report.metadata),
            "reason_codes": report.reason_codes,
        }
    )


def canonical_validation_report_hash(report: "IssueValidationReport") -> str:
    return _content_hash(canonical_validation_report_projection(report))


def validation_session_projection(session: "ValidationSession", index: int) -> dict[str, object]:
    lifecycle = session.lifecycle
    lifecycle_payload = (
        {"complete": False}
        if lifecycle is None
        else {**asdict(lifecycle), "complete": bool(lifecycle.complete)}
    )
    evidence = session.evidence
    return {
        "session_id": session.session_id,
        "process_id": session.process_id,
        "graph_fingerprint": session.graph_fingerprint,
        "dbms_fingerprint": session.dbms_fingerprint,
        "query_a_hash": evidence.query_a_hash,
        "query_b_hash": evidence.query_b_hash,
        "slow_query": evidence.slow_query,
        "measurement_source": evidence.measurement_source,
        "measurement_protocol": evidence.measurement_protocol,
        "runtime_verdict": evidence.verdict.value,
        "runtime_reason_codes": evidence.reason_codes,
        "paired_timing": tuple(asdict(sample) for sample in evidence.paired_samples),
        "negative_controls": tuple(
            {
                "kind": "A/A" if control_index == 0 else "B/B",
                "paired_timing": tuple(asdict(sample) for sample in control.paired_samples),
            }
            for control_index, control in enumerate(session.negative_controls)
        ),
        "lifecycle": lifecycle_payload,
        "health_event": session.health_event,
        "session_number": index + 1,
        "schedule_seed": session.schedule_seed,
    }


def canonical_validation_provenance_hash(
    *,
    original_validation_hash: str,
    reduced_validation_hash: str,
    original_validation_ref: Mapping[str, object],
    reduced_validation_ref: Mapping[str, object],
) -> str:
    return _content_hash(
        {
            "version": "validation-provenance-v1",
            "original_validation_hash": original_validation_hash,
            "reduced_validation_hash": reduced_validation_hash,
            "original_validation_ref": original_validation_ref,
            "reduced_validation_ref": reduced_validation_ref,
        }
    )


def query_text_hash(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("query hash requires text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safety_certificate_hash(certificate: SafetyCertificate) -> str:
    return _content_hash(certificate)


def candidate_hash_from_content_hashes(content_hashes: Mapping[str, str]) -> str:
    return _content_hash(dict(content_hashes))


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenMapping.from_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze_value(item) for item in value), key=repr))
    return value


def _hashable_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _hashable_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return tuple(_hashable_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_hashable_value(item) for item in value), key=repr))
    return value


_DUPLICATE_CLEARANCE_TOKEN = object()


@dataclass(frozen=True, init=False)
class DuplicateClearance:
    issue_id: str
    possible_duplicate: bool
    cluster_members: tuple[str, ...]
    cluster_version_hash: str
    evidence_url: str | None
    registry_signature: str
    family_id: str | None
    family_version_hash: str
    family_decision_hash: str
    _active: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("DuplicateClearance can only be issued by IssueRegistry")

    @classmethod
    def _from_registry(
        cls,
        *,
        issue_id: str,
        possible_duplicate: bool,
        cluster_members: tuple[str, ...],
        cluster_version_hash: str,
        evidence_url: str | None,
        family_id: str | None = None,
        family_version_hash: str = "",
        family_decision_hash: str = "",
        token: object,
    ) -> "DuplicateClearance":
        if token is not _DUPLICATE_CLEARANCE_TOKEN:
            raise TypeError("DuplicateClearance can only be issued by IssueRegistry")
        instance = object.__new__(cls)
        fields = {
            "issue_id": issue_id,
            "possible_duplicate": possible_duplicate,
            "cluster_members": tuple(sorted(cluster_members)),
            "cluster_version_hash": cluster_version_hash,
            "evidence_url": evidence_url,
            "family_id": family_id,
            "family_version_hash": family_version_hash,
            "family_decision_hash": family_decision_hash,
        }
        for name, value in fields.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "registry_signature", _duplicate_signature(fields))
        object.__setattr__(instance, "_active", True)
        return instance

    def verify(self) -> bool:
        fields = {
            "issue_id": self.issue_id,
            "possible_duplicate": self.possible_duplicate,
            "cluster_members": self.cluster_members,
            "cluster_version_hash": self.cluster_version_hash,
            "evidence_url": self.evidence_url,
            "family_id": self.family_id,
            "family_version_hash": self.family_version_hash,
            "family_decision_hash": self.family_decision_hash,
        }
        family_fields = (self.family_id, self.family_version_hash, self.family_decision_hash)
        family_binding_complete = all(family_fields) or not any(family_fields)
        return bool(
            self._active
            and self.issue_id in self.cluster_members
            and self.cluster_version_hash
            and self.registry_signature == _duplicate_signature(fields)
            and family_binding_complete
            and (
                not self.possible_duplicate
                or bool(self.evidence_url and self.evidence_url.strip())
            )
        )

    def _invalidate(self) -> None:
        object.__setattr__(self, "_active", False)


def _duplicate_signature(fields: Mapping[str, object]) -> str:
    return hashlib.sha256(
        ("graph-sym-perf-duplicate-clearance-v1\0" + stable_json_dumps(fields)).encode("utf-8")
    ).hexdigest()


def serialized_duplicate_clearance_is_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        members = tuple(sorted(str(member) for member in value["cluster_members"]))
        fields = {
            "issue_id": str(value["issue_id"]),
            "possible_duplicate": value["possible_duplicate"],
            "cluster_members": members,
            "cluster_version_hash": str(value["cluster_version_hash"]),
            "evidence_url": value["evidence_url"],
            "family_id": value["family_id"],
            "family_version_hash": str(value["family_version_hash"]),
            "family_decision_hash": str(value["family_decision_hash"]),
        }
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        isinstance(fields["possible_duplicate"], bool)
        and fields["issue_id"] in members
        and value.get("registry_signature") == _duplicate_signature(fields)
        and fields["family_id"]
        and fields["family_version_hash"]
        and fields["family_decision_hash"]
        and (
            not fields["possible_duplicate"]
            or isinstance(fields["evidence_url"], str)
            and bool(fields["evidence_url"].strip())
        )
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
    schedule_seed: int | None = None


_VALIDATION_REPORT_TOKEN = object()


@dataclass(frozen=True, init=False)
class IssueValidationReport:
    stage: PipelineStage
    equivalence: EquivalenceEvidence
    sessions: tuple[ValidationSession, ...]
    discovery_ratio: float | None
    validated_ratio: float | None
    pooled_delta_ms: float | None
    pooled_ratio_ci_low: float | None
    pooled_delta_ci_low_ms: float | None
    submission_path: SubmissionPath | None
    magnitude_stability: str
    reason_codes: tuple[str, ...]
    manifest: CandidateManifest
    validation_policy: ValidationPolicy
    validation_protocol: str
    metadata: Mapping[str, object]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("IssueValidationReport can only be constructed by validator")

    @classmethod
    def _from_validator(
        cls,
        *,
        stage: PipelineStage,
        equivalence: EquivalenceEvidence,
        sessions: tuple[ValidationSession, ...],
        discovery_ratio: float | None,
        validated_ratio: float | None,
        pooled_delta_ms: float | None = None,
        pooled_ratio_ci_low: float | None = None,
        pooled_delta_ci_low_ms: float | None = None,
        submission_path: SubmissionPath | None = None,
        magnitude_stability: str,
        reason_codes: tuple[str, ...],
        candidate: CandidateBundle,
        metadata: Mapping[str, object] | None,
        token: object,
    ) -> "IssueValidationReport":
        if token is not _VALIDATION_REPORT_TOKEN:
            raise TypeError("IssueValidationReport can only be constructed by validator")
        instance = object.__new__(cls)
        values = {
            "stage": stage,
            "equivalence": equivalence,
            "sessions": tuple(sessions),
            "discovery_ratio": discovery_ratio,
            "validated_ratio": validated_ratio,
            "pooled_delta_ms": pooled_delta_ms,
            "pooled_ratio_ci_low": pooled_ratio_ci_low,
            "pooled_delta_ci_low_ms": pooled_delta_ci_low_ms,
            "submission_path": submission_path,
            "magnitude_stability": magnitude_stability,
            "reason_codes": tuple(reason_codes),
            "manifest": CandidateManifest.from_candidate(candidate),
            "validation_policy": candidate.validation_policy,
            "validation_protocol": "independent-two-session-v1",
            "metadata": FrozenMapping.from_mapping(metadata or {}),
        }
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance

    @property
    def candidate_hash(self) -> str:
        return self.manifest.candidate_hash

    @property
    def query_a_hash(self) -> str:
        return str(self.manifest.content_hashes["query_a"])

    @property
    def query_b_hash(self) -> str:
        return str(self.manifest.content_hashes["query_b"])

    @property
    def policy_hash(self) -> str:
        return str(self.manifest.content_hashes["validation_policy"])

    @property
    def certificate_hash(self) -> str:
        return str(self.manifest.content_hashes["certificate"])

    @property
    def rule_id(self) -> str:
        return self.manifest.certificate.rule_id

    @property
    def proof_version(self) -> str:
        return self.manifest.certificate.proof_id

    def verify_integrity(self) -> tuple[str, ...]:
        return self.manifest.verify_integrity(self.validation_policy)


_PROMOTION_TOKEN = object()


@dataclass(frozen=True, init=False)
class SubmitReadyIssue:
    issue_id: str
    family_id: str
    stage: PipelineStage
    developer_status: DeveloperStatus
    validation: IssueValidationReport
    reduction: ReductionReport
    certificate: SafetyCertificate
    duplicate_clearance: DuplicateClearance
    family_decision: IssueFamilyDecision

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("SubmitReadyIssue can only be constructed by promote()")

    @classmethod
    def _from_promotion(
        cls,
        validation: IssueValidationReport,
        reduction: ReductionReport,
        certificate: SafetyCertificate,
        duplicate_clearance: DuplicateClearance,
        family_decision: IssueFamilyDecision,
        *,
        issue_id: str,
        token: object,
    ) -> "SubmitReadyIssue":
        if token is not _PROMOTION_TOKEN:
            raise TypeError("SubmitReadyIssue can only be constructed by promote()")
        instance = object.__new__(cls)
        object.__setattr__(instance, "issue_id", issue_id)
        object.__setattr__(instance, "family_id", getattr(family_decision, "family_id"))
        object.__setattr__(instance, "stage", PipelineStage.SUBMIT_READY)
        object.__setattr__(instance, "developer_status", DeveloperStatus.WAITING_REPLY)
        object.__setattr__(instance, "validation", validation)
        object.__setattr__(instance, "reduction", reduction)
        object.__setattr__(instance, "certificate", certificate)
        object.__setattr__(instance, "duplicate_clearance", duplicate_clearance)
        object.__setattr__(instance, "family_decision", family_decision)
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
    dbms_fingerprint: str = ""
    graph_snapshot_hash: str = ""
    family_id: str = ""
    family_version_hash: str = ""
    family_decision_hash: str = ""
    exact_signature: str = ""
    structural_signature: str | None = None
    fingerprint_hash: str = ""
    submission_path: str = ""
    reduced_phenotype: str = ""
    known_regression_match: bool = False
    original_validation_hash: str = ""
    reduced_validation_hash: str = ""
    original_validation_ref: Mapping[str, object] | None = None
    reduced_validation_ref: Mapping[str, object] | None = None
    validation_provenance_hash: str = ""
    owner_issue_id: str = ""


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
