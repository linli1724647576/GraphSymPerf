"""Versioned issue registry with exact and possible-duplicate separation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from graph_sym_perf.validation.models import DeveloperStatus, DuplicateDecision, PipelineStage, VersionedIssueRecord


@dataclass
class IssueRegistry:
    records: dict[str, VersionedIssueRecord] = field(default_factory=dict)
    _possible_duplicates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _duplicate_decisions: dict[str, bool] = field(default_factory=dict)

    def record(self, record: VersionedIssueRecord) -> None:
        self._validate_record(record)
        if record.stage is PipelineStage.SUBMITTED:
            raise ValueError("SUBMITTED can only be entered through mark_submitted()")
        if record.issue_id in self.records:
            raise ValueError(f"duplicate issue id: {record.issue_id}")
        if self.exact_duplicate(record) is not None:
            raise ValueError("exact duplicate issue record")
        possible = tuple(
            existing.issue_id
            for existing in self.records.values()
            if self._possible_cluster_key(existing) == self._possible_cluster_key(record)
        )
        self.records[record.issue_id] = record
        if possible:
            self._possible_duplicates[record.issue_id] = possible
            for existing_id in possible:
                linked = self._possible_duplicates.get(existing_id, ())
                self._possible_duplicates[existing_id] = tuple(dict.fromkeys((*linked, record.issue_id)))

    def exact_duplicate(self, candidate: VersionedIssueRecord) -> VersionedIssueRecord | None:
        for record in self.records.values():
            if (record.dbms, record.dbms_version, record.pair_hash, record.direction) == (candidate.dbms, candidate.dbms_version, candidate.pair_hash, candidate.direction):
                return record
        return None

    def update_developer_status(self, issue_id: str, status: DeveloperStatus, evidence_url: str) -> None:
        if not evidence_url.strip():
            raise ValueError("developer status update requires an evidence URL")
        record = self.records[issue_id]
        self.records[issue_id] = VersionedIssueRecord(**{**record.__dict__, "developer_status": status, "status_evidence_url": evidence_url})

    def possible_duplicate_ids(self, issue_id: str) -> tuple[str, ...]:
        return self._possible_duplicates.get(issue_id, ())

    def can_submit(self, issue_id: str) -> bool:
        return not self.possible_duplicate_ids(issue_id) or self._duplicate_decisions.get(issue_id) is True

    def duplicate_decision(self, issue_id: str) -> DuplicateDecision:
        possible = bool(self.possible_duplicate_ids(issue_id))
        resolved = self._duplicate_decisions.get(issue_id) is True
        return DuplicateDecision(possible, resolved, self.records[issue_id].status_evidence_url if resolved else None)

    def resolve_possible_duplicate(self, issue_id: str, *, distinct: bool, evidence_url: str) -> None:
        if not self.possible_duplicate_ids(issue_id):
            raise ValueError("issue is not in a possible-duplicate cluster")
        if not evidence_url.strip():
            raise ValueError("possible-duplicate resolution requires an evidence URL")
        self._duplicate_decisions[issue_id] = distinct
        record = self.records[issue_id]
        self.records[issue_id] = VersionedIssueRecord(**{**record.__dict__, "status_evidence_url": evidence_url})

    def mark_submitted(self, issue_id: str, *, evidence_url: str, submitted_at: str) -> None:
        record = self.records[issue_id]
        if record.stage is not PipelineStage.SUBMIT_READY:
            raise ValueError("only SUBMIT_READY can transition to SUBMITTED")
        if not self.can_submit(issue_id):
            raise ValueError("possible duplicate requires manual distinct resolution before submission")
        if not evidence_url.strip():
            raise ValueError("submission requires an evidence URL")
        date.fromisoformat(submitted_at)
        self.records[issue_id] = VersionedIssueRecord(
            **{
                **record.__dict__,
                "stage": PipelineStage.SUBMITTED,
                "status_evidence_url": evidence_url,
                "submitted_at": submitted_at,
            }
        )

    def reported_issue_ids(self) -> tuple[str, ...]:
        return tuple(sorted(record.issue_id for record in self.records.values() if record.stage is PipelineStage.SUBMITTED))

    def developer_validated_bug_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                record.issue_id
                for record in self.records.values()
                if record.developer_status in {DeveloperStatus.CONFIRMED, DeveloperStatus.FIXED}
            )
        )

    def developer_outcome_counts(self, *, as_of: str, observation_days: int = 90) -> dict[str, int]:
        observed_on = date.fromisoformat(as_of)
        submitted = tuple(record for record in self.records.values() if record.stage is PipelineStage.SUBMITTED)
        censored_waiting = 0
        unresolved_beyond_window = 0
        for record in submitted:
            if record.developer_status is not DeveloperStatus.WAITING_REPLY or not record.submitted_at:
                continue
            age = (observed_on - date.fromisoformat(record.submitted_at)).days
            if age <= observation_days:
                censored_waiting += 1
            else:
                unresolved_beyond_window += 1
        negatives = sum(
            record.developer_status
            in {
                DeveloperStatus.INTENDED_BEHAVIOR,
                DeveloperStatus.UNINTERESTED,
            }
            for record in submitted
        )
        return {
            "submitted": len(submitted),
            "developer_validated": sum(
                record.developer_status in {DeveloperStatus.CONFIRMED, DeveloperStatus.FIXED}
                for record in submitted
            ),
            "responded_negative": negatives,
            "right_censored_waiting": censored_waiting,
            "unresolved_beyond_window": unresolved_beyond_window,
        }

    @staticmethod
    def bug_family_key(record: VersionedIssueRecord) -> tuple[str, str, str, str]:
        return (record.rule_id, record.site_class, record.normalized_pattern, record.normalized_predicate)

    @staticmethod
    def _possible_cluster_key(record: VersionedIssueRecord) -> tuple[str, str, str, str, str, str]:
        return (
            record.dbms,
            record.dbms_version,
            record.rule_id,
            record.site_class,
            record.normalized_pattern,
            record.normalized_predicate,
        )

    @staticmethod
    def _validate_record(record: VersionedIssueRecord) -> None:
        required = {
            "dbms": record.dbms,
            "dbms_version": record.dbms_version,
            "rule_id": record.rule_id,
            "detector_version": record.detector_version,
            "rule_version": record.rule_version,
            "validator_version": record.validator_version,
            "reducer_version": record.reducer_version,
            "discovered_at": record.discovered_at,
            "site_class": record.site_class,
            "normalized_pattern": record.normalized_pattern,
            "normalized_predicate": record.normalized_predicate,
            "reduced_query_a": record.reduced_query_a or "",
            "reduced_query_b": record.reduced_query_b or "",
        }
        missing = tuple(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise ValueError(f"missing issue provenance field(s): {', '.join(missing)}")
        expected_hash = canonical_reduced_pair_hash(record.reduced_query_a or "", record.reduced_query_b or "")
        if record.pair_hash != expected_hash:
            raise ValueError("pair_hash does not match normalized reduced query pair")


def canonical_reduced_pair_hash(query_a: str, query_b: str) -> str:
    payload = f"{_normalized_query_skeleton(query_a)}\0{_normalized_query_skeleton(query_b)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_query_skeleton(query: str) -> str:
    variables: dict[str, str] = {}
    declarations = re.finditer(r"(?:[\(\[]\s*|\bAS\s+)([A-Za-z_]\w*)", query, re.IGNORECASE)
    for match in declarations:
        name = match.group(1)
        if name.upper() in _CYPHER_KEYWORDS:
            continue
        variables.setdefault(name, f"v{len(variables)}")
    normalized = query
    for source, replacement in sorted(variables.items(), key=lambda item: -len(item[0])):
        normalized = re.sub(rf"\b{re.escape(source)}\b", replacement, normalized)
    return " ".join(normalized.upper().split())


_CYPHER_KEYWORDS = {
    "MATCH",
    "OPTIONAL",
    "WHERE",
    "RETURN",
    "WITH",
    "ORDER",
    "BY",
    "AS",
    "AND",
    "OR",
    "TRUE",
    "FALSE",
    "NULL",
}
