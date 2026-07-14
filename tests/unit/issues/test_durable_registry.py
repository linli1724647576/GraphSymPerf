from types import SimpleNamespace
import json
from pathlib import Path
import multiprocessing

import pytest

from graph_sym_perf.issues.durable_registry import (
    durable_family_credentials_are_current,
    promote_unique_issue,
)
from graph_sym_perf.issues.dedup import (
    FingerprintSubject,
    fingerprint_issue,
    known_regression_signature_from_manifest,
)
from graph_sym_perf.issues.commands import _has_current_possible_duplicate
from graph_sym_perf.issues.registry import canonical_reduced_pair_hash
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord
from tests.unit.validation.test_promotion import _certificate, _reduction, _report


REGRESSION_MANIFEST = (
    Path(__file__).parents[2] / "fixtures" / "regressions" / "neo4j_bug001" / "manifest.yaml"
)


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


def _dynamic_receipt(issue) -> dict[str, object]:
    return {
        **_receipt(),
        "issue_id": issue.issue_id,
        "export_path": f"issues/submit-ready/{issue.issue_id}.json",
    }


def _concurrent_promote(root: str, queue) -> None:
    report = _report()
    reduction = _reduction()
    issue, status = promote_unique_issue(
        root,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )
    queue.put((getattr(issue, "family_id", None), status))


def test_real_durable_promotion_replays_owner_with_current_family_version(tmp_path) -> None:
    report = _report()
    reduction = _reduction()

    first, first_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )

    assert first is not None and repaired is not None
    assert first_status == "unique_family"
    assert repaired_status == "family_owner_repaired"
    assert first.family_id == repaired.family_id
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    record = json.loads((tmp_path / "registry.jsonl").read_text(encoding="utf-8"))
    assert record["family_id"] == repaired.family_id
    assert record["family_version_hash"] == repaired.family_decision.family_version_hash


def test_formal_promotion_requires_manifest_and_dbms_matches_sealed_target(tmp_path) -> None:
    report = _report()
    reduction = _reduction()

    with pytest.raises(TypeError, match="known_regression_manifest"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            materialize=_dynamic_receipt,
        )
    with pytest.raises(ValueError, match="sealed target"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="memgraph",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=_dynamic_receipt,
        )


def test_known_bug001_is_recorded_only_as_diagnostic_and_never_promoted(
    monkeypatch,
    tmp_path,
) -> None:
    fixture = REGRESSION_MANIFEST.parent
    known = known_regression_signature_from_manifest(REGRESSION_MANIFEST)
    fingerprint = fingerprint_issue(
        FingerprintSubject(
            dbms="neo4j",
            dbms_version="current-version",
            dbms_fingerprint="c" * 64,
            canonical_rule_id="pattern_order_symmetry",
            reduced_query_a=(fixture / "fast_query.cypher").read_text(encoding="utf-8").strip(),
            reduced_query_b=(fixture / "slow_query.cypher").read_text(encoding="utf-8").strip(),
            runtime_direction="b",
            site_class="match_pattern_component",
            submission_path="relative",
            reduced_validation_phenotype="relative:stable:b",
        )
    )
    assert known.matches(fingerprint)
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: "known-rediscovery",
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._validated_fingerprint",
        lambda *_args, **_kwargs: fingerprint,
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("known issue promoted")),
    )

    issue, status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )

    assert issue is None and status == "known_regression_excluded"
    diagnostic = json.loads((tmp_path / "known_regressions.jsonl").read_text(encoding="utf-8"))
    assert diagnostic["known_regression_match"] is True
    assert diagnostic["case_id"] == "neo4j_bug001_pattern_order"
    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "commits").exists()


def test_concurrent_replay_under_flock_commits_one_family(tmp_path) -> None:
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = tuple(
        context.Process(target=_concurrent_promote, args=(str(tmp_path), queue))
        for _index in range(2)
    )
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    outcomes = tuple(queue.get(timeout=2) for _process in processes)
    assert {status for _family, status in outcomes} == {
        "unique_family",
        "family_owner_repaired",
    }
    assert len({family for family, _status in outcomes}) == 1
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def _fingerprint(*, changed: bool = False):
    return fingerprint_issue(
        FingerprintSubject(
            dbms="neo4j",
            dbms_version="dbms-version",
            dbms_fingerprint="c" * 64,
            canonical_rule_id="pattern_order_symmetry",
            reduced_query_a=(
                "MATCH (left:Customer {score: 99}), (right:Place) RETURN left, right"
                if changed
                else "MATCH (a:Person {age: 10}), (b:City) RETURN a, b"
            ),
            reduced_query_b=(
                "MATCH (right:Place), (left:Customer {score: 99}) RETURN left, right"
                if changed
                else "MATCH (b:City), (a:Person {age: 10}) RETURN a, b"
            ),
            runtime_direction="b",
            site_class="match_pattern_component",
            submission_path="relative",
            reduced_validation_phenotype="relative:stable:b",
        )
    )


