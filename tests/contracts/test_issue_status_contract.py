"""Baseline guard against mixing internal evidence with developer outcomes."""

from graph_sym_perf.core.status import ClaimLevel, DeveloperStatus, ToolSupportLevel


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
