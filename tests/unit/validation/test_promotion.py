from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    EquivalenceEvidence,
    IssueValidationReport,
    PipelineStage,
    SafetyCertificate,
)
from graph_sym_perf.validation.promotion import promote


def _evidence() -> RuntimeEvidence:
    return RuntimeEvidence(RuntimeVerdict.PASS, "a", "b", (), 4.0, 30.0, 3.0, 5.0, 1.0, 0.01, ())


def test_profile_or_plan_like_metadata_cannot_promote_without_validation_and_reduction() -> None:
    report = IssueValidationReport(
        stage=PipelineStage.SCREENED,
        equivalence=EquivalenceEvidence(True, True, ()),
        sessions=(),
        discovery_ratio=100.0,
        validated_ratio=None,
        magnitude_stability="unvalidated",
        reason_codes=(),
        metadata={"profile_supported": True, "pmc_novelty": 1000, "plan_changed": True},
    )
    certificate = SafetyCertificate(rule_id="pattern_order_symmetry", tier="core", proof_version="v1", site_class="match")

    assert promote(report, reduction_status="reduced_valid", certificate=certificate) is None


def test_only_validation_passed_reduced_core_certificate_becomes_submit_ready() -> None:
    report = IssueValidationReport(
        stage=PipelineStage.VALIDATION_PASSED,
        equivalence=EquivalenceEvidence(True, True, ()),
        sessions=(),
        discovery_ratio=8.0,
        validated_ratio=4.0,
        magnitude_stability="stable",
        reason_codes=(),
        metadata={},
    )
    certificate = SafetyCertificate(rule_id="pattern_order_symmetry", tier="core", proof_version="v1", site_class="match")

    issue = promote(report, reduction_status="reduced_valid", certificate=certificate)
    assert issue is not None
    assert issue.stage is PipelineStage.SUBMIT_READY
    assert issue.developer_status is DeveloperStatus.WAITING_REPLY
