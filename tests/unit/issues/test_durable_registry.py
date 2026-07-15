from types import SimpleNamespace
import itertools
import json
from pathlib import Path
import multiprocessing
from dataclasses import replace

import pytest

from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.reducer.models import (
    MinimalityProof,
    MinimalityUniverse,
    ReductionReport,
    ReductionStatus,
)
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.issues.durable_registry import (
    durable_family_credentials_are_current,
    promote_unique_issue,
    _load_records,
    _commit_family_transaction,
    _historical_family_fingerprint_proof,
    _record_payload,
    _records_with_current_family,
    _recover_family_transaction,
    _write_payload_lines_atomic,
    _replay_family_index,
    verify_durable_family_artifact_proof,
)
from graph_sym_perf.issues.dedup import (
    FingerprintSubject,
    IssueFamilyIndex,
    fingerprint_issue,
    fingerprint_validated_reduction,
    known_regression_signature_from_manifest,
    compact_family_decision_payload,
)
from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.validation.models import compact_duplicate_clearance_payload
from graph_sym_perf.issues.commands import _has_current_possible_duplicate
from graph_sym_perf.issues.registry import canonical_reduced_pair_hash
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    PipelineStage,
    VersionedIssueRecord,
    canonical_validation_provenance_hash,
    canonical_validation_report_hash,
    safety_certificate_hash,
)
from graph_sym_perf.validation.evidence import combined_runtime_evidence
from tests.unit.validation.test_promotion import _certificate, _reduction, _report


REGRESSION_MANIFEST = (
    Path(__file__).parents[2] / "fixtures" / "regressions" / "neo4j_bug001" / "manifest.yaml"
)


@pytest.fixture(autouse=True)
def _receipt_only_materialization_seam(monkeypatch) -> None:
    """This unit module uses synthetic receipts; production E2E keeps real replay."""

    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._verify_owner_materialization_against_durable",
        lambda *_args, **_kwargs: None,
    )


def _validation_refs() -> dict[str, ArtifactReference]:
    return {
        "original_validation_ref": ArtifactReference(
            "1" * 64,
            "objects/11/" + "1" * 64,
            100,
            "application/json",
        ),
        "reduced_validation_ref": ArtifactReference(
            "2" * 64,
            "objects/22/" + "2" * 64,
            100,
            "application/json",
        ),
    }


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
        **_validation_refs(),
    )
    queue.put((getattr(issue, "family_id", None), status))


def test_receipt_only_committed_owner_replay_fails_closed(tmp_path) -> None:
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
        **_validation_refs(),
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )

    assert first is not None and repaired is None
    assert first_status == "unique_family"
    assert repaired_status == "owner_terminal_evidence_unavailable"
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    record = json.loads((tmp_path / "registry.jsonl").read_text(encoding="utf-8"))
    assert record["family_id"] == first.family_id
    assert record["family_version_hash"] == first.family_decision.family_version_hash


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
            **_validation_refs(),
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
            graph_snapshot_hash=known.graph_snapshot_hash,
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

    report = _report()
    reduction = _reduction()
    issue, status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )

    assert issue is None and status == "known_regression_excluded"
    diagnostic = json.loads((tmp_path / "known_regressions.jsonl").read_text(encoding="utf-8"))
    assert diagnostic["known_regression_match"] is True
    assert diagnostic["case_id"] == "neo4j_bug001_pattern_order"
    assert diagnostic["original_validation_hash"] == reduction.original_validation_hash
    assert diagnostic["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert diagnostic["original_validation_ref"]["content_hash"] == "1" * 64
    assert diagnostic["reduced_validation_ref"]["content_hash"] == "2" * 64
    assert len(diagnostic["validation_provenance_hash"]) == 64
    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "commits").exists()

    diagnostic["original_validation_hash"] = "0" * 64
    (tmp_path / "known_regressions.jsonl").write_text(
        json.dumps(diagnostic) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="diagnostic provenance"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=_dynamic_receipt,
            **_validation_refs(),
        )


def test_exact_duplicate_record_retains_replayable_validation_provenance(
    monkeypatch,
    tmp_path,
) -> None:
    issue_ids = iter(("issue-1", "issue-2"))
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: next(issue_ids),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, family_decision, duplicate_clearance, **_kwargs: SimpleNamespace(
            issue_id=family_decision.issue_id,
            family_id=family_decision.family_id,
            family_decision=family_decision,
            duplicate_clearance=duplicate_clearance,
        ),
    )
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
        **_validation_refs(),
    )
    duplicate, duplicate_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )

    assert first is not None and first_status == "unique_family"
    assert duplicate is None and duplicate_status == "exact_family_duplicate"
    review = json.loads((tmp_path / "dedup_review.jsonl").read_text(encoding="utf-8"))
    assert review["original_validation_hash"] == reduction.original_validation_hash
    assert review["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert review["original_validation_ref"]["content_hash"] == "1" * 64
    assert review["reduced_validation_ref"]["content_hash"] == "2" * 64
    assert len(review["validation_provenance_hash"]) == 64


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
        "owner_terminal_evidence_unavailable",
    }
    assert len({family for family, _status in outcomes if family is not None}) == 1
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_family_budget_overflow_is_atomic_deduplicated_and_has_no_raw_query(
    monkeypatch,
    tmp_path,
) -> None:
    oversized = fingerprint_issue(
        FingerprintSubject(
            **{
                **_subject_values(),
                "reduced_query_a": "MATCH (n) RETURN n // " + "x" * (64 * 1024),
            }
        )
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._validated_fingerprint",
        lambda *_args, **_kwargs: oversized,
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: "oversized-issue",
    )
    report = _report()
    reduction = _reduction()
    for _attempt in range(2):
        issue, status = promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=lambda _issue: (_ for _ in ()).throw(
                AssertionError("overflow must not materialize")
            ),
            **_validation_refs(),
        )
        assert issue is None and status == "family_budget_exceeded"

    diagnostics = (tmp_path / "family_overflow.jsonl").read_text(encoding="utf-8")
    assert len(diagnostics.splitlines()) == 1
    assert "x" * 100 not in diagnostics
    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "dedup_review.jsonl").exists()
    assert not (tmp_path / "commits").exists()


