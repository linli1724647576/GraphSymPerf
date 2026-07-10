from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence


class PipelineStage(str, Enum):
    DETECTED = "detected"
    SCREENED = "screened"
    VALIDATION_PASSED = "validation_passed"
    REDUCED_VALID = "reduced_valid"
    SUBMIT_READY = "submit_ready"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


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
class SafetyCertificate:
    rule_id: str
    tier: str
    proof_version: str
    site_class: str


@dataclass(frozen=True)
class EquivalenceEvidence:
    equivalent: bool
    complete_result: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ValidationSession:
    session_id: str
    process_id: int
    graph_fingerprint: str
    dbms_fingerprint: str
    evidence: RuntimeEvidence
    negative_controls: tuple[RuntimeEvidence, ...]
    health_event: str | None = None


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


@dataclass(frozen=True)
class SubmitReadyIssue:
    stage: PipelineStage
    developer_status: DeveloperStatus
    validation: IssueValidationReport
    certificate: SafetyCertificate


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
