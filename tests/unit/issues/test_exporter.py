import json
import copy

import pytest

from graph_sym_perf.artifacts.schema import ArtifactReference
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