def test_family_member_limit_preflight_does_not_change_registry_or_owner(
    monkeypatch,
    tmp_path,
) -> None:
    issue_ids = iter(("issue-1", "issue-2"))
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: next(issue_ids),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, family_decision, duplicate_clearance, **_kwargs: SimpleNamespace(
            issue_id=family_decision.issue_id,
            family_id=family_decision.family_id,
            family_decision=family_decision,
            duplicate_clearance=duplicate_clearance,
        ),
    )
    report = _report()
    reduction = _reduction()
    first, status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    assert first is not None and status == "unique_family", status
    registry_before = (tmp_path / "registry.jsonl").read_bytes()
    commit_before = (tmp_path / "commits" / "issue-1.json").read_bytes()
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.MAX_FAMILY_MEMBERS",
        1,
    )

    blocked, blocked_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda _issue: (_ for _ in ()).throw(
            AssertionError("member overflow must not materialize")
        ),
        **_validation_refs(),
    )

    assert blocked is None and blocked_status == "family_budget_exceeded"
    assert (tmp_path / "registry.jsonl").read_bytes() == registry_before
    assert (tmp_path / "commits" / "issue-1.json").read_bytes() == commit_before
    assert not (tmp_path / "dedup_review.jsonl").exists()


def test_compact_family_budget_measures_reviewer_prospective_owner_payload(
    monkeypatch,
    tmp_path,
) -> None:
    query_b = "MATCH (b:City), (a:Person) RETURN a, b"
    prefix = "MATCH (a:Person), (b:City) RETURN a, b // "
    query_a = (
        prefix
        + '"'
        + "x" * (60_020 - len(prefix.encode()) - len(query_b.encode()))
    )
    base = _reduction().reduced_pair
    pair = EquivalentQueryPair(
        query_a,
        query_b,
        {},
        base.graph_snapshot_hash,
        base.dbms_fingerprint,
    )
    report, reduction = _promotion_evidence(pair, suffix="reviewer-fixture")
    reviewer_fingerprint = fingerprint_validated_reduction(
        report,
        reduction,
        _certificate(),
        dbms="neo4j",
    )
    reviewer_decision = IssueFamilyIndex().add(reviewer_fingerprint, issue_id="ii")

    assert len(query_a.encode()) + len(query_b.encode()) == 60_021
    assert len(
        stable_json_dumps(compact_family_decision_payload(reviewer_decision)).encode()
    ) == 61_515
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: "ii",
    )

    issue, status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda _issue: (_ for _ in ()).throw(
            AssertionError("prospective compact overflow must not materialize")
        ),
        **_validation_refs(),
    )

    assert issue is None and status == "family_budget_exceeded"
    diagnostic = json.loads((tmp_path / "family_overflow.jsonl").read_text())
    assert diagnostic["reason"] == "compact_family_payload_bytes"
    assert diagnostic["observed"] > 61_515
    assert not (tmp_path / "registry.jsonl").exists()
    assert not (tmp_path / "dedup_review.jsonl").exists()
    assert not (tmp_path / "commits").exists()


