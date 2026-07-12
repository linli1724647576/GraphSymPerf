import pytest
import hashlib

from graph_sym_perf.artifacts.policy import ArtifactLevel, ArtifactPolicy
from graph_sym_perf.artifacts.schema import ArtifactRecord, ArtifactReference
from graph_sym_perf.validation.models import candidate_hash_from_content_hashes, canonical_content_hash


def _reference() -> dict[str, object]:
    return {
        "content_hash": "a" * 64,
        "relative_path": "objects/aa/" + "a" * 64,
        "size_bytes": 10,
        "media_type": "application/json",
        "schema_version": 1,
    }


def _candidate_payload() -> dict[str, object]:
    query_a = "MATCH (n) RETURN n"
    query_b = "MATCH (n) WHERE true RETURN n"
    certificate = {
        "rule_id": "constant_true_disjunction",
        "tier": "core",
        "proof_version": "kleene_or_true_v1",
        "site_class": "where_boolean",
    }
    content_hashes = {
        "query_a": hashlib.sha256(query_a.encode()).hexdigest(),
        "query_b": hashlib.sha256(query_b.encode()).hexdigest(),
        "parameters": canonical_content_hash({}),
        "graph_snapshot": "a" * 64,
        "dbms_fingerprint": canonical_content_hash("2" * 64),
        "dbms_version": "3" * 64,
        "resolved_config": "4" * 64,
        "discovery_slow_query": canonical_content_hash("b"),
        "discovery_ratio": canonical_content_hash(4.0),
        "validation_policy": "7" * 64,
        "certificate": canonical_content_hash(certificate),
    }
    return {
        "candidate_hash": candidate_hash_from_content_hashes(content_hashes),
        "content_hashes": content_hashes,
        "query_a": query_a,
        "query_b": query_b,
        "discovery_paired_timing": [{"order": "AB", "a_ms": 1.0, "b_ms": 4.0}],
        "graph_snapshot_ref": _reference(),
        "graph_snapshot_hash": "a" * 64,
        "parameters": {},
        "dbms_fingerprint": "2" * 64,
        "discovery_slow_query": "b",
        "discovery_median_ratio": 4.0,
        "rule_id": "constant_true_disjunction",
        "safety_certificate": certificate,
    }


def _session(session_id: str, process_id: int) -> dict[str, object]:
    query_a = "MATCH (n) RETURN n"
    query_b = "MATCH (n) WHERE true RETURN n"
    paired = [
        {
            "order": "AB" if index < 10 else "BA",
            "a_ms": 1.0,
            "b_ms": 4.0,
            "a_timeout": False,
            "b_timeout": False,
        }
        for index in range(20)
    ]
    control = [
        {
            "order": "AB" if index < 10 else "BA",
            "a_ms": 1.0,
            "b_ms": 1.0,
            "a_timeout": False,
            "b_timeout": False,
        }
        for index in range(20)
    ]
    return {
        "session_id": session_id,
        "process_id": process_id,
        "graph_fingerprint": "a" * 64,
        "dbms_fingerprint": "c" * 64,
        "query_a_hash": hashlib.sha256(query_a.encode()).hexdigest(),
        "query_b_hash": hashlib.sha256(query_b.encode()).hexdigest(),
        "slow_query": "b",
        "measurement_source": "ordinary_execute",
        "measurement_protocol": "ordinary-execute-20-pair-v1",
        "runtime_verdict": "pass",
        "runtime_reason_codes": [],
        "paired_timing": paired,
        "negative_controls": [
            {"kind": "A/A", "paired_timing": control},
            {"kind": "B/B", "paired_timing": control},
        ],
        "lifecycle": {"complete": True, "connection_id": f"connection-{session_id}"},
    }


def _validation_payload() -> dict[str, object]:
    return {
        "candidate_hash": "b" * 64,
        "candidate_ref": _reference(),
        "stage": "validation_passed",
        "validation_protocol": "independent-two-session-v1",
        "equivalence": {"equivalent": True, "complete_result": True, "reason_codes": []},
        "sessions": [_session("one", 1), _session("two", 2)],
        "environment": {
            "dbms_version": "dbms/1",
            "driver_version": "driver/1",
            "execution_mode": "container_image",
            "execution_artifact_digest": "sha256:" + "f" * 64,
            "cpu_architecture": "arm64",
            "config_hash": "d" * 64,
        },
        "dbms_fingerprint": "c" * 64,
        "validated_ratio": 4.0,
    }


def test_validation_session_requires_runtime_verdict_and_reason_codes() -> None:
    payload = _validation_payload()
    del payload["sessions"][0]["runtime_verdict"]

    with pytest.raises(ValueError, match="session evidence is incomplete"):
        ArtifactRecord.seal_validation(payload)


