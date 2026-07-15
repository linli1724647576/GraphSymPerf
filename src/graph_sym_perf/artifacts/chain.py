from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Mapping

from graph_sym_perf.artifacts.schema import ArtifactRecord, ArtifactReference
from graph_sym_perf.artifacts.store import (
    ContentAddressedStore,
    load_artifact_record,
    verify_issue_hash_chain,
)
from graph_sym_perf.validation.models import (
    IssueValidationReport,
    SubmitReadyIssue,
    canonical_validation_report_projection,
)
from graph_sym_perf.validation.promotion import submit_ready_issue_is_valid
from graph_sym_perf.core import stable_json_dumps


@dataclass(frozen=True)
class StoredArtifactRecord:
    record: ArtifactRecord
    reference: ArtifactReference


class ArtifactChainWriter:
    """Persist only typed validation/promotion outputs with resolvable hash links."""

    def __init__(self, store: ContentAddressedStore) -> None:
        self.store = store

    def persist_validation(
        self,
        report: IssueValidationReport,
        *,
        candidate_ref: ArtifactReference | dict[str, object],
        environment: Mapping[str, object],
    ) -> StoredArtifactRecord:
        if not isinstance(report, IssueValidationReport) or report.verify_integrity():
            raise TypeError("validation artifact requires an integrity-checked IssueValidationReport")
        candidate = load_artifact_record(self.store, candidate_ref)
        if getattr(candidate.level, "value", candidate.level) != "candidate":
            raise ValueError("validation candidate_ref must reference a candidate artifact")
        if candidate.payload.get("candidate_hash") != report.candidate_hash:
            raise ValueError("candidate reference does not bind the validation report")
        projection = canonical_validation_report_projection(report)
        record = ArtifactRecord.seal_validation(
            {
                **projection,
                "candidate_ref": _reference_payload(candidate_ref),
                "environment": dict(environment),
            }
        )
        reference = self.store.put_json(f"validation-{record.payload['validation_hash']}.json", record.to_dict())
        return StoredArtifactRecord(record, reference)

    def persist_issue(
        self,
        issue: SubmitReadyIssue,
        *,
        original_validation_ref: ArtifactReference | dict[str, object],
        final_validation_ref: ArtifactReference | dict[str, object],
        reproducer_ref: ArtifactReference | dict[str, object],
        explanation_ref: ArtifactReference | dict[str, object] | None = None,
    ) -> StoredArtifactRecord:
        if not isinstance(issue, SubmitReadyIssue) or not submit_ready_issue_is_valid(issue):
            raise TypeError("issue artifact requires a valid SubmitReadyIssue")
        original_record = load_artifact_record(self.store, original_validation_ref)
        final_record = load_artifact_record(self.store, final_validation_ref)
        _require_report_binding(original_record, issue.validation)
        if issue.reduction.final_validation is None:
            raise ValueError("SubmitReadyIssue has no reduced validation report")
        _require_report_binding(final_record, issue.reduction.final_validation)
        reduced_a = issue.reduction.reduced_pair.query_a
        reduced_b = issue.reduction.reduced_pair.query_b
        reduced_hashes = {
            "query_a": hashlib.sha256(reduced_a.encode("utf-8")).hexdigest(),
            "query_b": hashlib.sha256(reduced_b.encode("utf-8")).hexdigest(),
        }
        reduction_payload = {
            "candidate_hash": issue.validation.candidate_hash,
            "reduced_candidate_hash": issue.reduction.reduced_candidate_hash,
            "reduced_content_hashes": reduced_hashes,
            "status": issue.reduction.status.value,
            "stop_reason": issue.reduction.stop_reason,
            "minimality_proof": asdict(issue.reduction.minimality_proof),
            "final_evidence": asdict(issue.reduction.final_evidence),
            "validation_policy_hash": issue.reduction.validation_policy_hash,
            "certificate_hash": issue.reduction.certificate_hash,
            "original_validation_hash": issue.reduction.original_validation_hash,
            "reduced_validation_hash": issue.reduction.reduced_validation_hash,
            "original_validation_ref": _reference_payload(original_validation_ref),
            "reduced_validation_ref": _reference_payload(final_validation_ref),
        }
        reduction_ref = self.store.put_json(
            f"reduction-{issue.reduction.reduced_candidate_hash}.json",
            reduction_payload,
        )
        payload: dict[str, object] = {
            "issue_id": issue.issue_id,
            "family_id": issue.family_id,
            "rule_id": issue.certificate.rule_id,
            "stage": issue.stage.value,
            "reduction_status": issue.reduction.status.value,
            "certificate": asdict(issue.certificate),
            "duplicate_clearance": {
                "issue_id": issue.duplicate_clearance.issue_id,
                "possible_duplicate": issue.duplicate_clearance.possible_duplicate,
                "cluster_members": issue.duplicate_clearance.cluster_members,
                "cluster_version_hash": issue.duplicate_clearance.cluster_version_hash,
                "evidence_url": issue.duplicate_clearance.evidence_url,
                "registry_signature": issue.duplicate_clearance.registry_signature,
                "family_id": issue.duplicate_clearance.family_id,
                "family_version_hash": issue.duplicate_clearance.family_version_hash,
                "family_decision_hash": issue.duplicate_clearance.family_decision_hash,
            },
            "family_decision": {
                "issue_id": issue.family_decision.issue_id,
                "owner_issue_id": issue.family_decision.owner_issue_id,
                "family_id": issue.family_decision.family_id,
                "fingerprint": asdict(issue.family_decision.fingerprint),
                "match_kind": issue.family_decision.match_kind.value,
                "matched_issue_id": issue.family_decision.matched_issue_id,
                "known_regression_match": issue.family_decision.known_regression_match,
                "known_regression_case_id": issue.family_decision.known_regression_case_id,
                "is_new_family": issue.family_decision.is_new_family,
                "family_members": issue.family_decision.family_members,
                "family_version_hash": issue.family_decision.family_version_hash,
                "decision_hash": issue.family_decision.decision_hash,
            },
            "validated_ratio": issue.reduction.final_validation.validated_ratio,
            "dbms_version": issue.validation.manifest.dbms_version,
            "candidate_hash": issue.validation.candidate_hash,
            "reduced_candidate_hash": issue.reduction.reduced_candidate_hash,
            "reduced_query_a": reduced_a,
            "reduced_query_b": reduced_b,
            "reduced_content_hashes": reduced_hashes,
            "reproducer_ref": _reference_payload(reproducer_ref),
            "original_validation_ref": _reference_payload(original_validation_ref),
            "final_validation_ref": _reference_payload(final_validation_ref),
            "original_validation_hash": issue.reduction.original_validation_hash,
            "reduced_validation_hash": issue.reduction.reduced_validation_hash,
            "reduction_ref": asdict(reduction_ref),
        }
        if explanation_ref is not None:
            payload["explanation_ref"] = _reference_payload(explanation_ref)
        record = ArtifactRecord.seal_issue(payload)
        verify_issue_hash_chain(self.store, record)
        reference = self.store.put_json(f"issue-{issue.issue_id}.json", record.to_dict())
        return StoredArtifactRecord(record, reference)


def _reference_payload(reference: ArtifactReference | dict[str, object]) -> dict[str, object]:
    return asdict(reference) if isinstance(reference, ArtifactReference) else dict(reference)


def _require_report_binding(record: ArtifactRecord, report: IssueValidationReport) -> None:
    expected = canonical_validation_report_projection(report)
    for name, value in expected.items():
        if stable_json_dumps(record.payload.get(name)) != stable_json_dumps(value):
            raise ValueError(f"validation reference does not bind typed report field: {name}")


__all__ = ["ArtifactChainWriter", "StoredArtifactRecord"]