def test_long_nonowner_passes_when_actual_owner_compact_envelope_fits(
    monkeypatch,
    tmp_path,
) -> None:
    long_pair = _long_structural_pair(raw_bytes=60_021)
    small_pair = _small_structural_pair()
    issue_id = lambda _report, reduction: (
        "z-long-nonowner"
        if len(reduction.reduced_pair.query_a) > 10_000
        else "a-small-owner"
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id", issue_id
    )
    monkeypatch.setattr(
        "graph_sym_perf.validation.promotion.deterministic_issue_id", issue_id
    )
    original_report, first_reduction = _promotion_evidence(small_pair, suffix="small-owner")

    first, first_status = promote_unique_issue(
        tmp_path,
        validation=original_report,
        reduction=first_reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    long_report, long_reduction = _promotion_evidence(long_pair, suffix="long-nonowner")
    duplicate, duplicate_status = promote_unique_issue(
        tmp_path,
        validation=long_report,
        reduction=long_reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda _issue: (_ for _ in ()).throw(
            AssertionError("a non-owner duplicate must not materialize")
        ),
        **_validation_refs(),
    )

    assert first is not None and first_status == "unique_family"
    assert duplicate is None and duplicate_status == "structural_family_duplicate"
    assert not (tmp_path / "family_overflow.jsonl").exists()


def test_long_deterministic_new_owner_overflow_is_atomic_and_restart_deduplicated(
    monkeypatch,
    tmp_path,
) -> None:
    long_pair = _long_structural_pair(raw_bytes=60_021)
    small_pair = _small_structural_pair()
    issue_id = lambda _report, reduction: (
        "a-long-new-owner"
        if len(reduction.reduced_pair.query_a) > 10_000
        else "z-small-member"
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id", issue_id
    )
    monkeypatch.setattr(
        "graph_sym_perf.validation.promotion.deterministic_issue_id", issue_id
    )
    original_report, first_reduction = _promotion_evidence(small_pair, suffix="small-member")
    first, first_status = promote_unique_issue(
        tmp_path,
        validation=original_report,
        reduction=first_reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    assert first is not None and first_status == "unique_family"
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != ".registry.lock"
    }
    long_report, long_reduction = _promotion_evidence(long_pair, suffix="long-new-owner")

    for _attempt in range(2):
        blocked, status = promote_unique_issue(
            tmp_path,
            validation=long_report,
            reduction=long_reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=lambda _issue: (_ for _ in ()).throw(
                AssertionError("prospective owner overflow must not materialize")
            ),
            **_validation_refs(),
        )
        assert blocked is None and status == "family_budget_exceeded"

    after_non_diagnostic = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
        and path.name not in {".registry.lock", "family_overflow.jsonl"}
    }
    diagnostics = (tmp_path / "family_overflow.jsonl").read_text().splitlines()
    assert after_non_diagnostic == before
    assert len(diagnostics) == 1
    assert "x" * 100 not in diagnostics[0]
    assert json.loads(diagnostics[0])["reason"] == "compact_family_payload_bytes"


def _promotion_evidence(
    pair: EquivalentQueryPair,
    *,
    suffix: str,
) -> tuple[object, ReductionReport]:
    original_pair = EquivalentQueryPair(
        pair.query_a + " ",
        pair.query_b + " ",
        pair.parameters,
        pair.graph_snapshot_hash,
        pair.dbms_fingerprint,
    )
    original = _report(pair=original_pair, metadata={"phase": f"original-{suffix}"})
    final = _report(pair=pair, metadata={"phase": f"final-{suffix}"})
    evidence = combined_runtime_evidence(final)
    assert evidence is not None
    universe = MinimalityUniverse.recompute(pair)
    proof = MinimalityProof.from_results(
        pair,
        tuple(
            (deletion.canonical_id, "symptom_not_preserved", f"{index:064x}")
            for index, deletion in enumerate(universe.deletions, start=1)
        ),
    )
    reduction = ReductionReport(
        ReductionStatus.REDUCED_VALID,
        original_pair,
        pair,
        (),
        (),
        evidence,
        screen_evidence=evidence,
        minimality_proof=proof,
        final_validation=final,
        original_validation_hash=canonical_validation_report_hash(original),
        reduced_validation_hash=canonical_validation_report_hash(final),
        reduced_candidate_hash=final.candidate_hash,
        validation_policy_hash=final.policy_hash,
        certificate_hash=safety_certificate_hash(_certificate()),
        stop_reason="exhausted_and_validated",
    )
    return original, reduction


def _long_structural_pair(*, raw_bytes: int) -> EquivalentQueryPair:
    base = _reduction().reduced_pair
    fixed_bytes = 72
    variable_length = (raw_bytes - fixed_bytes) // 4
    padding = raw_bytes - fixed_bytes - 4 * variable_length
    variable = "x" * variable_length
    query_a = (
        f"MATCH ({variable}:Person), (b:City) RETURN {variable}, b"
        + " " * padding
    )
    query_b = f"MATCH (b:City), ({variable}:Person) RETURN {variable}, b"
    assert len(query_a.encode()) + len(query_b.encode()) == raw_bytes
    return EquivalentQueryPair(
        query_a,
        query_b,
        {},
        base.graph_snapshot_hash,
        base.dbms_fingerprint,
    )


def _small_structural_pair() -> EquivalentQueryPair:
    base = _reduction().reduced_pair
    return EquivalentQueryPair(
        "MATCH (a:Person), (b:City) RETURN a, b",
        "MATCH (b:City), (a:Person) RETURN a, b",
        {},
        base.graph_snapshot_hash,
        base.dbms_fingerprint,
    )


def _subject_values(*, changed: bool = False) -> dict[str, str]:
    return {
        "dbms": "neo4j",
        "dbms_version": "dbms-version",
        "dbms_fingerprint": "c" * 64,
        "graph_snapshot_hash": "1" * 64,
        "canonical_rule_id": "pattern_order_symmetry",
        "reduced_query_a": (
            "MATCH (left:Customer {score: 99}), (right:Place) RETURN left, right"
            if changed
            else "MATCH (a:Person {age: 10}), (b:City) RETURN a, b"
        ),
        "reduced_query_b": (
            "MATCH (right:Place), (left:Customer {score: 99}) RETURN left, right"
            if changed
            else "MATCH (b:City), (a:Person {age: 10}) RETURN a, b"
        ),
        "runtime_direction": "b",
        "site_class": "match_pattern_component",
        "submission_path": "relative",
        "reduced_validation_phenotype": "relative:stable:b",
    }


