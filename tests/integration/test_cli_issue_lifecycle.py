import json
import hashlib
from types import SimpleNamespace

from graph_sym_perf.cli.__main__ import main
from graph_sym_perf.issues.registry import canonical_reduced_pair_hash
from graph_sym_perf.issues.commands import _current_issue_registry


def _registry_record(issue_id="issue-1"):
    query_a = "RETURN 1"
    query_b = "RETURN 1 AS x"
    return {
        "issue_id": issue_id,
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


def test_issue_export_requires_a_submit_ready_payload(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"
    assert main(["issue", "export", "--issue", str(missing), "--output", str(tmp_path / "out.json")]) != 0
    assert "missing gate" in capsys.readouterr().out


def test_issue_export_rejects_raw_json_that_only_claims_submit_ready(tmp_path, capsys) -> None:
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"stage": "submit_ready", "reduction_status": "reduced_valid"}), encoding="utf-8")
    output = tmp_path / "out.json"

    assert main(["issue", "export", "--issue", str(forged), "--output", str(output)]) != 0
    assert not output.exists()
    assert "typed SubmitReadyIssue" in capsys.readouterr().out


def test_issue_export_copies_only_a_preexported_typed_issue_with_verified_chain(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    index_dir = run_dir / "issues" / "index"
    export_dir = run_dir / "issues" / "submit-ready"
    index_dir.mkdir(parents=True)
    export_dir.mkdir(parents=True)
    exported = {
        "issue_id": "issue-1",
        "stage": "submit_ready",
        "reduction_status": "reduced_valid",
        "rule_id": "constant_true_disjunction",
        "validated_ratio": 4.0,
        "dbms_version": "dbms/1",
        "reduced_original": "RETURN 1",
        "reduced_transformed": "RETURN 1 AS x",
        "family_id": "f" * 64,
        "exact_signature": "e" * 64,
        "structural_signature": None,
        "submission_path": "relative",
        "reduced_validation_phenotype": "relative:stable:b",
        "family_version_hash": "c" * 64,
        "family_decision_hash": "d" * 64,
        "original_validation_hash": "1" * 64,
        "reduced_validation_hash": "2" * 64,
    }
    payload = (json.dumps(exported) + "\n").encode()
    typed_export = export_dir / "issue-1.json"
    typed_export.write_bytes(payload)
    issue_ref = {
        "content_hash": "a" * 64,
        "relative_path": "objects/aa/" + "a" * 64,
        "size_bytes": 1,
        "media_type": "application/json",
        "schema_version": 1,
    }
    index = index_dir / "issue-1.json"
    receipt = {
        "artifact_store": "shards/neo4j-000/artifacts",
        "export_path": "issues/submit-ready/issue-1.json",
        "export_sha256": hashlib.sha256(payload).hexdigest(),
        "issue_id": "issue-1",
        "issue_ref": issue_ref,
    }
    index.write_text(json.dumps(receipt), encoding="utf-8")
    registry_record = _registry_record()
    (run_dir / "issues" / "registry.jsonl").write_text(json.dumps(registry_record) + "\n", encoding="utf-8")
    commits = run_dir / "issues" / "commits"
    commits.mkdir()
    (commits / "issue-1.json").write_text(
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
    clearance = _current_issue_registry(run_dir / "issues").duplicate_clearance("issue-1")
    assert clearance is not None
    monkeypatch.setattr(
        "graph_sym_perf.issues.commands.load_artifact_record",
        lambda _store, _ref: SimpleNamespace(
            level="issue",
            payload={
                "issue_id": "issue-1",
                "reduction_status": "reduced_valid",
                "certificate": {"rule_id": "constant_true_disjunction"},
                "validated_ratio": 4.0,
                "dbms_version": "dbms/1",
                "reduced_query_a": "RETURN 1",
                "reduced_query_b": "RETURN 1 AS x",
                "family_id": "f" * 64,
                "original_validation_hash": "1" * 64,
                "reduced_validation_hash": "2" * 64,
                "family_decision": {
                    "family_version_hash": "c" * 64,
                    "decision_hash": "d" * 64,
                    "fingerprint": {
                        "exact": "e" * 64,
                        "structural": None,
                        "submission_path": "relative",
                        "reduced_validation_phenotype": "relative:stable:b",
                    },
                },
                "duplicate_clearance": {
                    "issue_id": clearance.issue_id,
                    "possible_duplicate": clearance.possible_duplicate,
                    "cluster_members": clearance.cluster_members,
                    "cluster_version_hash": clearance.cluster_version_hash,
                    "evidence_url": clearance.evidence_url,
                    "registry_signature": clearance.registry_signature,
                },
            },
        ),
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.commands.verify_issue_hash_chain",
        lambda _store, _issue, **_kwargs: None,
    )
    monkeypatch.setattr(
        "graph_sym_perf.issues.commands._verify_issue_registry_binding",
        lambda *_args: None,
    )
    destination = tmp_path / "developer-issue.json"

    assert main(["issue", "export", "--issue", str(index), "--output", str(destination)]) == 0

    assert destination.read_bytes() == payload

    forged = {**exported, "reduction_status": "reduction_partial"}
    forged_payload = (json.dumps(forged) + "\n").encode()
    typed_export.write_bytes(forged_payload)
    forged_receipt = {**receipt, "export_sha256": hashlib.sha256(forged_payload).hexdigest()}
    index.write_text(json.dumps(forged_receipt), encoding="utf-8")
    (commits / "issue-1.json").write_text(
        json.dumps(
            {
                **forged_receipt,
                "registry_record_hash": hashlib.sha256(
                    json.dumps(registry_record, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    forged_destination = tmp_path / "forged-developer-issue.json"

    assert main(["issue", "export", "--issue", str(index), "--output", str(forged_destination)]) != 0
    assert not forged_destination.exists()


def test_issue_export_refuses_a_submit_ready_index_revoked_by_late_dedup(tmp_path) -> None:
    run_dir = tmp_path / "run"
    index_dir = run_dir / "issues" / "index"
    export_dir = run_dir / "issues" / "submit-ready"
    index_dir.mkdir(parents=True)
    export_dir.mkdir(parents=True)
    payload = b'{"issue_id":"issue-1","stage":"submit_ready"}\n'
    (export_dir / "issue-1.json").write_bytes(payload)
    index = index_dir / "issue-1.json"
    index.write_text(
        json.dumps(
            {
                "artifact_store": "shards/neo4j-000/artifacts",
                "export_path": "issues/submit-ready/issue-1.json",
                "export_sha256": hashlib.sha256(payload).hexdigest(),
                "issue_id": "issue-1",
                "issue_ref": {
                    "content_hash": "a" * 64,
                    "relative_path": "objects/aa/" + "a" * 64,
                    "size_bytes": 1,
                    "media_type": "application/json",
                    "schema_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "issues" / "revocations.jsonl").write_text(
        json.dumps({"issue_id": "issue-1", "reason": "possible_duplicate"}) + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "should-not-exist.json"

    assert main(["issue", "export", "--issue", str(index), "--output", str(destination)]) != 0

    assert not destination.exists()
