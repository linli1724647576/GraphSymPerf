"""Baseline guard against mixing pipeline statistics with developer outcomes."""

from graph_sym_perf.core.status import ClaimLevel, DeveloperStatus, ToolSupportLevel
from graph_sym_perf.validation.models import (
    DeveloperValidatedBug,
    DeveloperStatus as RuntimeFirstDeveloperStatus,
    PipelineStage,
    ReportedIssueReport,
    VersionedIssueRecord,
)


def test_developer_status_claim_level_and_tool_support_are_distinct_types() -> None:
    # ``fixed`` is intentionally shared as a human-readable value, but its
    # provenance is distinct: a maintainer outcome cannot be substituted for
    # an internal claim, and neither is a measurement support level.
    assert type(DeveloperStatus.FIXED) is not type(ClaimLevel.FIXED)
    assert type(DeveloperStatus.FIXED) is not type(ToolSupportLevel.NONE)
    assert type(ClaimLevel.FIXED) is not type(ToolSupportLevel.NONE)


def test_existing_public_issue_outcomes_are_not_tool_evidence() -> None:
    outcomes = {
        DeveloperStatus.FIXED,
        DeveloperStatus.CONFIRMED,
        DeveloperStatus.REPORTED,
        DeveloperStatus.REJECTED,
    }

    assert all(outcome.value not in {member.value for member in ToolSupportLevel} for outcome in outcomes)


def _record(issue_id: str, stage: PipelineStage, status: RuntimeFirstDeveloperStatus) -> VersionedIssueRecord:
    return VersionedIssueRecord(
        issue_id=issue_id,
        stage=stage,
        developer_status=status,
        dbms="neo4j",
        dbms_version="unknown",
        pair_hash=issue_id,
        direction="a_slower",
        rule_id="baseline",
        detector_version="baseline",
        validator_version="baseline",
        reducer_version="baseline",
    )


def test_reported_issue_reports_and_developer_validated_bugs_are_separate_counts() -> None:
    records = (
        _record("submitted-waiting", PipelineStage.SUBMITTED, RuntimeFirstDeveloperStatus.WAITING_REPLY),
        _record("fixed-not-submitted", PipelineStage.VALIDATION_PASSED, RuntimeFirstDeveloperStatus.FIXED),
        _record("submitted-confirmed", PipelineStage.SUBMITTED, RuntimeFirstDeveloperStatus.CONFIRMED),
    )

    reported_issue_reports = {record.issue_id for record in records if record.stage is PipelineStage.SUBMITTED}
    developer_validated_bugs = {
        record.issue_id
        for record in records
        if record.developer_status in {RuntimeFirstDeveloperStatus.CONFIRMED, RuntimeFirstDeveloperStatus.FIXED}
    }

    assert reported_issue_reports == {"submitted-waiting", "submitted-confirmed"}
    assert developer_validated_bugs == {"fixed-not-submitted", "submitted-confirmed"}
    assert reported_issue_reports != developer_validated_bugs


def test_reported_and_developer_validated_records_enforce_independent_gates() -> None:
    submitted_waiting = _record(
        "submitted-waiting",
        PipelineStage.SUBMITTED,
        RuntimeFirstDeveloperStatus.WAITING_REPLY,
    )
    fixed_not_submitted = _record(
        "fixed-not-submitted",
        PipelineStage.VALIDATION_PASSED,
        RuntimeFirstDeveloperStatus.FIXED,
    )

    assert type(PipelineStage.SUBMITTED) is not type(RuntimeFirstDeveloperStatus.CONFIRMED)
    assert ReportedIssueReport.from_record(submitted_waiting).issue_id == "submitted-waiting"
    assert DeveloperValidatedBug.from_record(fixed_not_submitted).issue_id == "fixed-not-submitted"

    try:
        DeveloperValidatedBug.from_record(submitted_waiting)
    except ValueError as exc:
        assert "CONFIRMED or FIXED" in str(exc)
    else:
        raise AssertionError("waiting-reply report must not become a developer-validated bug")

    try:
        ReportedIssueReport.from_record(fixed_not_submitted)
    except ValueError as exc:
        assert "SUBMITTED" in str(exc)
    else:
        raise AssertionError("a developer outcome must not replace the SUBMITTED pipeline gate")