def _fingerprint(*, changed: bool = False):
    return fingerprint_issue(FingerprintSubject(**_subject_values(changed=changed)))


def _formal_family_record(fingerprint, decision, *, stage: PipelineStage):
    original_ref = _validation_refs()["original_validation_ref"]
    reduced_ref = _validation_refs()["reduced_validation_ref"]
    original_mapping = original_ref.__dict__
    reduced_mapping = reduced_ref.__dict__
    changed = fingerprint.exact == _fingerprint(changed=True).exact
    query_a = (
        "MATCH (left:Customer {score: 99}), (right:Place) RETURN left, right"
        if changed
        else "MATCH (a:Person {age: 10}), (b:City) RETURN a, b"
    )
    query_b = (
        "MATCH (right:Place), (left:Customer {score: 99}) RETURN left, right"
        if changed
        else "MATCH (b:City), (a:Person {age: 10}) RETURN a, b"
    )
    return VersionedIssueRecord(
        issue_id=decision.issue_id,
        stage=stage,
        developer_status=DeveloperStatus.WAITING_REPLY,
        dbms="neo4j",
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
        reduced_query_a=query_a,
        reduced_query_b=query_b,
        dbms_fingerprint=fingerprint.dbms_fingerprint,
        graph_snapshot_hash=fingerprint.graph_snapshot_hash,
        family_id=decision.family_id or "",
        family_version_hash=decision.family_version_hash,
        family_decision_hash=decision.decision_hash,
        owner_issue_id=decision.owner_issue_id or "",
        exact_signature=fingerprint.exact,
        structural_signature=fingerprint.structural,
        fingerprint_hash=fingerprint.fingerprint_hash,
        submission_path=fingerprint.submission_path,
        reduced_phenotype=fingerprint.reduced_validation_phenotype,
        original_validation_hash="3" * 64,
        reduced_validation_hash="4" * 64,
        original_validation_ref=original_mapping,
        reduced_validation_ref=reduced_mapping,
        validation_provenance_hash=canonical_validation_provenance_hash(
            original_validation_hash="3" * 64,
            reduced_validation_hash="4" * 64,
            original_validation_ref=original_mapping,
            reduced_validation_ref=reduced_mapping,
        ),
    )


def test_durable_family_replay_uses_explicit_owner_independent_of_record_order() -> None:
    owner_fingerprint = _fingerprint()
    duplicate_fingerprint = _fingerprint(changed=True)
    issued = IssueFamilyIndex()
    issued.add(owner_fingerprint, issue_id="owner")
    duplicate_decision = issued.add(duplicate_fingerprint, issue_id="z-duplicate")
    owner_decision = issued.reissue_owner(owner_fingerprint, issue_id="owner")
    owner = _formal_family_record(
        owner_fingerprint,
        owner_decision,
        stage=PipelineStage.SUBMIT_READY,
    )
    duplicate = _formal_family_record(
        duplicate_fingerprint,
        duplicate_decision,
        stage=PipelineStage.REDUCED_VALID,
    )

    replayed = _replay_family_index((duplicate, owner))
    current = replayed.reissue_owner(owner_fingerprint, issue_id="owner")

    assert current.owner_issue_id == "owner"
    assert current.family_members == owner_decision.family_members


def test_compact_family_refs_recompute_raw_members_and_reject_missing_mismatch_and_symlink(
    tmp_path,
) -> None:
    run_dir = tmp_path / "run"
    root = run_dir / "issues"
    root.mkdir(parents=True)
    owner_fingerprint = _fingerprint()
    duplicate_fingerprint = _fingerprint(changed=True)
    issued = IssueFamilyIndex()
    issued.add(owner_fingerprint, issue_id="owner")
    issued.add(duplicate_fingerprint, issue_id="z-duplicate")
    owner_decision = issued.reissue_owner(owner_fingerprint, issue_id="owner")
    duplicate_decision = issued.reissue_member(
        duplicate_fingerprint,
        issue_id="z-duplicate",
    )
    owner = _formal_family_record(
        owner_fingerprint,
        owner_decision,
        stage=PipelineStage.SUBMIT_READY,
    )
    duplicate = _formal_family_record(
        duplicate_fingerprint,
        duplicate_decision,
        stage=PipelineStage.REDUCED_VALID,
    )
    _write_payload_lines_atomic(root / "registry.jsonl", [_record_payload(owner)])
    _write_payload_lines_atomic(root / "dedup_review.jsonl", [_record_payload(duplicate)])
    clearance = IssueRegistry().duplicate_clearance_for_family(
        owner_decision,
        (owner, duplicate),
    )
    family_payload = compact_family_decision_payload(owner_decision)
    clearance_payload = compact_duplicate_clearance_payload(clearance)

    verify_durable_family_artifact_proof(root, family_payload, clearance_payload)

    (root / "dedup_review.jsonl").unlink()
    with pytest.raises(ValueError, match="durable family"):
        verify_durable_family_artifact_proof(root, family_payload, clearance_payload)

    _write_payload_lines_atomic(root / "dedup_review.jsonl", [_record_payload(duplicate)])
    mismatched = _record_payload(duplicate)
    mismatched["reduced_query_b"] = "MATCH (forged) RETURN forged"
    _write_payload_lines_atomic(root / "dedup_review.jsonl", [mismatched])
    with pytest.raises(ValueError, match="fingerprint no longer verifies"):
        verify_durable_family_artifact_proof(root, family_payload, clearance_payload)

    _write_payload_lines_atomic(root / "dedup_review.jsonl", [_record_payload(duplicate)])
    registry_real = root / "registry.real.jsonl"
    (root / "registry.jsonl").replace(registry_real)
    (root / "registry.jsonl").symlink_to(registry_real)
    with pytest.raises(ValueError, match="symlink"):
        verify_durable_family_artifact_proof(root, family_payload, clearance_payload)


