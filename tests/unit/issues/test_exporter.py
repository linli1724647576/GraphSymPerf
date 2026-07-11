import json

import pytest

from graph_sym_perf.issues.exporter import export_issue
from graph_sym_perf.validation.models import PipelineStage
from graph_sym_perf.validation.promotion import promote
from tests.unit.validation.test_promotion import _certificate, _duplicate_clearance, _reduction, _report


def test_exporter_rejects_untyped_raw_candidate(tmp_path) -> None:
    with pytest.raises(TypeError):
        export_issue({"stage": "submit_ready"}, tmp_path / "issue.json")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        export_issue(_report(), tmp_path / "validation.json")  # type: ignore[arg-type]


def test_exporter_rechecks_runtime_gates_even_for_a_forged_submit_ready_instance(tmp_path) -> None:
    issue = promote(_report(), reduction_report=_reduction(), certificate=_certificate(), duplicate_clearance=_duplicate_clearance())
    assert issue is not None
    object.__setattr__(issue, "stage", PipelineStage.SCREENED)

    with pytest.raises(ValueError, match="promotion gates"):
        export_issue(issue, tmp_path / "forged.json")


def test_exporter_serializes_only_a_fully_gated_submit_ready_issue(tmp_path) -> None:
    issue = promote(_report(), reduction_report=_reduction(), certificate=_certificate(), duplicate_clearance=_duplicate_clearance())
    assert issue is not None

    path = export_issue(issue, tmp_path / "issue.json")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["issue_id"] == issue.issue_id
    assert payload["stage"] == "submit_ready"
    assert payload["reduction_status"] == "reduced_valid"
    assert payload["validated_ratio"] == issue.reduction.final_validation.validated_ratio
    assert payload["dbms_version"] == issue.validation.manifest.dbms_version
    assert payload["reduced_original"] == issue.reduction.reduced_pair.query_a
    assert payload["reduced_transformed"] == issue.reduction.reduced_pair.query_b


def test_exporter_rejects_a_clearance_for_a_different_issue_identity(tmp_path) -> None:
    issue = promote(
        _report(),
        reduction_report=_reduction(),
        certificate=_certificate(),
        duplicate_clearance=_duplicate_clearance(),
    )
    assert issue is not None
    object.__setattr__(issue, "duplicate_clearance", _duplicate_clearance(issue_id="wrong-issue"))

    with pytest.raises(ValueError, match="promotion gates"):
        export_issue(issue, tmp_path / "wrong-clearance.json")
