"""Versioned issue registry with exact and possible-duplicate separation."""

from __future__ import annotations

from dataclasses import dataclass, field

from graph_sym_perf.validation.models import DeveloperStatus, VersionedIssueRecord


@dataclass
class IssueRegistry:
    records: dict[str, VersionedIssueRecord] = field(default_factory=dict)

    def record(self, record: VersionedIssueRecord) -> None:
        if self.exact_duplicate(record) is not None:
            raise ValueError("exact duplicate issue record")
        self.records[record.issue_id] = record

    def exact_duplicate(self, candidate: VersionedIssueRecord) -> VersionedIssueRecord | None:
        for record in self.records.values():
            if (record.dbms, record.dbms_version, record.pair_hash, record.direction) == (candidate.dbms, candidate.dbms_version, candidate.pair_hash, candidate.direction):
                return record
        return None

    def update_developer_status(self, issue_id: str, status: DeveloperStatus, evidence_url: str) -> None:
        record = self.records[issue_id]
        self.records[issue_id] = VersionedIssueRecord(**{**record.__dict__, "developer_status": status, "status_evidence_url": evidence_url})