def test_exact_single_seed_owner_replays_deterministically_after_restart() -> None:
    fingerprints = {
        "z-owner": fingerprint_issue(
            FingerprintSubject(**{**_subject_values(), "graph_snapshot_hash": "1" * 64})
        ),
        "a-owner": fingerprint_issue(
            FingerprintSubject(**{**_subject_values(), "graph_snapshot_hash": "2" * 64})
        ),
    }

    for insertion_order in itertools.permutations(fingerprints):
        issued = IssueFamilyIndex()
        for issue_id in insertion_order:
            issued.add(fingerprints[issue_id], issue_id=issue_id)
        owner_decision = issued.reissue_owner(
            fingerprints["a-owner"],
            issue_id="a-owner",
        )
        member_decision = issued.reissue_member(
            fingerprints["z-owner"],
            issue_id="z-owner",
        )
        records = (
            _formal_family_record(
                fingerprints["a-owner"],
                owner_decision,
                stage=PipelineStage.SUBMIT_READY,
            ),
            _formal_family_record(
                fingerprints["z-owner"],
                member_decision,
                stage=PipelineStage.REDUCED_VALID,
            ),
        )

        for record_order in itertools.permutations(records):
            replayed = _replay_family_index(record_order)
            current = replayed.reissue_owner(
                fingerprints["a-owner"],
                issue_id="a-owner",
            )
            assert current.owner_issue_id == "a-owner"
            assert current.family_id == owner_decision.family_id
            assert current.family_version_hash == owner_decision.family_version_hash


@pytest.mark.parametrize(
    "order",
    tuple(itertools.permutations(("a-owner", "b-owner", "c-bridge"))),
)
def test_durable_bridge_union_leaves_one_owner_and_replays_after_restart(
    monkeypatch,
    tmp_path,
    order,
) -> None:
    fingerprints = (
        _fingerprint(changed=False),
        _fingerprint(changed=True),
        fingerprint_issue(
            FingerprintSubject(
                **{
                    **_subject_values(changed=False),
                    "site_class": "match_pattern_component",
                }
            )
        ),
    )
    # Make A's structural seed distinct while C keeps A's exact and B's structural seed.
    fingerprints = (
        fingerprint_issue(
            FingerprintSubject(
                **{**_subject_values(changed=False), "site_class": "where_boolean"}
            )
        ),
        fingerprints[1],
        fingerprints[2],
    )
    fingerprints_by_id = dict(
        zip(("a-owner", "b-owner", "c-bridge"), fingerprints, strict=True)
    )
    issue_ids = iter(order)
    issued_fingerprints = iter(fingerprints_by_id[issue_id] for issue_id in order)
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.deterministic_issue_id",
        lambda *_args: next(issue_ids),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._validated_fingerprint",
        lambda *_args, **_kwargs: next(issued_fingerprints),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._record",
        lambda issue_id, _validation, _reduction, _certificate_value, _dbms, *, fingerprint,
        family_decision, validation_provenance, stage: replace(
            _formal_family_record(fingerprint, family_decision, stage=stage),
            issue_id=issue_id,
            original_validation_hash=validation_provenance["original_validation_hash"],
            reduced_validation_hash=validation_provenance["reduced_validation_hash"],
            original_validation_ref=validation_provenance["original_validation_ref"],
            reduced_validation_ref=validation_provenance["reduced_validation_ref"],
            validation_provenance_hash=validation_provenance["validation_provenance_hash"],
        ),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry.promote",
        lambda *_args, family_decision, duplicate_clearance, **_kwargs: SimpleNamespace(
            issue_id=family_decision.issue_id,
            family_id=family_decision.family_id,
            family_decision=family_decision,
            duplicate_clearance=duplicate_clearance,
        ),
    )

    statuses = []
    for _ in range(3):
        _issue, status = promote_unique_issue(
            tmp_path,
            validation=_report(),
            reduction=_reduction(),
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=_dynamic_receipt,
            **_validation_refs(),
        )
        statuses.append(status)

    registry_records = _load_records(tmp_path / "registry.jsonl")
    review_records = _load_records(tmp_path / "dedup_review.jsonl")
    assert statuses[0] == "unique_family"
    assert len(registry_records) == 1
    assert len(review_records) == 2
    assert {record.owner_issue_id for record in (*registry_records, *review_records)} == {
        "b-owner"
    }
    replayed = _replay_family_index((*review_records, *registry_records))
    owner_fingerprint = fingerprints_by_id[registry_records[0].issue_id]
    current = replayed.reissue_owner(
        owner_fingerprint,
        issue_id=registry_records[0].issue_id,
    )
    assert {member for member, _exact in current.family_members} == {
        "a-owner",
        "b-owner",
        "c-bridge",
    }
    assert (tmp_path / "family_unions.jsonl").is_file()
    assert not (tmp_path / "family_transaction.pending.json").exists()


