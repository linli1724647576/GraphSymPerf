import pytest

from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord


def _record(issue_id: str, **changes) -> VersionedIssueRecord:
    values = {
        "issue_id": issue_id,
        "stage": PipelineStage.SUBMIT_READY,
        "developer_status": DeveloperStatus.WAITING_REPLY,
        "dbms": "neo4j",
        "dbms_version": "5",
        "pair_hash": "pair",
        "direction": "b",
        "rule_id": "rule",
        "detector_version": "d",
        "validator_version": "v",
        "reducer_version": "r",
        "rule_version": "rule-v1",
        "discovered_at": "2026-07-10",
        "site_class": "match",
        "normalized_pattern": "MATCH_NODE",
        "normalized_predicate": "PROPERTY_GT_LITERAL",
        "reduced_query_a": "MATCH (n:A) RETURN n",
        "reduced_query_b": "MATCH (n:A) WHERE true RETURN n",
    }
    values.update(changes)
    from graph_sym_perf.issues.registry import canonical_reduced_pair_hash

    values["pair_hash"] = canonical_reduced_pair_hash(values["reduced_query_a"], values["reduced_query_b"])
    return VersionedIssueRecord(**values)


def test_registry_keeps_pipeline_and_developer_status_orthogonal() -> None:
    registry = IssueRegistry()
    registry.record(_record("one"))
    registry.update_developer_status("one", DeveloperStatus.CONFIRMED, "https://example.test/1")
    assert registry.records["one"].stage is PipelineStage.SUBMIT_READY
    assert registry.records["one"].developer_status is DeveloperStatus.CONFIRMED


def test_registry_rejects_exact_duplicate_but_not_different_dbms_version() -> None:
    registry = IssueRegistry()
    registry.record(_record("one"))
    with pytest.raises(ValueError):
        registry.record(_record("two"))

    registry.record(_record("three", dbms_version="6"))
    assert set(registry.records) == {"one", "three"}


def test_registry_requires_all_version_and_provenance_fields() -> None:
    registry = IssueRegistry()

    with pytest.raises(ValueError, match="rule_version"):
        registry.record(_record("missing-version", rule_version=""))
    with pytest.raises(ValueError, match="discovered_at"):
        registry.record(_record("missing-date", discovered_at=""))


def test_reported_and_developer_validated_counts_use_orthogonal_axes() -> None:
    registry = IssueRegistry()
    registry.record(_record("submitted-waiting"))
    clearance = registry.duplicate_clearance("submitted-waiting")
    registry.mark_submitted("submitted-waiting", duplicate_clearance=clearance, evidence_url="https://example.test/issues/1", submitted_at="2026-07-01")
    registry.record(
        _record(
            "fixed-not-submitted",
            stage=PipelineStage.VALIDATION_PASSED,
            developer_status=DeveloperStatus.FIXED,
            reduced_query_a="MATCH (m:B)-[:R]->(x) RETURN m",
            reduced_query_b="MATCH (m:B)-[:R]->(x) WHERE true RETURN m",
        )
    )

    assert registry.reported_issue_ids() == ("submitted-waiting",)
    assert registry.developer_validated_bug_ids() == ("fixed-not-submitted",)


def test_registry_forbids_direct_submitted_insertion() -> None:
    registry = IssueRegistry()

    with pytest.raises(ValueError, match="mark_submitted"):
        registry.record(_record("bypass", stage=PipelineStage.SUBMITTED))


def test_waiting_reply_within_90_days_is_right_censored_not_negative() -> None:
    registry = IssueRegistry()
    registry.record(_record("waiting"))
    clearance = registry.duplicate_clearance("waiting")
    registry.mark_submitted("waiting", duplicate_clearance=clearance, evidence_url="https://example.test/issues/2", submitted_at="2026-07-01")

    counts = registry.developer_outcome_counts(as_of="2026-07-10")

    assert counts["right_censored_waiting"] == 1
    assert counts["responded_negative"] == 0


def test_waiting_reply_is_not_right_censored_at_the_exact_90_day_boundary() -> None:
    registry = IssueRegistry()
    registry.record(_record("waiting-boundary"))
    clearance = registry.duplicate_clearance("waiting-boundary")
    registry.mark_submitted(
        "waiting-boundary",
        duplicate_clearance=clearance,
        evidence_url="https://example.test/issues/boundary",
        submitted_at="2026-04-11",
    )

    counts = registry.developer_outcome_counts(as_of="2026-07-10")

    assert counts["right_censored_waiting"] == 0
    assert counts["unresolved_beyond_window"] == 1
