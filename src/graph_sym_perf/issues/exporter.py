"""Export boundary: only a typed SubmitReadyIssue can cross it."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.validation.models import SubmitReadyIssue
from graph_sym_perf.validation.promotion import submit_ready_issue_is_valid


def canonical_typed_export_projection(
    issue_payload: Mapping[str, object],
) -> dict[str, object]:
    """Project one sealed issue payload onto the only developer-export schema."""

    family = issue_payload.get("family_decision")
    certificate = issue_payload.get("certificate")
    fingerprint = family.get("fingerprint") if isinstance(family, Mapping) else None
    if (
        not isinstance(family, Mapping)
        or not isinstance(certificate, Mapping)
        or not isinstance(fingerprint, Mapping)
    ):
        raise ValueError("sealed issue cannot produce a canonical typed export")
    return {
        "issue_id": issue_payload.get("issue_id"),
        "family_id": issue_payload.get("family_id"),
        "stage": issue_payload.get("stage"),
        "dbms_version": issue_payload.get("dbms_version"),
        "rule_id": certificate.get("rule_id"),
        "validated_ratio": issue_payload.get("validated_ratio"),
        "reduction_status": issue_payload.get("reduction_status"),
        "reduced_original": issue_payload.get("reduced_query_a"),
        "reduced_transformed": issue_payload.get("reduced_query_b"),
        "exact_signature": fingerprint.get("exact"),
        "structural_signature": fingerprint.get("structural"),
        "submission_path": fingerprint.get("submission_path"),
        "reduced_validation_phenotype": fingerprint.get(
            "reduced_validation_phenotype"
        ),
        "family_version_hash": family.get("family_version_hash"),
        "family_decision_hash": family.get("decision_hash"),
        "original_validation_hash": issue_payload.get("original_validation_hash"),
        "reduced_validation_hash": issue_payload.get("reduced_validation_hash"),
        "original_validation_ref": issue_payload.get("original_validation_ref"),
        "reduced_validation_ref": issue_payload.get("final_validation_ref"),
    }


def export_issue(
    issue: SubmitReadyIssue,
    destination: str | Path,
    *,
    original_validation_ref: ArtifactReference,
    reduced_validation_ref: ArtifactReference,
) -> Path:
    if not isinstance(issue, SubmitReadyIssue):
        raise TypeError("issue exporter accepts SubmitReadyIssue only")
    if not submit_ready_issue_is_valid(issue):
        raise ValueError("SubmitReadyIssue no longer satisfies promotion gates")
    if not isinstance(original_validation_ref, ArtifactReference) or not isinstance(
        reduced_validation_ref, ArtifactReference
    ):
        raise TypeError("issue exporter requires both typed validation references")
    if original_validation_ref == reduced_validation_ref:
        raise ValueError("issue exporter requires distinct validation references")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    projection = canonical_typed_export_projection(
        {
            "issue_id": issue.issue_id,
            "family_id": issue.family_id,
            "stage": issue.stage.value,
            "dbms_version": issue.validation.manifest.dbms_version,
            "certificate": {"rule_id": issue.certificate.rule_id},
            "validated_ratio": issue.reduction.final_validation.validated_ratio,
            "reduction_status": issue.reduction.status.value,
            "reduced_query_a": issue.reduction.reduced_pair.query_a,
            "reduced_query_b": issue.reduction.reduced_pair.query_b,
            "family_decision": {
                "fingerprint": {
                    "exact": issue.family_decision.fingerprint.exact,
                    "structural": issue.family_decision.fingerprint.structural,
                    "submission_path": issue.family_decision.fingerprint.submission_path,
                    "reduced_validation_phenotype": (
                        issue.family_decision.fingerprint.reduced_validation_phenotype
                    ),
                },
                "family_version_hash": issue.family_decision.family_version_hash,
                "decision_hash": issue.family_decision.decision_hash,
            },
            "original_validation_hash": issue.reduction.original_validation_hash,
            "reduced_validation_hash": issue.reduction.reduced_validation_hash,
            "original_validation_ref": original_validation_ref.__dict__,
            "final_validation_ref": reduced_validation_ref.__dict__,
        }
    )
    payload = (
        json.dumps(
            projection,
            indent=2,
        )
        + "\n"
    )
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return path


__all__ = ["canonical_typed_export_projection", "export_issue"]
