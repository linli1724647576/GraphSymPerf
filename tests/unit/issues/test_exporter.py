import copy
import json
from dataclasses import asdict

import pytest

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.issues import exporter
from graph_sym_perf.issues.dedup import compact_family_decision_payload
from graph_sym_perf.issues.exporter import export_issue
from graph_sym_perf.validation.models import PipelineStage
from graph_sym_perf.validation.promotion import promote
from tests.unit.validation.test_promotion import (
    _certificate,
    _duplicate_clearance,
    _family_decision,
    _reduction,
    _report,
)


def _issue():
    report = _report()
    reduction = _reduction()
    decision = _family_decision(report, reduction)
    issue = promote(
        report,
        reduction_report=reduction,
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(family_decision=decision),
        family_decision=decision,
    )
    assert issue is not None
    return issue


def _refs() -> dict[str, ArtifactReference]:
    return {
        "original_validation_ref": ArtifactReference(
            "1" * 64, "objects/11/" + "1" * 64, 100, "application/json"
        ),
        "reduced_validation_ref": ArtifactReference(
            "2" * 64, "objects/22/" + "2" * 64, 100, "application/json"
        ),
    }


def _sealed_projection_source() -> dict[str, object]:
    issue = _issue()
    refs = _refs()
    return {
        "issue_id": issue.issue_id,
        "family_id": issue.family_id,
        "stage": issue.stage.value,
        "dbms_version": issue.validation.manifest.dbms_version,
        "certificate": asdict(issue.certificate),
        "validated_ratio": issue.reduction.final_validation.validated_ratio,
        "reduction_status": issue.reduction.status.value,
        "reduced_query_a": issue.reduction.reduced_pair.query_a,
        "reduced_query_b": issue.reduction.reduced_pair.query_b,
        "family_decision": compact_family_decision_payload(issue.family_decision),
        "original_validation_hash": issue.reduction.original_validation_hash,
        "reduced_validation_hash": issue.reduction.reduced_validation_hash,
        "original_validation_ref": asdict(refs["original_validation_ref"]),
        "final_validation_ref": asdict(refs["reduced_validation_ref"]),
    }


@pytest.mark.parametrize(
    "attack",
    (
        "ratio_bool",
        "ratio_nan",
        "ratio_inf",
        "ratio_negative_inf",
        "ratio_string",
        "reference_schema_bool",
        "reference_extra",
        "reference_missing",
        "reference_wrong_type",
        "family_extra",
        "family_missing",
        "family_wrong_type",
        "certificate_extra",
        "certificate_missing",
        "certificate_wrong_type",
        "fingerprint_extra",
        "fingerprint_missing",
        "fingerprint_wrong_type",
        "member_count_bool",
    ),
)
def test_canonical_typed_export_projection_rejects_ambiguous_types_and_shapes(
    attack,
) -> None:
    source = copy.deepcopy(_sealed_projection_source())
    family = source["family_decision"]
    certificate = source["certificate"]
    assert isinstance(family, dict)
    assert isinstance(certificate, dict)
    fingerprint = family["fingerprint"]
    assert isinstance(fingerprint, dict)
    if attack == "ratio_bool":
        source["validated_ratio"] = True
    elif attack == "ratio_nan":
        source["validated_ratio"] = float("nan")
    elif attack == "ratio_inf":
        source["validated_ratio"] = float("inf")
    elif attack == "ratio_negative_inf":
        source["validated_ratio"] = float("-inf")
    elif attack == "ratio_string":
        source["validated_ratio"] = "4.0"
    elif attack == "reference_schema_bool":
        source["original_validation_ref"]["schema_version"] = True
    elif attack == "reference_extra":
        source["original_validation_ref"]["extra"] = "forged"
    elif attack == "reference_missing":
        source["original_validation_ref"].pop("media_type")
    elif attack == "reference_wrong_type":
        source["original_validation_ref"]["size_bytes"] = "100"
    elif attack == "family_extra":
        family["extra"] = "forged"
    elif attack == "family_missing":
        family.pop("member_proof_root")
    elif attack == "family_wrong_type":
        source["family_decision"] = []
    elif attack == "certificate_extra":
        certificate["extra"] = "forged"
    elif attack == "certificate_missing":
        certificate.pop("proof_id")
    elif attack == "certificate_wrong_type":
        certificate["submit_eligible"] = 1
    elif attack == "fingerprint_extra":
        fingerprint["extra"] = "forged"
    elif attack == "fingerprint_missing":
        fingerprint.pop("query_a_hash")
    elif attack == "fingerprint_wrong_type":
        fingerprint["normalized_pair"] = ["only-one"]
    else:
        family["member_count"] = True

    with pytest.raises(ValueError):
        exporter.canonical_typed_export_projection(source)