def _issue_payload() -> dict[str, object]:
    query_a = "MATCH (n) RETURN n"
    query_b = "MATCH (n) WHERE true RETURN n"
    return {
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
            "possible_duplicate": False,
            "cluster_members": ["issue-1"],
            "cluster_version_hash": "a" * 64,
            "evidence_url": None,
            "registry_signature": "c" * 64,
        },
        "validated_ratio": 4.0,
        "dbms_version": "dbms/1",
        "candidate_hash": "b" * 64,
        "reduced_candidate_hash": "d" * 64,
        "reduced_query_a": query_a,
        "reduced_query_b": query_b,
        "reduced_content_hashes": {
            "query_a": hashlib.sha256(query_a.encode()).hexdigest(),
            "query_b": hashlib.sha256(query_b.encode()).hexdigest(),
        },
        "reproducer_ref": _reference(),
        "original_validation_ref": _reference(),
        "final_validation_ref": _reference(),
        "reduction_ref": _reference(),
        "explanation_ref": _reference(),
    }


def test_policy_promotes_only_candidates_validation_and_issues() -> None:
    policy = ArtifactPolicy(shard_budget_mb=1)
    assert policy.level_for("accepted") is ArtifactLevel.MINIMAL
    assert policy.level_for("candidate") is ArtifactLevel.CANDIDATE
    assert policy.level_for("validation") is ArtifactLevel.VALIDATION
    assert policy.level_for("issue") is ArtifactLevel.ISSUE
    with pytest.raises(ValueError, match="unknown artifact stage"):
        policy.level_for("plan" + "_only")


def test_minimal_record_rejects_embedded_queries_or_profile_payloads() -> None:
    ArtifactRecord(
        level=ArtifactLevel.MINIMAL,
        payload={
            "query_hash": "a" * 64,
            "rule_id": "pattern_order_symmetry",
            "site": "clauses[0].pattern.components[0]",
            "equivalence": "complete_typed_equivalent",
            "screen_runtime_ms": 3.0,
            "error_kind": None,
        },
    )
    with pytest.raises(ValueError, match="minimal artifact"):
        ArtifactRecord(level=ArtifactLevel.MINIMAL, payload={"query": "MATCH (n) RETURN n"})
    with pytest.raises(ValueError, match="minimal artifact"):
        ArtifactRecord(level=ArtifactLevel.MINIMAL, payload={"profile_samples": [1, 2]})
    with pytest.raises(ValueError, match="query identity"):
        ArtifactRecord(level=ArtifactLevel.MINIMAL, payload={})


def test_candidate_validation_and_issue_levels_enforce_their_evidence_boundaries() -> None:
    candidate = ArtifactRecord(
        level=ArtifactLevel.CANDIDATE,
        payload=_candidate_payload(),
    )
    assert candidate.to_dict()["measurement_protocol"] == "execute-only-screen-v3"

    validation = ArtifactRecord.seal_validation(_validation_payload())
    assert validation.to_dict()["measurement_protocol"] == "independent-two-session-v1"

    issue = ArtifactRecord.seal_issue(_issue_payload())
    assert issue.to_dict()["measurement_protocol"] == "submit-ready-v3"

    with pytest.raises(ValueError, match="missing required fields"):
        ArtifactRecord(level=ArtifactLevel.CANDIDATE, payload={"candidate_hash": "b" * 64})
    with pytest.raises(ValueError, match="exactly two independent sessions"):
        ArtifactRecord.seal_validation({**_validation_payload(), "sessions": [_session("one", 1)]})
    with pytest.raises(ValueError, match="old measurement protocol"):
        ArtifactRecord(level=ArtifactLevel.ISSUE, payload=dict(issue.payload), measurement_protocol="profile-v1")
    with pytest.raises(ValueError, match="explanation-only"):
        ArtifactRecord(
            level=ArtifactLevel.CANDIDATE,
            payload={**dict(candidate.payload), "discovery_paired_timing": [{"profile_plan": {"rows": 1}}]},
        )
    with pytest.raises(ValueError, match="unknown fields"):
        ArtifactRecord(level=ArtifactLevel.ISSUE, payload={**dict(issue.payload), "screen_score": 99})
    with pytest.raises(ValueError, match="parameters does not match"):
        ArtifactRecord(level=ArtifactLevel.CANDIDATE, payload={**dict(candidate.payload), "parameters": {"x": 1}})
    duplicate_controls = _validation_payload()
    duplicate_controls["sessions"][0]["negative_controls"][1]["kind"] = "A/A"
    with pytest.raises(ValueError, match="exactly A/A and B/B"):
        ArtifactRecord.seal_validation(duplicate_controls)
    undersampled = _validation_payload()
    undersampled["sessions"][0]["paired_timing"] = undersampled["sessions"][0][
        "paired_timing"
    ][:1]
    with pytest.raises(ValueError, match="exactly 20 balanced paired samples"):
        ArtifactRecord.seal_validation(undersampled)
    forged_ratio = _validation_payload()
    forged_ratio["validated_ratio"] = 99.0
    with pytest.raises(ValueError, match="validated_ratio does not match raw samples"):
        ArtifactRecord.seal_validation(forged_ratio)
    forged_direction = _validation_payload()
    forged_direction["sessions"][0]["slow_query"] = "a"
    with pytest.raises(ValueError, match="slow_query contradicts raw samples"):
        ArtifactRecord.seal_validation(forged_direction)


def test_artifact_reference_rejects_parent_traversal_inside_relative_path() -> None:
    with pytest.raises(ValueError, match="path must be relative"):
        ArtifactReference("a" * 64, "objects/../outside", 1)
