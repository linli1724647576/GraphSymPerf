from __future__ import annotations

import json

from graph_sym_perf.experiment.state import ErrorKind
from graph_sym_perf.reliability.findings import ReliabilityFindingStore


def test_entry_findings_are_versioned_and_do_not_collide_with_legacy_query_scope(
    tmp_path,
) -> None:
    path = tmp_path / "reliability" / "findings.jsonl"
    path.parent.mkdir(parents=True)
    legacy = {
        "kind": ErrorKind.DBMS_CRASHED.value,
        "query_hash": "q" * 64,
        "graph_hash": "g" * 64,
        "dbms_fingerprint": "a" * 64,
        "dbms_version": "neo4j/test",
        "reproductions": 1,
        "attempt_evidence": ["entries/legacy/attempt-000"],
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    store = ReliabilityFindingStore(tmp_path)

    query = store.record(
        kind=ErrorKind.DBMS_CRASHED,
        query_hash="q" * 64,
        graph_hash="g" * 64,
        dbms_fingerprint="a" * 64,
        dbms_version="neo4j/test",
        attempt_evidence="entries/legacy/attempt-001",
    )
    entry = store.record_entry(
        kind=ErrorKind.DBMS_CRASHED,
        entry_id="p1:neo4j:discovery:rule:256:0",
        graph_hash="g" * 64,
        dbms_fingerprint="a" * 64,
        dbms_version="neo4j/test",
        observed_identity={
            "dbms_fingerprint": "b" * 64,
            "dbms_health_epoch": "e" * 64,
        },
        attempt_evidence="entries/current/attempt-000",
    )

    assert query.reproductions == 2
    assert entry.reproductions == 1
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["schema_version"] == 2
    assert records[-1]["scope"] == "entry"
    assert records[-1]["query_hash"] is None


def test_entry_finding_reproduction_dedupes_on_observed_identity(tmp_path) -> None:
    store = ReliabilityFindingStore(tmp_path)
    arguments = {
        "kind": ErrorKind.INFRASTRUCTURE_ERROR,
        "entry_id": "p1:neo4j:discovery:rule:256:0",
        "graph_hash": "g" * 64,
        "dbms_fingerprint": "a" * 64,
        "dbms_version": None,
        "observed_identity": {"graph_snapshot_hash": "h" * 64},
    }

    first = store.record_entry(
        **arguments,
        attempt_evidence="entries/current/attempt-000",
    )
    second = store.record_entry(
        **arguments,
        attempt_evidence="entries/current/attempt-001",
    )

    assert first.reproductions == 1
    assert second.reproductions == 2
    assert second.attempt_evidence == (
        "entries/current/attempt-000",
        "entries/current/attempt-001",
    )