def test_durable_bridge_union_recovers_a_crash_after_registry_replace(
    monkeypatch,
    tmp_path,
) -> None:
    fingerprint_a = fingerprint_issue(
        FingerprintSubject(
            **{**_subject_values(changed=False), "site_class": "where_boolean"}
        )
    )
    fingerprint_b = _fingerprint(changed=True)
    fingerprint_c = _fingerprint(changed=False)
    index = IssueFamilyIndex()
    decision_a = index.add(fingerprint_a, issue_id="a-owner")
    decision_b = index.add(fingerprint_b, issue_id="b-owner")
    before = (
        _formal_family_record(
            fingerprint_a,
            decision_a,
            stage=PipelineStage.SUBMIT_READY,
        ),
        _formal_family_record(
            fingerprint_b,
            decision_b,
            stage=PipelineStage.SUBMIT_READY,
        ),
    )
    decision_c = index.add(fingerprint_c, issue_id="c-bridge")
    candidate = _formal_family_record(
        fingerprint_c,
        decision_c,
        stage=PipelineStage.REDUCED_VALID,
    )
    after = _records_with_current_family(
        index,
        (*before, candidate),
        family_id=str(decision_c.family_id),
    )
    _write_payload_lines_atomic(
        tmp_path / "registry.jsonl",
        [_record_payload(record) for record in before],
    )
    crashed = False

    def crash_once(_root):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("injected union crash")

    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._after_union_registry_replace",
        crash_once,
    )
    with pytest.raises(OSError, match="injected union crash"):
        _commit_family_transaction(
            tmp_path,
            previous_records=before,
            records=after,
        )

    assert (tmp_path / "family_transaction.pending.json").is_file()
    assert len(_load_records(tmp_path / "registry.jsonl")) == 1
    _recover_family_transaction(tmp_path)

    records = _load_records(
        tmp_path / "registry.jsonl",
        tmp_path / "dedup_review.jsonl",
    )
    replayed = _replay_family_index(records)
    owner = next(record for record in records if record.stage is PipelineStage.SUBMIT_READY)
    owner_fingerprint = {
        "a-owner": fingerprint_a,
        "b-owner": fingerprint_b,
        "c-bridge": fingerprint_c,
    }[owner.issue_id]
    current = replayed.reissue_owner(owner_fingerprint, issue_id=owner.issue_id)
    assert len(current.family_members) == 3
    assert not (tmp_path / "family_transaction.pending.json").exists()
    assert len((tmp_path / "family_unions.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_historical_compact_proof_rebuild_splits_an_abc_bridge_from_raw_records(
    tmp_path,
) -> None:
    fingerprint_a = fingerprint_issue(
        FingerprintSubject(
            **{**_subject_values(changed=False), "site_class": "where_boolean"}
        )
    )
    fingerprint_b = _fingerprint(changed=True)
    fingerprint_c = _fingerprint(changed=False)
    index = IssueFamilyIndex()
    decision_a = index.add(fingerprint_a, issue_id="a-owner")
    decision_b = index.add(fingerprint_b, issue_id="b-owner")
    record_a = _formal_family_record(
        fingerprint_a, decision_a, stage=PipelineStage.SUBMIT_READY
    )
    record_b = _formal_family_record(
        fingerprint_b, decision_b, stage=PipelineStage.SUBMIT_READY
    )
    old_clearance = IssueRegistry().duplicate_clearance_for_family(
        decision_b, (record_b,)
    )
    old_family_payload = compact_family_decision_payload(decision_b)
    old_clearance_payload = compact_duplicate_clearance_payload(old_clearance)
    decision_c = index.add(fingerprint_c, issue_id="c-bridge")
    candidate = _formal_family_record(
        fingerprint_c, decision_c, stage=PipelineStage.REDUCED_VALID
    )
    after = _records_with_current_family(
        index,
        (record_a, record_b, candidate),
        family_id=str(decision_c.family_id),
    )

    _commit_family_transaction(
        tmp_path,
        previous_records=(record_a, record_b),
        records=after,
    )
    proof = _historical_family_fingerprint_proof(
        tmp_path,
        old_family_payload,
        old_clearance_payload,
    )

    assert tuple(member for member, _fingerprint_value in proof) == ("b-owner",)


def test_historical_compact_proof_rebuilds_an_exact_owner_change(
    tmp_path,
) -> None:
    fingerprint = _fingerprint()
    index = IssueFamilyIndex()
    old_decision = index.add(fingerprint, issue_id="z-old-owner")
    old_record = _formal_family_record(
        fingerprint, old_decision, stage=PipelineStage.SUBMIT_READY
    )
    old_clearance = IssueRegistry().duplicate_clearance_for_family(
        old_decision, (old_record,)
    )
    old_family_payload = compact_family_decision_payload(old_decision)
    old_clearance_payload = compact_duplicate_clearance_payload(old_clearance)
    new_decision = index.add(fingerprint, issue_id="a-new-owner")
    candidate = _formal_family_record(
        fingerprint, new_decision, stage=PipelineStage.REDUCED_VALID
    )
    after = _records_with_current_family(
        index,
        (old_record, candidate),
        family_id=str(new_decision.family_id),
    )

    _commit_family_transaction(
        tmp_path,
        previous_records=(old_record,),
        records=after,
    )
    proof = _historical_family_fingerprint_proof(
        tmp_path,
        old_family_payload,
        old_clearance_payload,
    )

    assert tuple(member for member, _fingerprint_value in proof) == ("z-old-owner",)


@pytest.mark.parametrize("attack", ("missing", "reordered", "rewritten"))
def test_historical_compact_proof_rejects_missing_reordered_or_rewritten_audit(
    tmp_path,
    attack,
) -> None:
    fingerprint = _fingerprint()
    index = IssueFamilyIndex()
    old_decision = index.add(fingerprint, issue_id="z-old-owner")
    old_record = _formal_family_record(
        fingerprint, old_decision, stage=PipelineStage.SUBMIT_READY
    )
    old_clearance = IssueRegistry().duplicate_clearance_for_family(
        old_decision, (old_record,)
    )
    old_family_payload = compact_family_decision_payload(old_decision)
    old_clearance_payload = compact_duplicate_clearance_payload(old_clearance)
    previous = (old_record,)
    for issue_id in ("b-middle-owner", "a-current-owner"):
        candidate_decision = index.add(fingerprint, issue_id=issue_id)
        candidate = _formal_family_record(
            fingerprint, candidate_decision, stage=PipelineStage.REDUCED_VALID
        )
        after = _records_with_current_family(
            index,
            (*previous, candidate),
            family_id=str(candidate_decision.family_id),
        )
        _commit_family_transaction(
            tmp_path,
            previous_records=previous,
            records=after,
        )
        previous = after
    audit_path = tmp_path / "family_unions.jsonl"
    lines = audit_path.read_text().splitlines()
    assert len(lines) == 2
    if attack == "missing":
        lines = lines[1:]
    elif attack == "reordered":
        lines = list(reversed(lines))
    else:
        payload = json.loads(lines[0])
        payload["added_issue_ids"] = ["rewritten-member"]
        lines[0] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    audit_path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ValueError, match="audit"):
        _historical_family_fingerprint_proof(
            tmp_path,
            old_family_payload,
            old_clearance_payload,
        )