@pytest.mark.parametrize(
    "attack",
    ("top_extra", "top_missing", "top_wrong_type", "nested_schema_bool", "nan"),
)
def test_canonical_typed_export_stable_serialization_rejects_noncanonical_payload(
    attack,
) -> None:
    projection = exporter.canonical_typed_export_projection(
        _sealed_projection_source()
    )
    if attack == "top_extra":
        projection["extra"] = "forged"
    elif attack == "top_missing":
        projection.pop("rule_id")
    elif attack == "top_wrong_type":
        projection["stage"] = 1
    elif attack == "nested_schema_bool":
        projection["reduced_validation_ref"]["schema_version"] = True
    else:
        projection["validated_ratio"] = float("nan")

    with pytest.raises(ValueError):
        exporter.canonical_typed_export_dumps(projection)


def test_exporter_rejects_untyped_raw_candidate(tmp_path) -> None:
    with pytest.raises(TypeError):
        export_issue({"stage": "submit_ready"}, tmp_path / "issue.json", **_refs())  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        export_issue(_report(), tmp_path / "validation.json", **_refs())  # type: ignore[arg-type]


def test_exporter_rechecks_runtime_gates_even_for_a_forged_submit_ready_instance(tmp_path) -> None:
    issue = _issue()
    object.__setattr__(issue, "stage", PipelineStage.SCREENED)

    with pytest.raises(ValueError, match="promotion gates"):
        export_issue(issue, tmp_path / "forged.json", **_refs())


def test_exporter_serializes_only_a_fully_gated_submit_ready_issue(tmp_path) -> None:
    issue = _issue()

    path = export_issue(issue, tmp_path / "issue.json", **_refs())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
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
    assert payload["issue_id"] == issue.issue_id
    assert payload["stage"] == "submit_ready"
    assert payload["reduction_status"] == "reduced_valid"
    assert payload["validated_ratio"] == issue.reduction.final_validation.validated_ratio
    assert payload["dbms_version"] == issue.validation.manifest.dbms_version
    assert payload["reduced_original"] == issue.reduction.reduced_pair.query_a
    assert payload["reduced_transformed"] == issue.reduction.reduced_pair.query_b
    assert payload["family_id"] == issue.family_id
    assert payload["exact_signature"] == issue.family_decision.fingerprint.exact
    assert payload["structural_signature"] == issue.family_decision.fingerprint.structural
    assert payload["submission_path"] == issue.family_decision.fingerprint.submission_path
    assert payload["family_decision_hash"] == issue.family_decision.decision_hash
    assert payload["original_validation_hash"] == issue.reduction.original_validation_hash
    assert payload["reduced_validation_hash"] == issue.reduction.reduced_validation_hash
    assert payload["original_validation_ref"]["content_hash"] == "1" * 64
    assert payload["reduced_validation_ref"]["content_hash"] == "2" * 64


def test_exporter_rejects_a_clearance_for_a_different_issue_identity(tmp_path) -> None:
    issue = _issue()
    object.__setattr__(issue, "duplicate_clearance", _duplicate_clearance(issue_id="wrong-issue"))

    with pytest.raises(ValueError, match="promotion gates"):
        export_issue(issue, tmp_path / "wrong-clearance.json", **_refs())


def test_exporter_rejects_a_tampered_family_decision(tmp_path) -> None:
    issue = _issue()
    forged = copy.copy(issue.family_decision)
    object.__setattr__(forged, "decision_hash", "0" * 64)
    object.__setattr__(issue, "family_decision", forged)

    with pytest.raises(ValueError, match="promotion gates"):
        export_issue(issue, tmp_path / "forged-family.json", **_refs())
