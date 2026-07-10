"""Baseline guard against mixing pipeline statistics with developer outcomes."""

from graph_sym_perf.core.status import ClaimLevel, DeveloperStatus, ToolSupportLevel
from graph_sym_perf.validation.models import (
    DeveloperStatus as RuntimeFirstDeveloperStatus,
    PipelineStage,
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
