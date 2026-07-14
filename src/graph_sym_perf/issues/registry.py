"""Versioned issue registry with exact and possible-duplicate separation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from collections.abc import Iterable

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.cypher.parser import alpha_normalize_query
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    DuplicateClearance,
    PipelineStage,
    VersionedIssueRecord,
    _DUPLICATE_CLEARANCE_TOKEN,
)


@dataclass
class IssueRegistry:
    records: dict[str, VersionedIssueRecord] = field(default_factory=dict)
    _possible_duplicates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _duplicate_clearances: dict[str, DuplicateClearance] = field(default_factory=dict)

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
            for issue_id in (*possible, record.issue_id):
                self._invalidate_clearance(issue_id)

    def exact_duplicate(self, candidate: VersionedIssueRecord) -> VersionedIssueRecord | None:
        for record in self.records.values():
            if record.exact_signature and candidate.exact_signature:
                if record.exact_signature == candidate.exact_signature:
                    return record
                continue
            if (record.dbms, record.dbms_version, record.pair_hash, record.direction) == (candidate.dbms, candidate.dbms_version, candidate.pair_hash, candidate.direction):
                return record
        return None

    def duplicate_clearance_for_family(
        self,
        decision: object,
        records: Iterable[VersionedIssueRecord],
    ) -> DuplicateClearance:
        """Sign a clearance only for the current parser-proven family snapshot."""

        from graph_sym_perf.issues.dedup import FamilyMatchKind, IssueFamilyDecision

        if (
            not isinstance(decision, IssueFamilyDecision)
            or not decision.verify()
            or not decision.is_new_family
            or decision.match_kind is not FamilyMatchKind.FRESH
            or decision.family_id is None
        ):
            raise ValueError("family clearance requires a current family-owner decision")
        by_id = {record.issue_id: record for record in records}
        expected_members = dict(decision.family_members)
        if set(by_id) != set(expected_members):
            raise ValueError("family clearance records do not match current family members")
        for issue_id, exact in expected_members.items():
            record = by_id[issue_id]
            if record.exact_signature != exact or record.family_id != decision.family_id:
                raise ValueError("family clearance record fingerprint mismatch")
        members = tuple(sorted(expected_members))
        return DuplicateClearance._from_registry(
            issue_id=decision.issue_id,
            possible_duplicate=False,
            cluster_members=members,
            cluster_version_hash=decision.family_version_hash,
            evidence_url=None,
            family_id=decision.family_id,
            family_version_hash=decision.family_version_hash,
            family_decision_hash=decision.decision_hash,
            token=_DUPLICATE_CLEARANCE_TOKEN,
        )

    def update_developer_status(self, issue_id: str, status: DeveloperStatus, evidence_url: str) -> None:
        if not evidence_url.strip():
            raise ValueError("developer status update requires an evidence URL")
        record = self.records[issue_id]
        self.records[issue_id] = VersionedIssueRecord(**{**record.__dict__, "developer_status": status, "status_evidence_url": evidence_url})

    def possible_duplicate_ids(self, issue_id: str) -> tuple[str, ...]:
        return self._possible_duplicates.get(issue_id, ())

    def can_submit(self, issue_id: str, clearance: DuplicateClearance | None) -> bool:
        return bool(
            clearance is not None
            and clearance.verify()
            and clearance.issue_id == issue_id
            and clearance.cluster_version_hash == self._cluster_version_hash(issue_id)
            and clearance.cluster_members == self._cluster_members(issue_id)
        )

    def duplicate_clearance(self, issue_id: str) -> DuplicateClearance | None:
        possible = bool(self.possible_duplicate_ids(issue_id))
        if possible:
            clearance = self._duplicate_clearances.get(issue_id)
            return clearance if clearance is not None and self.can_submit(issue_id, clearance) else None
        clearance = DuplicateClearance._from_registry(
            issue_id=issue_id,
            possible_duplicate=False,
            cluster_members=self._cluster_members(issue_id),
            cluster_version_hash=self._cluster_version_hash(issue_id),
            evidence_url=None,
            token=_DUPLICATE_CLEARANCE_TOKEN,
        )
        self._duplicate_clearances[issue_id] = clearance
        return clearance

    def resolve_possible_duplicate(self, issue_id: str, *, distinct: bool, evidence_url: str) -> None:
        if not self.possible_duplicate_ids(issue_id):
            raise ValueError("issue is not in a possible-duplicate cluster")
        if not evidence_url.strip():
            raise ValueError("possible-duplicate resolution requires an evidence URL")
        self._invalidate_clearance(issue_id)
        record = self.records[issue_id]
        self.records[issue_id] = VersionedIssueRecord(**{**record.__dict__, "status_evidence_url": evidence_url})
        if distinct:
            self._duplicate_clearances[issue_id] = DuplicateClearance._from_registry(
                issue_id=issue_id,
                possible_duplicate=True,
                cluster_members=self._cluster_members(issue_id),
                cluster_version_hash=self._cluster_version_hash(issue_id),
                evidence_url=evidence_url,
                token=_DUPLICATE_CLEARANCE_TOKEN,
            )

    def mark_submitted(
        self,
        issue_id: str,
        *,
        duplicate_clearance: DuplicateClearance | None,
        evidence_url: str,
        submitted_at: str,
    ) -> None:
        record = self.records[issue_id]
        if record.stage is not PipelineStage.SUBMIT_READY:
            raise ValueError("only SUBMIT_READY can transition to SUBMITTED")
        if not self.can_submit(issue_id, duplicate_clearance):
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
            if age < observation_days:
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
        if record.family_id:
            return (
                record.dbms,
                record.dbms_version,
                record.rule_id,
                record.site_class,
                record.family_id,
                record.family_id,
            )
        return (
            record.dbms,
            record.dbms_version,
            record.rule_id,
            record.site_class,
            record.normalized_pattern,
            record.normalized_predicate,
        )

    def _cluster_members(self, issue_id: str) -> tuple[str, ...]:
        return tuple(sorted((issue_id, *self.possible_duplicate_ids(issue_id))))

    def _cluster_version_hash(self, issue_id: str) -> str:
        members = self._cluster_members(issue_id)
        payload = tuple(
            (
                member,
                self.records[member].pair_hash,
                self.records[member].dbms_version,
                self.records[member].rule_version,
            )
            for member in members
        )
        return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()

    def _invalidate_clearance(self, issue_id: str) -> None:
        clearance = self._duplicate_clearances.pop(issue_id, None)
        if clearance is not None:
            clearance._invalidate()

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
        formal_fields = {
            "dbms_fingerprint": record.dbms_fingerprint,
            "family_id": record.family_id,
            "family_version_hash": record.family_version_hash,
            "family_decision_hash": record.family_decision_hash,
            "exact_signature": record.exact_signature,
            "fingerprint_hash": record.fingerprint_hash,
            "submission_path": record.submission_path,
            "reduced_phenotype": record.reduced_phenotype,
        }
        if any(formal_fields.values()):
            missing_formal = tuple(
                name for name, value in formal_fields.items() if not str(value).strip()
            )
            if missing_formal or record.known_regression_match:
                raise ValueError(
                    "incomplete formal issue family provenance: "
                    + ", ".join(missing_formal or ("known_regression_match",))
                )
            expected_hash = record.exact_signature
        else:
            expected_hash = canonical_reduced_pair_hash(
                record.reduced_query_a or "", record.reduced_query_b or ""
            )
        if record.pair_hash != expected_hash:
            raise ValueError("pair_hash does not match normalized reduced query pair")


def canonical_reduced_pair_hash(query_a: str, query_b: str) -> str:
    payload = f"{alpha_normalize_query(query_a)}\0{alpha_normalize_query(query_b)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