@pytest.mark.parametrize("attack", ("missing", "conflicting", "multiple_submit_ready", "owner_absent"))
def test_durable_family_replay_rejects_ambiguous_owner_state(attack: str) -> None:
    owner_fingerprint = _fingerprint()
    duplicate_fingerprint = _fingerprint(changed=True)
    issued = IssueFamilyIndex()
    issued.add(owner_fingerprint, issue_id="owner")
    duplicate_decision = issued.add(duplicate_fingerprint, issue_id="z-duplicate")
    owner_decision = issued.reissue_owner(owner_fingerprint, issue_id="owner")
    owner = _formal_family_record(owner_fingerprint, owner_decision, stage=PipelineStage.SUBMIT_READY)
    duplicate = _formal_family_record(
        duplicate_fingerprint,
        duplicate_decision,
        stage=PipelineStage.REDUCED_VALID,
    )
    records = {
        "missing": (replace(owner, owner_issue_id=""), duplicate),
        "conflicting": (owner, replace(duplicate, owner_issue_id="z-duplicate")),
        "multiple_submit_ready": (owner, replace(duplicate, stage=PipelineStage.SUBMIT_READY)),
        "owner_absent": (duplicate,),
    }[attack]

    with pytest.raises(ValueError, match="owner"):
        _replay_family_index(records)