def test_structural_duplicate_survives_restart_and_owner_repair_uses_new_version(
    monkeypatch,
    tmp_path,
) -> None:
    issue_ids = iter(("issue-1", "issue-2", "issue-1"))
    original_fingerprint = _fingerprint()
    changed_fingerprint = _fingerprint(changed=True)
    fingerprints = iter((original_fingerprint, changed_fingerprint, original_fingerprint))
    by_hash = {
        original_fingerprint.fingerprint_hash: original_fingerprint,
        changed_fingerprint.fingerprint_hash: changed_fingerprint,
    }
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: next(issue_ids),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._validated_fingerprint",
        lambda *_args, **_kwargs: next(fingerprints),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._record_fingerprint",
        lambda record: by_hash[record.fingerprint_hash],
    )

    def formal_record(issue_id, _validation, _reduction, _certificate_value, dbms, *, fingerprint, family_decision, stage):
        return VersionedIssueRecord(
            issue_id=issue_id,
            stage=stage,
            developer_status=DeveloperStatus.WAITING_REPLY,
            dbms=dbms,
            dbms_version=fingerprint.dbms_version,
            pair_hash=fingerprint.exact,
            direction=fingerprint.runtime_direction,
            rule_id=fingerprint.canonical_rule_id,
            detector_version="runtime-first-v3",
            validator_version="independent-two-session-v1",
            reducer_version="paired-ast-ddmin-v1",
            rule_version="pattern-order-symmetry-v1",
            discovered_at="2026-07-15T00:00:00+00:00",
            site_class=fingerprint.site_class,
            normalized_pattern="parser-proven-a",
            normalized_predicate="parser-proven-b",
            reduced_query_a="MATCH (n) RETURN n",
            reduced_query_b="MATCH (n) RETURN n",
            dbms_fingerprint=fingerprint.dbms_fingerprint,
            family_id=family_decision.family_id,
            family_version_hash=family_decision.family_version_hash,
            family_decision_hash=family_decision.decision_hash,
            exact_signature=fingerprint.exact,
            structural_signature=fingerprint.structural,
            fingerprint_hash=fingerprint.fingerprint_hash,
            submission_path=fingerprint.submission_path,
            reduced_phenotype=fingerprint.reduced_validation_phenotype,
        )

    monkeypatch.setattr("graph_sym_perf.issues.durable_registry._record", formal_record)
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, family_decision, duplicate_clearance, **_kwargs: SimpleNamespace(
            issue_id=family_decision.issue_id,
            family_id=family_decision.family_id,
            family_decision=family_decision,
            duplicate_clearance=duplicate_clearance,
        ),
    )

    first, first_status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )
    duplicate, duplicate_status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=object(),
        reduction=object(),
        certificate=object(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
    )

    assert first is not None and first_status == "unique_family"
    assert duplicate is None and duplicate_status == "structural_family_duplicate"
    assert repaired is not None and repaired_status == "family_owner_repaired"
    assert first.family_decision.verify()
    assert not durable_family_credentials_are_current(
        tmp_path,
        decision=first.family_decision,
        clearance=first.duplicate_clearance,
    )
    assert repaired.family_decision.verify()
    assert durable_family_credentials_are_current(
        tmp_path,
        decision=repaired.family_decision,
        clearance=repaired.duplicate_clearance,
    )
    assert len(repaired.family_decision.family_members) == 2
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((tmp_path / "dedup_review.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_registry_commits_only_after_materialization_and_repairs_exact_retry(tmp_path) -> None:
    report = _report()
    reduction = _reduction()
    calls = []

    def materialize(issue):
        calls.append("materialized")
        return _dynamic_receipt(issue)

    first, first_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=materialize,
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=materialize,
    )

    assert first is not None and repaired is not None
    assert first_status == "unique_family"
    assert repaired_status == "family_owner_repaired"
    assert calls == ["materialized", "materialized"]
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "commits" / f"{first.issue_id}.json").is_file()


def test_materialization_failure_never_creates_a_committed_registry_record(tmp_path) -> None:
    report = _report()
    reduction = _reduction()
    with pytest.raises(OSError, match="injected"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=lambda _issue: (_ for _ in ()).throw(OSError("injected")),
        )

    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "commits" / "issue-1.json").exists()


def test_missing_materializer_fails_before_any_registry_mutation(tmp_path) -> None:
    with pytest.raises(TypeError, match="materialize"):
        promote_unique_issue(
            tmp_path,
            validation=_report(),
            reduction=_reduction(),
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=None,  # type: ignore[arg-type]
        )

    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "commits").exists()


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
