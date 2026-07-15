"""Export boundary: only a typed SubmitReadyIssue can cross it."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, fields
from pathlib import Path

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.issues.dedup import (
    IssueFingerprint,
    compact_family_decision_payload,
    serialized_family_decision_is_valid,
)
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.models import SubmitReadyIssue
from graph_sym_perf.validation.promotion import submit_ready_issue_is_valid


_TYPED_EXPORT_FIELDS = frozenset(
    {
        "issue_id",
        "family_id",
        "stage",
        "dbms_version",
        "rule_id",
        "validated_ratio",
        "reduction_status",
        "reduced_original",
        "reduced_transformed",
        "exact_signature",
        "structural_signature",
        "submission_path",
        "reduced_validation_phenotype",
        "family_version_hash",
        "family_decision_hash",
        "original_validation_hash",
        "reduced_validation_hash",
        "original_validation_ref",
        "reduced_validation_ref",
    }
)
_ARTIFACT_REFERENCE_FIELDS = frozenset(
    {"content_hash", "relative_path", "size_bytes", "media_type", "schema_version"}
)
_COMPACT_FAMILY_FIELDS = frozenset(
    {
        "issue_id",
        "owner_issue_id",
        "family_id",
        "fingerprint",
        "match_kind",
        "matched_issue_id",
        "known_regression_match",
        "known_regression_case_id",
        "is_new_family",
        "member_count",
        "member_proof_root",
        "registry_proof_ref",
        "family_version_hash",
        "decision_hash",
    }
)
_LEGACY_FAMILY_FIELDS = frozenset(
    {
        "issue_id",
        "owner_issue_id",
        "family_id",
        "fingerprint",
        "match_kind",
        "matched_issue_id",
        "known_regression_match",
        "known_regression_case_id",
        "is_new_family",
        "family_members",
        "family_fingerprints",
        "family_version_hash",
        "decision_hash",
    }
)
_FINGERPRINT_FIELDS = frozenset(field.name for field in fields(IssueFingerprint))
_CERTIFICATE_FIELDS = frozenset(field.name for field in fields(SafetyCertificate))


def canonical_typed_export_projection(
    issue_payload: Mapping[str, object],
) -> dict[str, object]:
    """Project one sealed issue payload onto the only developer-export schema."""

    if not isinstance(issue_payload, Mapping):
        raise ValueError("canonical typed export requires a sealed issue mapping")
    family = issue_payload.get("family_decision")
    certificate = issue_payload.get("certificate")
    checked_family = _validate_family(family)
    checked_certificate = _validate_certificate(certificate)
    fingerprint = checked_family["fingerprint"]
    projection = {
        "issue_id": issue_payload.get("issue_id"),
        "family_id": issue_payload.get("family_id"),
        "stage": issue_payload.get("stage"),
        "dbms_version": issue_payload.get("dbms_version"),
        "rule_id": checked_certificate.rule_id,
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
        "family_version_hash": checked_family.get("family_version_hash"),
        "family_decision_hash": checked_family.get("decision_hash"),
        "original_validation_hash": issue_payload.get("original_validation_hash"),
        "reduced_validation_hash": issue_payload.get("reduced_validation_hash"),
        "original_validation_ref": issue_payload.get("original_validation_ref"),
        "reduced_validation_ref": issue_payload.get("final_validation_ref"),
    }
    _validate_canonical_typed_export(projection)
    if (
        checked_family["issue_id"] != projection["issue_id"]
        or checked_family["owner_issue_id"] != projection["issue_id"]
        or checked_family["family_id"] != projection["family_id"]
        or fingerprint["dbms_version"] != projection["dbms_version"]
        or fingerprint["canonical_rule_id"] != projection["rule_id"]
        or fingerprint["reduced_query_a"] != projection["reduced_original"]
        or fingerprint["reduced_query_b"] != projection["reduced_transformed"]
    ):
        raise ValueError("canonical typed export source fields are inconsistent")
    return projection


def canonical_typed_export_dumps(value: Mapping[str, object]) -> str:
    """Validate and deterministically encode the exact typed-export schema."""

    _validate_canonical_typed_export(value)
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical typed export is not strict JSON") from exc


def _validate_canonical_typed_export(value: object) -> None:
    mapping = _exact_mapping(value, _TYPED_EXPORT_FIELDS, "typed export")
    for name in (
        "issue_id",
        "family_id",
        "dbms_version",
        "rule_id",
        "reduced_original",
        "reduced_transformed",
        "exact_signature",
        "reduced_validation_phenotype",
        "family_version_hash",
        "family_decision_hash",
        "original_validation_hash",
        "reduced_validation_hash",
    ):
        _nonempty_string(mapping.get(name), name)
    if mapping.get("stage") != "submit_ready":
        raise ValueError("typed export stage must be submit_ready")
    if mapping.get("reduction_status") != "reduced_valid":
        raise ValueError("typed export reduction_status must be reduced_valid")
    ratio = mapping.get("validated_ratio")
    if type(ratio) is not float or not math.isfinite(ratio):
        raise ValueError("typed export validated_ratio must be a finite float")
    structural = mapping.get("structural_signature")
    if structural is not None and not isinstance(structural, str):
        raise ValueError("typed export structural_signature must be text or null")
    if mapping.get("submission_path") not in {"relative", "absolute"}:
        raise ValueError("typed export submission_path is invalid")
    _validate_artifact_reference(
        mapping.get("original_validation_ref"),
        "original_validation_ref",
    )
    _validate_artifact_reference(
        mapping.get("reduced_validation_ref"),
        "reduced_validation_ref",
    )


def _validate_artifact_reference(value: object, name: str) -> dict[str, object]:
    mapping = _exact_mapping(value, _ARTIFACT_REFERENCE_FIELDS, name)
    if (
        not isinstance(mapping.get("content_hash"), str)
        or not isinstance(mapping.get("relative_path"), str)
        or type(mapping.get("size_bytes")) is not int
        or not isinstance(mapping.get("media_type"), str)
        or type(mapping.get("schema_version")) is not int
    ):
        raise ValueError(f"{name} has ambiguous field types")
    try:
        ArtifactReference(**mapping)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc
    return mapping


def _validate_family(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("family_decision must be an object")
    supplied = set(value)
    if supplied == _COMPACT_FAMILY_FIELDS:
        mapping = dict(value)
        compact = True
    elif supplied == _LEGACY_FAMILY_FIELDS:
        mapping = dict(value)
        compact = False
    else:
        raise ValueError("family_decision has missing or unknown fields")
    fingerprint = _validate_fingerprint(mapping.get("fingerprint"))
    for name in (
        "issue_id",
        "owner_issue_id",
        "family_id",
        "match_kind",
        "family_version_hash",
        "decision_hash",
    ):
        _nonempty_string(mapping.get(name), f"family_decision.{name}")
    if (
        mapping.get("matched_issue_id") is not None
        or mapping.get("known_regression_case_id") is not None
        or type(mapping.get("known_regression_match")) is not bool
        or type(mapping.get("is_new_family")) is not bool
    ):
        raise ValueError("family_decision has ambiguous nullable/bool fields")
    if compact:
        _nonempty_string(
            mapping.get("member_proof_root"),
            "family_decision.member_proof_root",
        )
        if type(mapping.get("member_count")) is not int:
            raise ValueError("family_decision member_count must be int")
        proof_ref = _exact_mapping(
            mapping.get("registry_proof_ref"),
            frozenset({"schema", "registry_rel", "review_rel"}),
            "family_decision.registry_proof_ref",
        )
        if any(not isinstance(item, str) or not item for item in proof_ref.values()):
            raise ValueError("family_decision.registry_proof_ref must contain text")
    else:
        _validate_legacy_family_proof(mapping)
    if not serialized_family_decision_is_valid(mapping):
        raise ValueError("family_decision does not verify")
    mapping["fingerprint"] = fingerprint
    return mapping


def _validate_legacy_family_proof(mapping: Mapping[str, object]) -> None:
    members = mapping.get("family_members")
    fingerprints = mapping.get("family_fingerprints")
    if (
        not isinstance(members, list | tuple)
        or not isinstance(fingerprints, list | tuple)
        or not members
        or not fingerprints
    ):
        raise ValueError("legacy family proof must contain member sequences")
    for member in members:
        if (
            not isinstance(member, list | tuple)
            or len(member) != 2
            or any(not isinstance(item, str) for item in member)
        ):
            raise ValueError("legacy family member entry is invalid")
    for member in fingerprints:
        if (
            not isinstance(member, list | tuple)
            or len(member) != 2
            or not isinstance(member[0], str)
        ):
            raise ValueError("legacy family fingerprint entry is invalid")
        _validate_fingerprint(member[1])


def _validate_fingerprint(value: object) -> dict[str, object]:
    mapping = _exact_mapping(value, _FINGERPRINT_FIELDS, "family fingerprint")
    for name in _FINGERPRINT_FIELDS - {"structural", "normalized_pair"}:
        if not isinstance(mapping.get(name), str):
            raise ValueError(f"family fingerprint {name} must be text")
    structural = mapping.get("structural")
    if structural is not None and not isinstance(structural, str):
        raise ValueError("family fingerprint structural must be text or null")
    normalized_pair = mapping.get("normalized_pair")
    if normalized_pair is not None and (
        not isinstance(normalized_pair, list | tuple)
        or len(normalized_pair) != 2
        or any(not isinstance(item, str) for item in normalized_pair)
    ):
        raise ValueError("family fingerprint normalized_pair is invalid")
    normalized = dict(mapping)
    if normalized_pair is not None:
        normalized["normalized_pair"] = tuple(normalized_pair)
    try:
        fingerprint = IssueFingerprint(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("family fingerprint is invalid") from exc
    if not fingerprint.verify():
        raise ValueError("family fingerprint does not verify")
    return mapping


def _validate_certificate(value: object) -> SafetyCertificate:
    mapping = _exact_mapping(value, _CERTIFICATE_FIELDS, "certificate")
    for name in _CERTIFICATE_FIELDS - {
        "submit_eligible",
        "variables_in_scope",
        "preconditions",
    }:
        if not isinstance(mapping.get(name), str):
            raise ValueError(f"certificate {name} must be text")
    if type(mapping.get("submit_eligible")) is not bool:
        raise ValueError("certificate submit_eligible must be bool")
    normalized = dict(mapping)
    for name in ("variables_in_scope", "preconditions"):
        sequence = mapping.get(name)
        if (
            not isinstance(sequence, list | tuple)
            or any(not isinstance(item, str) for item in sequence)
        ):
            raise ValueError(f"certificate {name} must be a string sequence")
        normalized[name] = tuple(sequence)
    try:
        return SafetyCertificate(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("certificate does not verify") from exc


def _exact_mapping(
    value: object,
    expected: frozenset[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} has missing or unknown fields")
    return dict(value)


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    return value


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
            "certificate": asdict(issue.certificate),
            "validated_ratio": issue.reduction.final_validation.validated_ratio,
            "reduction_status": issue.reduction.status.value,
            "reduced_query_a": issue.reduction.reduced_pair.query_a,
            "reduced_query_b": issue.reduction.reduced_pair.query_b,
            "family_decision": compact_family_decision_payload(
                issue.family_decision
            ),
            "original_validation_hash": issue.reduction.original_validation_hash,
            "reduced_validation_hash": issue.reduction.reduced_validation_hash,
            "original_validation_ref": asdict(original_validation_ref),
            "final_validation_ref": asdict(reduced_validation_ref),
        }
    )
    payload = canonical_typed_export_dumps(projection) + "\n"
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


__all__ = [
    "canonical_typed_export_dumps",
    "canonical_typed_export_projection",
    "export_issue",
]