def test_receipt_only_structural_owner_repair_fails_closed_after_restart(
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

    def formal_record(
        issue_id,
        _validation,
        _reduction,
        _certificate_value,
        dbms,
        *,
        fingerprint,
        family_decision,
        validation_provenance,
        stage,
    ):
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
                graph_snapshot_hash=fingerprint.graph_snapshot_hash,
            family_id=family_decision.family_id,
            family_version_hash=family_decision.family_version_hash,
                family_decision_hash=family_decision.decision_hash,
                owner_issue_id=family_decision.owner_issue_id,
            exact_signature=fingerprint.exact,
            structural_signature=fingerprint.structural,
            fingerprint_hash=fingerprint.fingerprint_hash,
            submission_path=fingerprint.submission_path,
            reduced_phenotype=fingerprint.reduced_validation_phenotype,
            original_validation_hash=validation_provenance["original_validation_hash"],
            reduced_validation_hash=validation_provenance["reduced_validation_hash"],
            original_validation_ref=validation_provenance["original_validation_ref"],
            reduced_validation_ref=validation_provenance["reduced_validation_ref"],
            validation_provenance_hash=validation_provenance["validation_provenance_hash"],
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
        **_validation_refs(),
    )
    duplicate, duplicate_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )

    assert first is not None and first_status == "unique_family"
    assert duplicate is None and duplicate_status == "structural_family_duplicate"
    assert repaired is None and repaired_status == "owner_terminal_evidence_unavailable"
    assert first.family_decision.verify()
    assert not durable_family_credentials_are_current(
        tmp_path,
        decision=first.family_decision,
        clearance=first.duplicate_clearance,
    )
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((tmp_path / "dedup_review.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    review = json.loads((tmp_path / "dedup_review.jsonl").read_text(encoding="utf-8"))
    assert review["original_validation_hash"] == reduction.original_validation_hash
    assert review["reduced_validation_hash"] == reduction.reduced_validation_hash
    assert review["original_validation_ref"]["content_hash"] == "1" * 64
    assert review["reduced_validation_ref"]["content_hash"] == "2" * 64


def test_receipt_only_exact_retry_never_rematerializes_without_sealed_evidence(tmp_path) -> None:
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
        **_validation_refs(),
    )
    repaired, repaired_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=materialize,
        **_validation_refs(),
    )

    assert first is not None and repaired is None
    assert first_status == "unique_family"
    assert repaired_status == "owner_terminal_evidence_unavailable"
    assert calls == ["materialized"]
    assert len((tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "commits" / f"{first.issue_id}.json").is_file()


def test_receipt_only_owner_retry_after_registry_commit_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    report = _report()
    reduction = _reduction()
    materialized = []
    from graph_sym_perf.issues import durable_registry as registry_module

    real_write_commit = registry_module._write_commit

    def crash_before_commit(*_args, **_kwargs):
        raise OSError("injected commit crash")

    monkeypatch.setattr(registry_module, "_write_commit", crash_before_commit)
    with pytest.raises(OSError, match="injected commit crash"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=lambda issue: materialized.append(issue.issue_id)
            or _dynamic_receipt(issue),
            **_validation_refs(),
        )

    registry_before = json.loads((tmp_path / "registry.jsonl").read_text(encoding="utf-8"))
    monkeypatch.setattr(registry_module, "_write_commit", real_write_commit)
    repaired, status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda issue: materialized.append(issue.issue_id)
        or _dynamic_receipt(issue),
        **_validation_refs(),
    )
    terminal, terminal_status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda _issue: (_ for _ in ()).throw(
            AssertionError("committed retry rematerialized")
        ),
        **_validation_refs(),
    )

    assert repaired is None and status == "owner_terminal_evidence_unavailable"
    assert terminal is None and terminal_status == "owner_terminal_evidence_unavailable"
    assert json.loads((tmp_path / "registry.jsonl").read_text(encoding="utf-8")) == registry_before
    assert len(materialized) == 1


@pytest.mark.parametrize(
    "drift",
    (
        {"submission_path": "absolute"},
        {"reduced_validation_phenotype": "absolute:stable:b"},
    ),
)
def test_durable_owner_retry_rejects_path_or_phenotype_identity_drift(
    monkeypatch,
    tmp_path,
    drift,
) -> None:
    report = _report()
    reduction = _reduction()
    first, _status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    assert first is not None
    original = first.family_decision.fingerprint
    drifted = fingerprint_issue(
        FingerprintSubject(
            dbms=original.dbms,
            dbms_version=original.dbms_version,
            dbms_fingerprint=original.dbms_fingerprint,
            graph_snapshot_hash=original.graph_snapshot_hash,
            canonical_rule_id=original.canonical_rule_id,
            reduced_query_a=reduction.reduced_pair.query_a,
            reduced_query_b=reduction.reduced_pair.query_b,
            runtime_direction=original.runtime_direction,
            site_class=original.site_class,
            submission_path=drift.get("submission_path", original.submission_path),
            reduced_validation_phenotype=drift.get(
                "reduced_validation_phenotype",
                original.reduced_validation_phenotype,
            ),
        )
    )
    assert drifted.exact == original.exact
    monkeypatch.setattr(
        "graph_sym_perf.issues.durable_registry._validated_fingerprint",
        lambda *_args, **_kwargs: drifted,
    )

    with pytest.raises(ValueError, match="current family owner"):
        promote_unique_issue(
            tmp_path,
            validation=report,
            reduction=reduction,
            certificate=_certificate(),
            dbms="neo4j",
            known_regression_manifest=REGRESSION_MANIFEST,
            materialize=_dynamic_receipt,
            **_validation_refs(),
        )


def test_durable_owner_retry_does_not_replace_committed_validation_references(
    tmp_path,
) -> None:
    report = _report()
    reduction = _reduction()
    first, _status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=_dynamic_receipt,
        **_validation_refs(),
    )
    assert first is not None
    drifted_refs = {
        "original_validation_ref": ArtifactReference(
            "9" * 64,
            "objects/99/" + "9" * 64,
            100,
            "application/json",
        ),
        "reduced_validation_ref": ArtifactReference(
            "8" * 64,
            "objects/88/" + "8" * 64,
            100,
            "application/json",
        ),
    }

    retried, status = promote_unique_issue(
        tmp_path,
        validation=report,
        reduction=reduction,
        certificate=_certificate(),
        dbms="neo4j",
        known_regression_manifest=REGRESSION_MANIFEST,
        materialize=lambda _issue: (_ for _ in ()).throw(
            AssertionError("drifted validation refs rematerialized owner")
        ),
        **drifted_refs,
    )

    assert retried is None
    assert status == "owner_terminal_evidence_unavailable"
    record = json.loads((tmp_path / "registry.jsonl").read_text(encoding="utf-8"))
    assert record["original_validation_ref"]["content_hash"] == "1" * 64
    assert record["reduced_validation_ref"]["content_hash"] == "2" * 64


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
            **_validation_refs(),
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
            **_validation_refs(),
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
