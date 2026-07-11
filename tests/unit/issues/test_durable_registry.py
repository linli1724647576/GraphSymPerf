from types import SimpleNamespace
import json

import pytest

from graph_sym_perf.issues.durable_registry import promote_unique_issue
from graph_sym_perf.issues.commands import _has_current_possible_duplicate
from graph_sym_perf.issues.registry import canonical_reduced_pair_hash
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord


def _record() -> VersionedIssueRecord:
    query_a = "MATCH (n) RETURN n"
    query_b = "MATCH (n) WHERE true RETURN n"
    return VersionedIssueRecord(
        issue_id="issue-1",
        stage=PipelineStage.REDUCED_VALID,
        developer_status=DeveloperStatus.WAITING_REPLY,
        dbms="neo4j",
        dbms_version="dbms/1",
        pair_hash=canonical_reduced_pair_hash(query_a, query_b),
        direction="b",
        rule_id="constant_true_disjunction",
        detector_version="runtime-first-v3",
        validator_version="independent-two-session-v1",
        reducer_version="paired-ast-ddmin-v1",
        rule_version="kleene-or-true-v1",
        discovered_at="2026-07-11T00:00:00+00:00",
        site_class="where_boolean",
        normalized_pattern=query_a,
        normalized_predicate=query_b,
        reduced_query_a=query_a,
        reduced_query_b=query_b,
    )


def _receipt() -> dict[str, object]:
    return {
        "artifact_store": "shards/neo4j-000/artifacts",
        "export_path": "issues/submit-ready/issue-1.json",
        "export_sha256": "a" * 64,
        "issue_id": "issue-1",
        "issue_ref": {"content_hash": "b" * 64},
    }


def test_registry_commits_only_after_materialization_and_repairs_exact_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("graph_sym_perf.issues.durable_registry.deterministic_issue_id", lambda *_args: "issue-1")
    monkeypatch.setattr("graph_sym_perf.issues.durable_registry._record", lambda *_args, **_kwargs: _record())
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, **_kwargs: SimpleNamespace(issue_id="issue-1"),
    )
    calls = []

    def materialize(_issue):
        calls.append("materialized")
        return _receipt()

    first, first_status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        materialize=materialize,
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        materialize=materialize,
    )

    assert first is not None and repaired is not None
    assert first_status == "unique"
    assert repaired_status == "exact_duplicate_repaired"
    assert calls == ["materialized", "materialized"]
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "commits" / "issue-1.json").is_file()


def test_materialization_failure_never_creates_a_committed_registry_record(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("graph_sym_perf.issues.durable_registry.deterministic_issue_id", lambda *_args: "issue-1")
    monkeypatch.setattr("graph_sym_perf.issues.durable_registry._record", lambda *_args, **_kwargs: _record())
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, **_kwargs: SimpleNamespace(issue_id="issue-1"),
    )

    with pytest.raises(OSError, match="injected"):
        promote_unique_issue(
            tmp_path,
            validation=object(),
            reduction=object(),
            certificate=object(),
            dbms="neo4j",
            materialize=lambda _issue: (_ for _ in ()).throw(OSError("injected")),
        )

    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "commits" / "issue-1.json").exists()


def test_export_dedup_check_rebuilds_cluster_even_when_revocation_log_is_missing(tmp_path) -> None:
    root = tmp_path / "issues"
    root.mkdir()
    first = VersionedIssueRecord(**{**_record().__dict__, "stage": PipelineStage.SUBMIT_READY})
    second = VersionedIssueRecord(
        **{
            **_record().__dict__,
            "issue_id": "issue-2",
            "direction": "a",
            "stage": PipelineStage.REDUCED_VALID,
        }
    )

    def payload(record):
        value = {**record.__dict__}
        value["stage"] = record.stage.value
        value["developer_status"] = record.developer_status.value
        return value

    (root / "registry.jsonl").write_text(json.dumps(payload(first)) + "\n", encoding="utf-8")
    (root / "dedup_review.jsonl").write_text(json.dumps(payload(second)) + "\n", encoding="utf-8")

    assert _has_current_possible_duplicate(root, "issue-1")
    assert not (root / "revocations.jsonl").exists()
