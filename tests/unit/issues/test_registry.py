import pytest

from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord


def _record(issue_id: str) -> VersionedIssueRecord:
    return VersionedIssueRecord(issue_id, PipelineStage.SUBMIT_READY, DeveloperStatus.WAITING_REPLY, "neo4j", "5", "pair", "b_slower", "rule", "d", "v", "r")


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
