import json
import hashlib
from dataclasses import asdict
from dataclasses import replace

import pytest

from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import (
    ArtifactBudgetExceeded,
    ContentAddressedStore,
    enforce_shard_budget,
    verify_issue_hash_chain,
)
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.experiment.plan_models import ShardTask
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import GeneratedShard, RuntimeFirstShardWorker
from graph_sym_perf.issues.commands import IssueCommands
from graph_sym_perf.issues.registry import IssueRegistry, canonical_reduced_pair_hash
from graph_sym_perf.reducer.models import MinimalityProof, MinimalityUniverse
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.validation.models import (
    DeveloperStatus,
    FrozenMapping,
    PipelineStage,
    VersionedIssueRecord,
    candidate_hash_from_content_hashes,
    canonical_content_hash,
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
        certificate = {
            "rule_id": "constant_true_disjunction",
            "tier": "core",
            "proof_version": "kleene_or_true_v1",
            "site_class": "where_boolean",
        }
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
    certificate = {
        "rule_id": "constant_true_disjunction",
        "tier": "core",
        "proof_version": "kleene_or_true_v1",
        "site_class": "where_boolean",
    }
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
            },
        )
    )
    reproducer_ref = asdict(store.put("reproducer", b"CREATE ();"))
    registry_record = {
        "issue_id": "issue-1",
        "stage": "submit_ready",
        "developer_status": "waiting_reply",
        "dbms": "neo4j",
        "dbms_version": "dbms/1",
        "pair_hash": canonical_reduced_pair_hash(query_a, query_b),
        "direction": "b",
        "rule_id": "constant_true_disjunction",
        "detector_version": "runtime-first-v3",
        "validator_version": "independent-two-session-v1",
        "reducer_version": "paired-ast-ddmin-v1",
        "status_evidence_url": None,
        "rule_version": "kleene_or_true_v1",
        "discovered_at": "2026-07-11T00:00:00+00:00",
        "site_class": "where_boolean",
        "normalized_pattern": query_a,
        "normalized_predicate": query_b,
        "reduced_query_a": query_a,
        "reduced_query_b": query_b,
        "submitted_at": None,
    }
    registry = IssueRegistry()
    registry.record(
        VersionedIssueRecord(
            **{
                **registry_record,
                "stage": PipelineStage.SUBMIT_READY,
                "developer_status": DeveloperStatus.WAITING_REPLY,
            }
        )
    )
    clearance = registry.duplicate_clearance("issue-1")
    assert clearance is not None
    issue = ArtifactRecord.seal_issue(
        {
            "issue_id": "issue-1",
            "stage": "submit_ready",
            "reduction_status": "reduced_valid",
            "certificate": {
                "rule_id": "constant_true_disjunction",
                "tier": "core",
                "proof_version": "kleene_or_true_v1",
                "site_class": "where_boolean",
            },
            "duplicate_clearance": {
                "issue_id": "issue-1",
                "possible_duplicate": clearance.possible_duplicate,
                "cluster_members": clearance.cluster_members,
                "cluster_version_hash": clearance.cluster_version_hash,
                "evidence_url": clearance.evidence_url,
                "registry_signature": clearance.registry_signature,
            },
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
    issue_body = {key: value for key, value in issue.payload.items() if key != "issue_hash"}

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
    exported = IssueCommands().export(issue=index_path, output=tmp_path / "developer.json")
    assert json.loads(exported.read_text(encoding="utf-8"))["reduction_status"] == "reduced_valid"

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
