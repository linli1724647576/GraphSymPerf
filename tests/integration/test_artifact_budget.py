import json
import hashlib
import multiprocessing
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path

import pytest

from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import (
    ArtifactBudgetExceeded,
    ContentAddressedStore,
    enforce_shard_budget,
    load_artifact_record,
    verify_candidate_outcome_validation_chain,
    verify_issue_hash_chain,
)
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem, stable_json_dumps
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.issues.commands import IssueCommands
from graph_sym_perf.issues.durable_registry import (
    _commit_family_transaction,
    _records_with_current_family,
    durable_family_payload_is_current,
)
from graph_sym_perf.issues.dedup import FingerprintSubject, IssueFamilyIndex, fingerprint_issue
from graph_sym_perf.issues.registry import IssueRegistry
from graph_sym_perf.reducer.models import MinimalityProof, MinimalityUniverse
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    FrozenMapping,
    PipelineStage,
    VersionedIssueRecord,
    candidate_hash_from_content_hashes,
    canonical_content_hash,
    canonical_validation_provenance_hash,
)


def _run_issue_export(index: str, output: str, queue) -> None:
    try:
        exported = IssueCommands().export(issue=index, output=output)
        queue.put(("ok", str(exported)))
    except Exception as exc:  # pragma: no cover - marshalled to the parent assertion
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _issue_export_with_timeout(index, output) -> tuple[str, str]:
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(
        target=_run_issue_export,
        args=(str(index), str(output), queue),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        pytest.fail("IssueCommands.export deadlocked while validating a formal family")
    assert process.exitcode == 0
    return queue.get(timeout=2)


def _canonical_test_certificate() -> dict[str, object]:
    rule = RuleRegistry.default().get("constant_true_disjunction")
    return asdict(
        SafetyCertificate(
            rule.rule_id,
            "clauses[0].where",
            (),
            "boolean",
            "three_valued",
            "where",
            rule.purity_requirements,
            rule.proof_id,
            "capability-hash",
        )
    )


def test_store_deduplicates_content_and_enforces_per_shard_budget(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path, budget_bytes=3)
    first = store.put("a", b"abc")
    second = store.put("duplicate", b"abc")
    assert first == second
    assert store.used_bytes == 3
    with pytest.raises(ArtifactBudgetExceeded):
        store.put("b", b"d")


def test_store_reconciles_object_tree_after_link_before_counter_crash(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path, budget_bytes=4)
    store.put("first", b"abc")
    # Simulate a stale counter left by a crash after the object link was made.
    (tmp_path / ".used_bytes").write_text("0", encoding="ascii")

    recovered = ContentAddressedStore(tmp_path, budget_bytes=4)

    assert recovered.used_bytes == 3
    with pytest.raises(ArtifactBudgetExceeded):
        recovered.put("would-overflow", b"de")


def test_nonessential_minimal_diagnostics_degrade_with_explicit_warning(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path, budget_bytes=3)
    store.put("essential", b"abc")

    result = store.put_with_policy("debug", b"d", level=ArtifactLevel.MINIMAL, essential=False)

    assert result.reference is None
    assert result.warning == "SHARD_ARTIFACT_BUDGET_DEGRADED"


def test_one_thousand_minimal_records_are_well_below_500_mb(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path, budget_bytes=500 * 1024 * 1024)
    for index in range(1000):
        payload = json.dumps(
            {
                "query_hash": f"{index:064x}",
                "rule_id": "pattern_order_symmetry",
                "site": "clauses[0].pattern.components[0]",
                "equivalence": "complete_typed_equivalent",
                "screen_runtime_ms": 3.0,
                "error_kind": None,
            },
            separators=(",", ":"),
        ).encode()
        store.put(f"record-{index}", payload)
    assert store.used_bytes < 500 * 1024 * 1024


def test_complete_shard_budget_drops_only_declared_nonessential_diagnostics(tmp_path) -> None:
    shard = tmp_path / "shard"
    shard.mkdir()
    (shard / "accepted.jsonl").write_bytes(b"a" * 60)
    raw = shard / "attempt-001" / "gdsmith_raw"
    raw.mkdir(parents=True)
    (raw / "debug.log").write_bytes(b"d" * 60)

    report = enforce_shard_budget(
        shard,
        budget_bytes=100,
        nonessential_paths=(raw,),
    )

    assert report.used_bytes == 60
    assert report.warnings == ("SHARD_ARTIFACT_BUDGET_DEGRADED:attempt-001/gdsmith_raw",)
    assert not raw.exists()


def test_complete_shard_budget_never_drops_essential_candidate_evidence(tmp_path) -> None:
    shard = tmp_path / "shard"
    shard.mkdir()
    (shard / "candidates.jsonl").write_bytes(b"c" * 101)
    with pytest.raises(ArtifactBudgetExceeded, match="complete shard artifacts"):
        enforce_shard_budget(shard, budget_bytes=100)


def test_complete_shard_budget_refuses_to_delete_the_shard_root(tmp_path) -> None:
    shard = tmp_path / "shard"
    shard.mkdir()
    (shard / "essential").write_bytes(b"x" * 101)
    with pytest.raises(ValueError, match="shard root"):
        enforce_shard_budget(shard, budget_bytes=100, nonessential_paths=(shard,))
    assert (shard / "essential").is_file()


def test_issue_artifact_resolves_original_reduction_and_final_validation_hash_chain(tmp_path) -> None:
    run_dir = tmp_path / "run"
    store = ContentAddressedStore(
        run_dir / "shards" / "neo4j-000" / "artifacts",
        budget_bytes=1024 * 1024,
    )
    paired_samples = [
        {
            "order": "AB" if index < 10 else "BA",
            "a_ms": 1.0,
            "b_ms": 4.0,
            "a_timeout": False,
            "b_timeout": False,
        }
        for index in range(20)
    ]
    control_samples = [
        {
            "order": "AB" if index < 10 else "BA",
            "a_ms": 1.0,
            "b_ms": 1.0,
            "a_timeout": False,
            "b_timeout": False,
        }
        for index in range(20)
    ]

    graph_ref = asdict(store.put("graph", b"CREATE ();"))

    def candidate(query_a: str, query_b: str) -> tuple[str, dict[str, object]]:
        certificate = _canonical_test_certificate()
        hashes = {
            "query_a": hashlib.sha256(query_a.encode()).hexdigest(),
            "query_b": hashlib.sha256(query_b.encode()).hexdigest(),
            "parameters": canonical_content_hash({}),
            "graph_snapshot": graph_ref["content_hash"],
            "dbms_fingerprint": canonical_content_hash("c" * 64),
            "dbms_version": canonical_content_hash("dbms/1"),
            "resolved_config": "e" * 64,
            "discovery_slow_query": canonical_content_hash("b"),
            "discovery_ratio": canonical_content_hash(4.0),
            "validation_policy": "3" * 64,
            "certificate": canonical_content_hash(certificate),
        }
        candidate_hash = candidate_hash_from_content_hashes(hashes)
        record = ArtifactRecord(
            level=ArtifactLevel.CANDIDATE,
            payload={
                "candidate_hash": candidate_hash,
                "content_hashes": hashes,
                "query_a": query_a,
                "query_b": query_b,
                "parameters": {},
                "discovery_paired_timing": paired_samples[:5],
                "graph_snapshot_ref": graph_ref,
                "graph_snapshot_hash": graph_ref["content_hash"],
                "dbms_fingerprint": "c" * 64,
                "rule_id": certificate["rule_id"],
                "safety_certificate": certificate,
                "discovery_median_ratio": 4.0,
                "discovery_slow_query": "b",
            },
        )
        return candidate_hash, asdict(store.put_json(f"candidate-{candidate_hash}", record.to_dict()))

    def validation(
        candidate_hash: str,
        candidate_ref: dict[str, object],
        suffix: str,
        query_a: str,
        query_b: str,
    ) -> ArtifactRecord:
        session = lambda name, pid: {
            "session_id": f"{suffix}-{name}",
            "process_id": pid,
            "schedule_seed": pid,
            "graph_fingerprint": graph_ref["content_hash"],
            "dbms_fingerprint": "c" * 64,
            "query_a_hash": hashlib.sha256(query_a.encode()).hexdigest(),
            "query_b_hash": hashlib.sha256(query_b.encode()).hexdigest(),
            "slow_query": "b",
            "measurement_source": "ordinary_execute",
            "measurement_protocol": "ordinary-execute-20-pair-v1",
            "runtime_verdict": "pass",
            "runtime_reason_codes": [],
            "paired_timing": paired_samples,
            "negative_controls": [
                {"kind": "A/A", "paired_timing": control_samples},
                {"kind": "B/B", "paired_timing": control_samples},
            ],
            "lifecycle": {"complete": True, "connection_id": f"connection-{suffix}-{name}"},
        }
        return ArtifactRecord.seal_validation(
            {
                "report_hash": canonical_content_hash(
                    {"candidate_hash": candidate_hash, "session_group": suffix}
                ),
                "candidate_hash": candidate_hash,
                "candidate_ref": candidate_ref,
                "stage": "validation_passed",
                "validation_protocol": "independent-two-session-v1",
                "equivalence": {"equivalent": True, "complete_result": True, "reason_codes": []},
                "sessions": [session("one", 1), session("two", 2)],
                "environment": {
                    "dbms_version": "dbms/1",
                    "driver_version": "driver/1",
                    "execution_mode": "container_image",
                    "execution_artifact_digest": "sha256:" + "f" * 64,
                    "cpu_architecture": "arm64",
                    "config_hash": "e" * 64,
                },
                "dbms_fingerprint": "c" * 64,
                "validated_ratio": 4.0,
                "pooled_delta_ms": 3.0,
                "pooled_ratio_ci_low": 4.0,
                "pooled_delta_ci_low_ms": 3.0,
                    "submission_path": "relative",
                    "magnitude_stability": "stable",
                "noise_floor_ms": 0.0,
                "timer_resolution_ms": 0.001,
                "metadata": {"absolute_also_passed": False},
            }
        )

    original_a = "MATCH (n) WHERE n.id >= 0 RETURN n"
    original_b = "MATCH (n) WHERE true AND n.id >= 0 RETURN n"
    query_a = "MATCH (n) RETURN n"
    query_b = "MATCH (n) WHERE true RETURN n"
    original_candidate_hash, original_candidate_ref = candidate(original_a, original_b)
    reduced_candidate_hash, reduced_candidate_ref = candidate(query_a, query_b)
    original = validation(
        original_candidate_hash,
        original_candidate_ref,
        "original",
        original_a,
        original_b,
    )
    final = validation(
        reduced_candidate_hash,
        reduced_candidate_ref,
        "reduced",
        query_a,
        query_b,
    )
    original_ref = asdict(store.put_json("original-validation", original.to_dict()))
    final_ref = asdict(store.put_json("final-validation", final.to_dict()))

    rejected_direction_body = json.loads(json.dumps(dict(original.payload)))
    rejected_direction_body.pop("validation_hash")
    rejected_direction_body["stage"] = "rejected"
    rejected_direction_body["reason_codes"] = ["direction_mismatch"]
    for session in rejected_direction_body["sessions"]:
        session["slow_query"] = "b"
        session["runtime_verdict"] = "fail"
        session["runtime_reason_codes"] = ["direction_mismatch"]
        session["paired_timing"] = [
            {**sample, "a_ms": sample["b_ms"], "b_ms": sample["a_ms"]}
            for sample in paired_samples
        ]
    rejected_direction = ArtifactRecord.seal_validation(rejected_direction_body)
    assert rejected_direction.payload["stage"] == "rejected"

    unexplained_direction_body = json.loads(json.dumps(rejected_direction_body))
    for session in unexplained_direction_body["sessions"]:
        session["runtime_reason_codes"] = []
    with pytest.raises(ValueError, match="slow_query contradicts raw samples"):
        ArtifactRecord.seal_validation(unexplained_direction_body)

    invalid_session_body = json.loads(json.dumps(dict(original.payload)))
    invalid_session_body.pop("validation_hash")
    invalid_session_body["stage"] = "invalid"
    invalid_session_body["reason_codes"] = ["session_process_failed"]
    invalid_session_body["equivalence"] = {
        "equivalent": False,
        "complete_result": False,
        "reason_codes": ["session_process_failed"],
    }
    for session in invalid_session_body["sessions"]:
        session["graph_fingerprint"] = ""
        session["dbms_fingerprint"] = ""
        session["measurement_source"] = None
        session["measurement_protocol"] = None
        session["runtime_verdict"] = "invalid"
        session["runtime_reason_codes"] = ["session_process_failed"]
        session["paired_timing"] = []
        session["negative_controls"] = []
        session["lifecycle"] = {"complete": False, "connection_id": ""}
    invalid_session = ArtifactRecord.seal_validation(invalid_session_body)
    assert invalid_session.payload["stage"] == "invalid"

    unexplained_invalid_body = json.loads(json.dumps(invalid_session_body))
    unexplained_invalid_body["sessions"][0]["runtime_reason_codes"] = []
    with pytest.raises(ValueError, match="session lifecycle is incomplete"):
        ArtifactRecord.seal_validation(unexplained_invalid_body)

    rejected_incomplete_body = json.loads(json.dumps(invalid_session_body))
    rejected_incomplete_body["stage"] = "rejected"
    with pytest.raises(ValueError, match="session lifecycle is incomplete"):
        ArtifactRecord.seal_validation(rejected_incomplete_body)

    reduced_hashes = {
        "query_a": hashlib.sha256(query_a.encode()).hexdigest(),
        "query_b": hashlib.sha256(query_b.encode()).hexdigest(),
    }
    reduced_pair = EquivalentQueryPair(
        query_a,
        query_b,
        FrozenMapping.from_mapping({}),
        graph_ref["content_hash"],
        "c" * 64,
    )
    universe = MinimalityUniverse.recompute(reduced_pair)
    proof = MinimalityProof.from_results(
        reduced_pair,
        tuple(
            (deletion.canonical_id, "symptom_not_preserved", "9" * 64)
            for deletion in universe.deletions
        ),
    )
    certificate = _canonical_test_certificate()
    original_validation_hash = original.payload["report_hash"]
    reduced_validation_hash = final.payload["report_hash"]
    reduction_ref = asdict(
        store.put_json(
            "reduction",
            {
                "candidate_hash": original_candidate_hash,
                "reduced_candidate_hash": reduced_candidate_hash,
                "reduced_content_hashes": reduced_hashes,
                "status": "reduced_valid",
                "stop_reason": "exhausted_and_validated",
                "minimality_proof": asdict(proof),
                "final_evidence": {
                    "verdict": "pass",
                    "query_a_hash": reduced_hashes["query_a"],
                    "query_b_hash": reduced_hashes["query_b"],
                    "paired_samples": paired_samples + paired_samples,
                    "median_ratio": 4.0,
                    "median_delta_ms": 3.0,
                    "ci_ratio_low": 4.0,
                    "ci_ratio_high": 4.0,
                    "direction_consistency": 1.0,
                    "relative_mad": 0.0,
                    "reason_codes": [],
                    "timeout_lower_bound_ratio": None,
                    "slow_query": "b",
                    "graph_snapshot_hash": graph_ref["content_hash"],
                    "dbms_fingerprint": "c" * 64,
                    "measurement_source": "ordinary_execute",
                    "measurement_protocol": "ordinary-execute-40-pair-combined-v1",
                },
                "validation_policy_hash": "3" * 64,
                "certificate_hash": canonical_content_hash(certificate),
                "original_validation_hash": original_validation_hash,
                "reduced_validation_hash": reduced_validation_hash,
                "original_validation_ref": original_ref,
                "reduced_validation_ref": final_ref,
            },
        )
    )
    reproducer_ref = asdict(store.put("reproducer", b"CREATE ();"))
    fingerprint = fingerprint_issue(
        FingerprintSubject(
            dbms="neo4j",
            dbms_version="dbms/1",
            dbms_fingerprint="c" * 64,
            graph_snapshot_hash=graph_ref["content_hash"],
            canonical_rule_id="constant_true_disjunction",
            reduced_query_a=query_a,
            reduced_query_b=query_b,
            runtime_direction="b",
            site_class="where_boolean",
            submission_path="relative",
            reduced_validation_phenotype="relative:stable:b",
        )
    )
    family_decision = IssueFamilyIndex().add(fingerprint, issue_id="issue-1")
    assert family_decision.family_id is not None
    typed_registry_record = VersionedIssueRecord(
        issue_id="issue-1",
        stage=PipelineStage.SUBMIT_READY,
        developer_status=DeveloperStatus.WAITING_REPLY,
        dbms="neo4j",
        dbms_version="dbms/1",
        pair_hash=fingerprint.exact,
        direction="b",
        rule_id="constant_true_disjunction",
        detector_version="runtime-first-v3",
        validator_version="independent-two-session-v1",
        reducer_version="paired-ast-ddmin-v1",
        status_evidence_url=None,
        rule_version="kleene_or_true_v1",
        discovered_at="2026-07-11T00:00:00+00:00",
        site_class="where_boolean",
        normalized_pattern=(fingerprint.normalized_pair or (query_a, query_b))[0],
        normalized_predicate=(fingerprint.normalized_pair or (query_a, query_b))[1],
        reduced_query_a=query_a,
        reduced_query_b=query_b,
        submitted_at=None,
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
        known_regression_match=False,
        original_validation_hash=original_validation_hash,
        reduced_validation_hash=reduced_validation_hash,
        original_validation_ref=original_ref,
        reduced_validation_ref=final_ref,
        validation_provenance_hash=canonical_validation_provenance_hash(
            original_validation_hash=original_validation_hash,
            reduced_validation_hash=reduced_validation_hash,
            original_validation_ref=original_ref,
            reduced_validation_ref=final_ref,
        ),
    )
    registry = IssueRegistry()
    registry.record(typed_registry_record)
    clearance = registry.duplicate_clearance_for_family(
        family_decision,
        (typed_registry_record,),
    )
    registry_record = asdict(typed_registry_record)
    registry_record["stage"] = typed_registry_record.stage.value
    registry_record["developer_status"] = typed_registry_record.developer_status.value
    issue = ArtifactRecord.seal_issue(
        {
            "issue_id": "issue-1",
            "family_id": family_decision.family_id,
            "stage": "submit_ready",
            "reduction_status": "reduced_valid",
            "certificate": _canonical_test_certificate(),
            "duplicate_clearance": {
                "issue_id": "issue-1",
                "possible_duplicate": clearance.possible_duplicate,
                "cluster_members": clearance.cluster_members,
                "cluster_version_hash": clearance.cluster_version_hash,
                "evidence_url": clearance.evidence_url,
                "registry_signature": clearance.registry_signature,
                "family_id": clearance.family_id,
                "family_version_hash": clearance.family_version_hash,
                "family_decision_hash": clearance.family_decision_hash,
            },
            "family_decision": {
                "issue_id": family_decision.issue_id,
                "owner_issue_id": family_decision.owner_issue_id,
                "family_id": family_decision.family_id,
                "fingerprint": asdict(family_decision.fingerprint),
                "match_kind": family_decision.match_kind.value,
                "matched_issue_id": family_decision.matched_issue_id,
                "known_regression_match": family_decision.known_regression_match,
                "known_regression_case_id": family_decision.known_regression_case_id,
                    "is_new_family": family_decision.is_new_family,
                    "family_members": family_decision.family_members,
                    "family_fingerprints": tuple(
                        (member, asdict(member_fingerprint))
                        for member, member_fingerprint in family_decision.family_fingerprints
                    ),
                    "family_version_hash": family_decision.family_version_hash,
                "decision_hash": family_decision.decision_hash,
            },
            "original_validation_hash": original_validation_hash,
            "reduced_validation_hash": reduced_validation_hash,
            "validated_ratio": 4.0,
            "dbms_version": "dbms/1",
            "candidate_hash": original_candidate_hash,
            "reduced_candidate_hash": reduced_candidate_hash,
            "reduced_query_a": query_a,
            "reduced_query_b": query_b,
            "reduced_content_hashes": reduced_hashes,
            "reproducer_ref": reproducer_ref,
            "original_validation_ref": original_ref,
            "final_validation_ref": final_ref,
            "reduction_ref": reduction_ref,
        }
    )

    verify_issue_hash_chain(store, issue)
    candidate_outcome = {
        "candidate_hash": original_candidate_hash,
        "validation": {
            "stage": "validation_passed",
            "validation_ref": original_ref,
        },
        "reduction": {
            "status": "reduced_valid",
            "reduced_candidate_hash": reduced_candidate_hash,
            "reduction_ref": reduction_ref,
            "original_validation_hash": original_validation_hash,
            "reduced_validation_hash": reduced_validation_hash,
            "original_validation_ref": original_ref,
            "reduced_validation_ref": final_ref,
        },
    }
    verify_candidate_outcome_validation_chain(store, candidate_outcome)
    with pytest.raises(ValueError, match="differs from reduction summary"):
        verify_candidate_outcome_validation_chain(
            store,
            {
                **candidate_outcome,
                "reduction": {
                    **candidate_outcome["reduction"],
                    "reduced_validation_hash": "0" * 64,
                },
            },
        )
    forged_validation = dict(original.payload)
    forged_validation.pop("validation_hash")
    forged_validation["report_hash"] = "0" * 64
    forged_validation["validation_hash"] = hashlib.sha256(
        stable_json_dumps(forged_validation).encode("utf-8")
    ).hexdigest()
    forged_validation_ref = asdict(
        store.put_json(
            "forged-original-validation-projection",
            {**original.to_dict(), **forged_validation},
        )
    )
    synchronized_reduction = json.loads(store.read(reduction_ref))
    synchronized_reduction["original_validation_hash"] = "0" * 64
    synchronized_reduction["original_validation_ref"] = forged_validation_ref
    synchronized_reduction_ref = asdict(
        store.put_json("forged-synchronized-reduction", synchronized_reduction)
    )
    synchronized_issue = ArtifactRecord.seal_issue(
        {
            **{key: value for key, value in issue.payload.items() if key != "issue_hash"},
            "original_validation_hash": "0" * 64,
            "original_validation_ref": forged_validation_ref,
            "reduction_ref": synchronized_reduction_ref,
        }
    )
    with pytest.raises(ValueError, match="canonical report projection"):
        verify_issue_hash_chain(store, synchronized_issue)
    issue_body = {key: value for key, value in issue.payload.items() if key != "issue_hash"}

    for hash_field in ("original_validation_hash", "reduced_validation_hash"):
        rewritten = ArtifactRecord.seal_issue(
            {**issue_body, hash_field: "0" * 64}
        )
        with pytest.raises(ValueError, match="validation report hash"):
            verify_issue_hash_chain(store, rewritten)

    foreign_dbms_body = json.loads(json.dumps(dict(final.payload)))
    foreign_dbms_body.pop("validation_hash")
    foreign_dbms_body["dbms_fingerprint"] = "d" * 64
    for session in foreign_dbms_body["sessions"]:
        session["dbms_fingerprint"] = "d" * 64
    foreign_dbms = ArtifactRecord.seal_validation(foreign_dbms_body)
    foreign_dbms_ref = asdict(
        store.put_json("foreign-dbms-validation", foreign_dbms.to_dict())
    )
    foreign_dbms_issue = ArtifactRecord.seal_issue(
        {**issue_body, "final_validation_ref": foreign_dbms_ref}
    )
    with pytest.raises(ValueError, match="session identity does not bind the candidate"):
        verify_issue_hash_chain(store, foreign_dbms_issue)

    reversed_samples = [
        {**sample, "a_ms": sample["b_ms"], "b_ms": sample["a_ms"]}
        for sample in paired_samples
    ]
    reversed_validation_body = json.loads(json.dumps(dict(final.payload)))
    reversed_validation_body.pop("validation_hash")
    for session in reversed_validation_body["sessions"]:
        session["slow_query"] = "a"
        session["paired_timing"] = reversed_samples
    reversed_validation = ArtifactRecord.seal_validation(reversed_validation_body)
    reversed_validation_ref = asdict(
        store.put_json("reversed-validation", reversed_validation.to_dict())
    )
    reversed_reduction = json.loads(store.read(reduction_ref))
    reversed_reduction["final_evidence"].update(
        {
            "slow_query": "a",
            "paired_samples": reversed_samples + reversed_samples,
        }
    )
    reversed_reduction_ref = asdict(
        store.put_json("reversed-reduction", reversed_reduction)
    )
    reversed_issue = ArtifactRecord.seal_issue(
        {
            **issue_body,
            "final_validation_ref": reversed_validation_ref,
            "reduction_ref": reversed_reduction_ref,
        }
    )
    with pytest.raises(ValueError, match="discovery direction"):
        verify_issue_hash_chain(store, reversed_issue)

    alternate_samples = [
        {**sample, "b_ms": 5.0}
        for sample in paired_samples
    ]
    alternate_reduction = json.loads(store.read(reduction_ref))
    alternate_reduction["final_evidence"].update(
        {
            "paired_samples": alternate_samples + alternate_samples,
            "median_ratio": 5.0,
            "median_delta_ms": 4.0,
            "ci_ratio_low": 5.0,
            "ci_ratio_high": 5.0,
        }
    )
    alternate_reduction_ref = asdict(
        store.put_json("alternate-reduction", alternate_reduction)
    )
    alternate_issue = ArtifactRecord.seal_issue(
        {**issue_body, "reduction_ref": alternate_reduction_ref}
    )
    with pytest.raises(ValueError, match="samples do not bind final validation"):
        verify_issue_hash_chain(store, alternate_issue)

    forged_ratio_issue = ArtifactRecord.seal_issue(
        {**issue_body, "validated_ratio": 99.0}
    )
    with pytest.raises(ValueError, match="validated_ratio does not bind"):
        verify_issue_hash_chain(store, forged_ratio_issue)

    reduction_payload = json.loads(store.read(reduction_ref))
    reduction_payload["final_evidence"]["median_ratio"] = 99.0
    forged_reduction_ref = asdict(
        store.put_json("forged-reduction", reduction_payload)
    )
    forged_evidence = ArtifactRecord.seal_issue(
        {**issue_body, "reduction_ref": forged_reduction_ref}
    )
    with pytest.raises(ValueError, match="does not satisfy promotion"):
        verify_issue_hash_chain(store, forged_evidence)

    issue_ref = asdict(store.put_json("issue-1", issue.to_dict()))
    typed_export = {
        "issue_id": "issue-1",
        "stage": "submit_ready",
        "reduction_status": "reduced_valid",
        "rule_id": "constant_true_disjunction",
        "validated_ratio": 4.0,
        "dbms_version": "dbms/1",
        "reduced_original": query_a,
        "reduced_transformed": query_b,
        "family_id": family_decision.family_id,
        "exact_signature": fingerprint.exact,
        "structural_signature": fingerprint.structural,
        "submission_path": fingerprint.submission_path,
        "reduced_validation_phenotype": fingerprint.reduced_validation_phenotype,
        "family_version_hash": family_decision.family_version_hash,
        "family_decision_hash": family_decision.decision_hash,
        "original_validation_hash": original_validation_hash,
        "reduced_validation_hash": reduced_validation_hash,
        "original_validation_ref": original_ref,
        "reduced_validation_ref": final_ref,
    }
    export_path = run_dir / "issues" / "submit-ready" / "issue-1.json"
    export_path.parent.mkdir(parents=True)
    export_path.write_text(json.dumps(typed_export) + "\n", encoding="utf-8")
    receipt = {
        "artifact_store": "shards/neo4j-000/artifacts",
        "export_path": "issues/submit-ready/issue-1.json",
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
        "issue_id": "issue-1",
        "issue_ref": issue_ref,
    }
    index_path = run_dir / "issues" / "index" / "issue-1.json"
    index_path.parent.mkdir()
    index_path.write_text(json.dumps(receipt), encoding="utf-8")
    (run_dir / "issues" / "registry.jsonl").write_text(
        json.dumps(registry_record) + "\n",
        encoding="utf-8",
    )
    commit_path = run_dir / "issues" / "commits" / "issue-1.json"
    commit_path.parent.mkdir()
    commit_path.write_text(
        json.dumps(
            {
                **receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    export_status, export_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "developer.json",
    )
    assert export_status == "ok", export_result
    exported = Path(export_result)
    assert json.loads(exported.read_text(encoding="utf-8"))["reduction_status"] == "reduced_valid"

    # A/C share an exact signature while B/C share a structural signature.
    # Simulate a crash after the durable union but before owner artifact renewal;
    # the next export must renew from issue-1's sealed evidence, never C's.
    original_files = {
        path: path.read_bytes()
        for path in (export_path, index_path, commit_path, run_dir / "issues" / "registry.jsonl")
    }
    pattern_a = "MATCH (a:Person {age: 10}), (b:City) RETURN a, b"
    pattern_b = "MATCH (b:City), (a:Person {age: 10}) RETURN a, b"
    pattern_candidate_hash, pattern_candidate_ref = candidate(pattern_a, pattern_b)
    pattern_validation = validation(
        pattern_candidate_hash,
        pattern_candidate_ref,
        "pattern-reduced",
        pattern_a,
        pattern_b,
    )
    pattern_validation_ref = asdict(
        store.put_json("pattern-final-validation", pattern_validation.to_dict())
    )
    pattern_hashes = {
        "query_a": hashlib.sha256(pattern_a.encode()).hexdigest(),
        "query_b": hashlib.sha256(pattern_b.encode()).hexdigest(),
    }
    pattern_pair = EquivalentQueryPair(
        pattern_a,
        pattern_b,
        FrozenMapping.from_mapping({}),
        graph_ref["content_hash"],
        "c" * 64,
    )
    pattern_universe = MinimalityUniverse.recompute(pattern_pair)
    pattern_proof = MinimalityProof.from_results(
        pattern_pair,
        tuple(
            (deletion.canonical_id, "symptom_not_preserved", "8" * 64)
            for deletion in pattern_universe.deletions
        ),
    )
    pattern_reduction = json.loads(store.read(reduction_ref))
    pattern_reduction.update(
        reduced_candidate_hash=pattern_candidate_hash,
        reduced_content_hashes=pattern_hashes,
        reduced_validation_hash=pattern_validation.payload["report_hash"],
        reduced_validation_ref=pattern_validation_ref,
        minimality_proof=asdict(pattern_proof),
    )
    pattern_reduction["final_evidence"].update(
        query_a_hash=pattern_hashes["query_a"],
        query_b_hash=pattern_hashes["query_b"],
    )
    pattern_reduction_ref = asdict(
        store.put_json("pattern-reduction", pattern_reduction)
    )
    continuity_fingerprint = fingerprint_issue(
        FingerprintSubject(
            dbms="neo4j",
            dbms_version="dbms/1",
            dbms_fingerprint="c" * 64,
            graph_snapshot_hash=graph_ref["content_hash"],
            canonical_rule_id="constant_true_disjunction",
            reduced_query_a=pattern_a,
            reduced_query_b=pattern_b,
            runtime_direction="b",
            site_class="where_boolean",
            submission_path="relative",
            reduced_validation_phenotype="relative:stable:b",
        )
    )
    continuity_family_decision = IssueFamilyIndex().add(
        continuity_fingerprint,
        issue_id="issue-1",
    )
    continuity_registry_record = replace(
        typed_registry_record,
        pair_hash=continuity_fingerprint.exact,
        reduced_query_a=pattern_a,
        reduced_query_b=pattern_b,
        family_id=str(continuity_family_decision.family_id),
        family_version_hash=continuity_family_decision.family_version_hash,
        family_decision_hash=continuity_family_decision.decision_hash,
        owner_issue_id="issue-1",
        exact_signature=continuity_fingerprint.exact,
        structural_signature=continuity_fingerprint.structural,
        fingerprint_hash=continuity_fingerprint.fingerprint_hash,
        reduced_validation_hash=pattern_validation.payload["report_hash"],
        reduced_validation_ref=pattern_validation_ref,
        validation_provenance_hash=canonical_validation_provenance_hash(
            original_validation_hash=original_validation_hash,
            reduced_validation_hash=pattern_validation.payload["report_hash"],
            original_validation_ref=original_ref,
            reduced_validation_ref=pattern_validation_ref,
        ),
    )
    continuity_clearance = IssueRegistry().duplicate_clearance_for_family(
        continuity_family_decision,
        (continuity_registry_record,),
    )
    continuity_issue = ArtifactRecord.seal_issue(
        {
            **issue_body,
            "family_id": continuity_family_decision.family_id,
            "reduced_candidate_hash": pattern_candidate_hash,
            "reduced_query_a": pattern_a,
            "reduced_query_b": pattern_b,
            "reduced_content_hashes": pattern_hashes,
            "final_validation_ref": pattern_validation_ref,
            "reduced_validation_hash": pattern_validation.payload["report_hash"],
            "reduction_ref": pattern_reduction_ref,
            "duplicate_clearance": {
                "issue_id": continuity_clearance.issue_id,
                "possible_duplicate": continuity_clearance.possible_duplicate,
                "cluster_members": continuity_clearance.cluster_members,
                "cluster_version_hash": continuity_clearance.cluster_version_hash,
                "evidence_url": continuity_clearance.evidence_url,
                "registry_signature": continuity_clearance.registry_signature,
                "family_id": continuity_clearance.family_id,
                "family_version_hash": continuity_clearance.family_version_hash,
                "family_decision_hash": continuity_clearance.family_decision_hash,
            },
            "family_decision": {
                "issue_id": continuity_family_decision.issue_id,
                "owner_issue_id": continuity_family_decision.owner_issue_id,
                "family_id": continuity_family_decision.family_id,
                "fingerprint": asdict(continuity_family_decision.fingerprint),
                "match_kind": continuity_family_decision.match_kind.value,
                "matched_issue_id": continuity_family_decision.matched_issue_id,
                "known_regression_match": continuity_family_decision.known_regression_match,
                "known_regression_case_id": continuity_family_decision.known_regression_case_id,
                "is_new_family": continuity_family_decision.is_new_family,
                "family_members": continuity_family_decision.family_members,
                "family_fingerprints": tuple(
                    (member, asdict(member_fingerprint))
                    for member, member_fingerprint in continuity_family_decision.family_fingerprints
                ),
                "family_version_hash": continuity_family_decision.family_version_hash,
                "decision_hash": continuity_family_decision.decision_hash,
            },
        }
    )
    verify_issue_hash_chain(store, continuity_issue)
    continuity_issue_ref = asdict(
        store.put_json("pattern-issue-1", continuity_issue.to_dict())
    )
    continuity_export = {
        **typed_export,
        "family_id": continuity_family_decision.family_id,
        "reduced_original": pattern_a,
        "reduced_transformed": pattern_b,
        "exact_signature": continuity_fingerprint.exact,
        "structural_signature": continuity_fingerprint.structural,
        "family_version_hash": continuity_family_decision.family_version_hash,
        "family_decision_hash": continuity_family_decision.decision_hash,
        "reduced_validation_hash": pattern_validation.payload["report_hash"],
        "reduced_validation_ref": pattern_validation_ref,
    }
    export_path.write_text(json.dumps(continuity_export) + "\n", encoding="utf-8")
    continuity_receipt = {
        **receipt,
        "issue_ref": continuity_issue_ref,
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(continuity_receipt), encoding="utf-8")
    continuity_registry_payload = asdict(continuity_registry_record)
    continuity_registry_payload["stage"] = continuity_registry_record.stage.value
    continuity_registry_payload["developer_status"] = continuity_registry_record.developer_status.value
    (run_dir / "issues" / "registry.jsonl").write_text(
        json.dumps(continuity_registry_payload) + "\n",
        encoding="utf-8",
    )
    commit_path.write_text(
        json.dumps(
            {
                **continuity_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(
                        continuity_registry_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    renamed_a = "MATCH (left:Customer {score: 99}), (right:Place) RETURN left, right"
    renamed_b = "MATCH (right:Place), (left:Customer {score: 99}) RETURN left, right"
    bridge_fingerprints = None
    for suffix in range(256):
        phenotype = f"relative:stable:b:bridge-{suffix}"
        candidate_b = fingerprint_issue(
            FingerprintSubject(
                dbms="neo4j",
                dbms_version="dbms/1",
                dbms_fingerprint="c" * 64,
                graph_snapshot_hash=graph_ref["content_hash"],
                canonical_rule_id="constant_true_disjunction",
                reduced_query_a=renamed_a,
                reduced_query_b=renamed_b,
                runtime_direction="b",
                site_class="where_boolean",
                submission_path="relative",
                reduced_validation_phenotype=phenotype,
            )
        )
        candidate_c = fingerprint_issue(
            FingerprintSubject(
                dbms="neo4j",
                dbms_version="dbms/1",
                dbms_fingerprint="c" * 64,
                graph_snapshot_hash=graph_ref["content_hash"],
                canonical_rule_id="constant_true_disjunction",
                reduced_query_a=pattern_a,
                reduced_query_b=pattern_b,
                runtime_direction="b",
                site_class="where_boolean",
                submission_path="relative",
                reduced_validation_phenotype=phenotype,
            )
        )
        candidate_family = IssueFamilyIndex().add(candidate_b, issue_id="issue-2").family_id
        if (
            candidate_b.structural == candidate_c.structural
            and candidate_c.exact == continuity_fingerprint.exact
            and continuity_family_decision.family_id < candidate_family
        ):
            bridge_fingerprints = (candidate_b, candidate_c)
            break
    assert bridge_fingerprints is not None
    fingerprint_b, fingerprint_c = bridge_fingerprints
    bridge_index = IssueFamilyIndex()
    bridge_index.add(continuity_fingerprint, issue_id="issue-1")
    decision_b = bridge_index.add(fingerprint_b, issue_id="issue-2")
    decision_c = bridge_index.add(fingerprint_c, issue_id="issue-3")
    owner_record_before = continuity_registry_record
    owner_b_before = replace(
        continuity_registry_record,
        issue_id="issue-2",
        pair_hash=fingerprint_b.exact,
        reduced_query_a=renamed_a,
        reduced_query_b=renamed_b,
        family_id=str(decision_b.family_id),
        family_version_hash=decision_b.family_version_hash,
        family_decision_hash=decision_b.decision_hash,
        owner_issue_id="issue-2",
        exact_signature=fingerprint_b.exact,
        structural_signature=fingerprint_b.structural,
        fingerprint_hash=fingerprint_b.fingerprint_hash,
        submission_path=fingerprint_b.submission_path,
        reduced_phenotype=fingerprint_b.reduced_validation_phenotype,
    )
    bridge_record = replace(
        continuity_registry_record,
        issue_id="issue-3",
        stage=PipelineStage.REDUCED_VALID,
        family_id=str(decision_c.family_id),
        family_version_hash=decision_c.family_version_hash,
        family_decision_hash=decision_c.decision_hash,
        owner_issue_id=str(decision_c.owner_issue_id),
        exact_signature=fingerprint_c.exact,
        structural_signature=fingerprint_c.structural,
        fingerprint_hash=fingerprint_c.fingerprint_hash,
        submission_path=fingerprint_c.submission_path,
        reduced_phenotype=fingerprint_c.reduced_validation_phenotype,
    )
    union_records = _records_with_current_family(
        bridge_index,
        (owner_record_before, owner_b_before, bridge_record),
        family_id=str(decision_c.family_id),
    )
    _commit_family_transaction(
        run_dir / "issues",
        previous_records=(owner_record_before, owner_b_before),
        records=union_records,
    )
    assert len((run_dir / "issues" / "registry.jsonl").read_text().splitlines()) == 1
    assert len((run_dir / "issues" / "dedup_review.jsonl").read_text().splitlines()) == 2
    old_issue_payload = dict(continuity_issue.payload)
    continuity_status, continuity_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "bridge-renewed-developer.json",
    )
    assert continuity_status == "ok", continuity_result
    renewed_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert renewed_index["issue_ref"] != continuity_issue_ref
    renewed_issue = load_artifact_record(store, renewed_index["issue_ref"])
    assert renewed_issue.payload["original_validation_ref"] == original_ref
    assert renewed_issue.payload["final_validation_ref"] == pattern_validation_ref
    assert renewed_issue.payload["original_validation_hash"] == original_validation_hash
    assert renewed_issue.payload["reduced_validation_hash"] == pattern_validation.payload["report_hash"]
    assert not durable_family_payload_is_current(run_dir / "issues", old_issue_payload)
    assert len((run_dir / "issues" / "family_unions.jsonl").read_text().splitlines()) == 1

    for path, payload in original_files.items():
        path.write_bytes(payload)
    (run_dir / "issues" / "dedup_review.jsonl").unlink(missing_ok=True)
    (run_dir / "issues" / "family_unions.jsonl").unlink(missing_ok=True)

    synchronized_export = {
        **typed_export,
        "family_id": "0" * 64,
        "exact_signature": "1" * 64,
        "structural_signature": "2" * 64,
        "submission_path": "absolute",
        "reduced_validation_phenotype": "absolute:stable:a",
        "family_version_hash": "3" * 64,
        "family_decision_hash": "4" * 64,
        "original_validation_hash": "5" * 64,
        "reduced_validation_hash": "6" * 64,
        "original_validation_ref": final_ref,
        "reduced_validation_ref": original_ref,
    }
    export_path.write_text(json.dumps(synchronized_export) + "\n", encoding="utf-8")
    synchronized_receipt = {
        **receipt,
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(synchronized_receipt), encoding="utf-8")
    commit_path.write_text(
        json.dumps(
            {
                **synchronized_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    synchronized_status, synchronized_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "synchronized-developer.json",
    )
    assert synchronized_status == "error"
    assert "IssueGateError" in synchronized_result

    reference_tampered_export = {
        **typed_export,
        "original_validation_ref": final_ref,
    }
    export_path.write_text(json.dumps(reference_tampered_export) + "\n", encoding="utf-8")
    reference_tampered_receipt = {
        **receipt,
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(reference_tampered_receipt), encoding="utf-8")
    commit_path.write_text(
        json.dumps(
            {
                **reference_tampered_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    tampered_ref_status, tampered_ref_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "tampered-ref-developer.json",
    )
    assert tampered_ref_status == "error"
    assert "IssueGateError" in tampered_ref_result

    malformed_issue_payload = json.loads(json.dumps(issue.to_dict()))
    malformed_issue_payload["family_decision"]["family_members"] = [[]]
    malformed_issue_ref = asdict(
        store.put_json("malformed-family-members", malformed_issue_payload)
    )
    malformed_receipt = {
        **receipt,
        "issue_ref": malformed_issue_ref,
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(malformed_receipt), encoding="utf-8")
    commit_path.write_text(
        json.dumps(
            {
                **malformed_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    malformed_status, malformed_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "malformed-family-developer.json",
    )
    assert malformed_status == "error"
    assert "IssueGateError" in malformed_result

    export_path.write_text(json.dumps(typed_export) + "\n", encoding="utf-8")
    receipt["export_sha256"] = hashlib.sha256(export_path.read_bytes()).hexdigest()
    index_path.write_text(json.dumps(receipt), encoding="utf-8")
    commit_path.write_text(
        json.dumps(
            {
                **receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    replayed_index = IssueFamilyIndex()
    replayed_index.add(fingerprint, issue_id="issue-1")
    duplicate_decision = replayed_index.add(fingerprint, issue_id="issue-2")
    current_owner_decision = replayed_index.reissue_owner(
        fingerprint,
        issue_id="issue-1",
    )
    duplicate_record = replace(
        typed_registry_record,
        issue_id="issue-2",
        stage=PipelineStage.REDUCED_VALID,
        family_version_hash=duplicate_decision.family_version_hash,
        family_decision_hash=duplicate_decision.decision_hash,
    )
    duplicate_payload = asdict(duplicate_record)
    duplicate_payload["stage"] = duplicate_record.stage.value
    duplicate_payload["developer_status"] = duplicate_record.developer_status.value
    (run_dir / "issues" / "dedup_review.jsonl").write_text(
        json.dumps(duplicate_payload) + "\n",
        encoding="utf-8",
    )

    stale_status, stale_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "stale-developer.json",
    )
    assert stale_status == "error"
    assert "IssueGateError" in stale_result

    current_owner_record = replace(
        typed_registry_record,
        family_version_hash=current_owner_decision.family_version_hash,
        family_decision_hash=current_owner_decision.decision_hash,
    )
    current_clearance = IssueRegistry().duplicate_clearance_for_family(
        current_owner_decision,
        (current_owner_record, duplicate_record),
    )
    current_registry_payload = asdict(current_owner_record)
    current_registry_payload["stage"] = current_owner_record.stage.value
    current_registry_payload["developer_status"] = current_owner_record.developer_status.value
    (run_dir / "issues" / "registry.jsonl").write_text(
        json.dumps(current_registry_payload) + "\n",
        encoding="utf-8",
    )
    repaired_issue = ArtifactRecord.seal_issue(
        {
            **issue_body,
            "duplicate_clearance": {
                "issue_id": current_clearance.issue_id,
                "possible_duplicate": current_clearance.possible_duplicate,
                "cluster_members": current_clearance.cluster_members,
                "cluster_version_hash": current_clearance.cluster_version_hash,
                "evidence_url": current_clearance.evidence_url,
                "registry_signature": current_clearance.registry_signature,
                "family_id": current_clearance.family_id,
                "family_version_hash": current_clearance.family_version_hash,
                "family_decision_hash": current_clearance.family_decision_hash,
            },
            "family_decision": {
                "issue_id": current_owner_decision.issue_id,
                "owner_issue_id": current_owner_decision.owner_issue_id,
                "family_id": current_owner_decision.family_id,
                "fingerprint": asdict(current_owner_decision.fingerprint),
                "match_kind": current_owner_decision.match_kind.value,
                "matched_issue_id": current_owner_decision.matched_issue_id,
                "known_regression_match": current_owner_decision.known_regression_match,
                "known_regression_case_id": current_owner_decision.known_regression_case_id,
                "is_new_family": current_owner_decision.is_new_family,
                "family_members": current_owner_decision.family_members,
                "family_fingerprints": tuple(
                    (member, asdict(member_fingerprint))
                    for member, member_fingerprint in current_owner_decision.family_fingerprints
                ),
                "family_version_hash": current_owner_decision.family_version_hash,
                "decision_hash": current_owner_decision.decision_hash,
            },
        }
    )
    repaired_issue_ref = asdict(
        store.put_json("issue-1-repaired", repaired_issue.to_dict())
    )
    repaired_export = {
        **typed_export,
        "family_version_hash": current_owner_decision.family_version_hash,
        "family_decision_hash": current_owner_decision.decision_hash,
    }
    export_path.write_text(json.dumps(repaired_export) + "\n", encoding="utf-8")
    repaired_receipt = {
        **receipt,
        "issue_ref": repaired_issue_ref,
        "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(repaired_receipt), encoding="utf-8")
    commit_path.write_text(
        json.dumps(
            {
                **repaired_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(
                        current_registry_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    repaired_status, repaired_result = _issue_export_with_timeout(
        index_path,
        tmp_path / "repaired-developer.json",
    )
    assert repaired_status == "ok", repaired_result
    assert Path(repaired_result).is_file()

    forged_body = {key: value for key, value in issue.payload.items() if key != "issue_hash"}
    forged = ArtifactRecord.seal_issue({**forged_body, "final_validation_ref": original_ref})
    with pytest.raises(ValueError, match="reduced candidate"):
        verify_issue_hash_chain(store, forged)

    missing_candidate_ref = {
        "content_hash": "f" * 64,
        "relative_path": "objects/ff/" + "f" * 64,
        "size_bytes": 1,
        "media_type": "application/json",
        "schema_version": 1,
    }
    invalid_validation_body = {
        key: value for key, value in original.payload.items() if key != "validation_hash"
    }
    invalid_validation = ArtifactRecord.seal_validation(
        {**invalid_validation_body, "candidate_ref": missing_candidate_ref}
    )
    invalid_validation_ref = asdict(
        store.put_json("invalid-validation", invalid_validation.to_dict())
    )
    missing_chain = ArtifactRecord.seal_issue(
        {**forged_body, "original_validation_ref": invalid_validation_ref}
    )
    with pytest.raises(FileNotFoundError):
        verify_issue_hash_chain(store, missing_chain)


def test_complete_one_thousand_accepted_pair_worker_shard_stays_below_500_mb(tmp_path) -> None:
    class Adapter:
        target = TargetSystem.NEO4J

        def connectivity_check(self):
            return True, "connected"

        def ensure_schema(self):
            return None

        def load_gdsmith_graph(self, statements):
            return None

        def execute(self, query, parameters, query_kind, **kwargs):
            return ExecutionResult(
                system=self.target,
                query_kind=query_kind,
                rows=({"value": 1},),
                elapsed_ms=1.0,
                summary={"truncated": False},
            )

        def dbms_fingerprint(self):
            return DBMSFingerprint("a" * 64, server_version="test-dbms/1")

        def dbms_health_epoch(self):
            return DBMSHealthEpoch("e" * 64, "test")

        def close(self):
            return None

    queries = tuple(
        f"MATCH (a:L1:L2)--(b:L1:L2), (c:L1:L2)--(d:L1:L2) "
        f"WHERE a.value = {index} AND b.value = {index} RETURN a, b, c, d"
        for index in range(1000)
    )
    generated = GeneratedShard(
        graph_statements=("CREATE (n:L1:L2 {value: 1})",),
        query_texts=queries,
        generator_metadata={"backend": "gdsmith"},
    )
    spec = load_experiment_spec("config/experiment.yaml")
    runtime = dict(spec.runtime)
    runtime["screen"] = {"warmups": 0, "pairs": 5, "ratio": 2.0, "schedule": "randomized_ab_ba"}
    worker = RuntimeFirstShardWorker(
        replace(spec, runtime=runtime),
        generation_runner=lambda task, raw_dir: generated,
        adapter_factory=lambda task: Adapter(),
    )
    worker.bind_run_dir(tmp_path)

    result = worker(ShardTask(TargetSystem.NEO4J, 0, 7, 1000, 1000, 600))

    shard = tmp_path / "shards" / "neo4j-000"
    assert result["accepted_pairs"] == 1000
    assert result["acceptance_status"] == "complete"
    assert len((shard / "accepted_pairs.jsonl").read_text().splitlines()) == 1000
    assert sum(path.stat().st_size for path in shard.rglob("*") if path.is_file()) <= 500 * 1024 * 1024
