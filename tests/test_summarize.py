import csv
import json
from pathlib import Path

import pytest

from summarize import BUG_FIELDS, main, summarize


def record(root, directory, case_id, pattern="P1", status="candidate", dbms="neo4j", version="Neo4j/5"):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    data = {"format_version": 1, "input_hash": case_id,
            "case": {"pattern": pattern, "seed": 1, "scale": 100},
            "result": {"status": status, "dbms": dbms, "database_version": version}}
    (path / "result.json").write_text(json.dumps(data))
    return path


def annotations(root, rows=()):
    path = root / "bugs.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(BUG_FIELDS)
        for row in rows:
            if len(row) == 5:
                row = (*row, "Memgraph/3" if row[1] == "memgraph" else "Neo4j/5")
            writer.writerow(row)
    return path


def manifest(root, directory, patterns=("P1",), kind="discovery", *, attempted=2, elapsed=10, reason="case_budget"):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    data = {"format_version": 1, "dbms": "neo4j", "database_version": "Neo4j/5", "kind": kind,
            "patterns": list(patterns),
            "budgets": {p: {"max_cases": 2, "max_seconds": 30} for p in patterns},
            "per_pattern": {p: {"attempted": attempted, "executed": attempted,
                                "elapsed_seconds": elapsed, "stop_reason": reason} for p in patterns}}
    (path / "run.json").write_text(json.dumps(data))


def test_replays_and_versions_do_not_multiply_cases_or_bugs(tmp_path):
    results = tmp_path / "results"
    identity = "1" * 64
    record(results, "run/a", identity)
    record(results, "run/b", identity, status="not_confirmed")
    record(results, "replay/a", identity)
    manifest(results, "run")
    manifest(results, "replay", kind="replay", attempted=100, elapsed=100)
    bugs = annotations(tmp_path, [(identity, "neo4j", "BUG001", "reproduced", "")])
    rows, matrix = summarize(results, bugs, tmp_path / "out")
    row = rows[0]
    assert (row["cases"], row["valid"], row["candidate"], row["unique_bugs"]) == (1, 1, 1, 1)
    assert (row["attempted"], row["elapsed_seconds"], row["case_budget"]) == (2, 10, 2)
    assert row["versions"] == "Neo4j/5"
    assert row["bugs_per_hour"] == 360
    assert matrix == [{"dbms": "neo4j", "bug_id": "BUG001", "status": "reproduced", "issue_url": "", "P1": 1}]


def test_shared_bug_attribution_and_dbms_identity(tmp_path):
    results = tmp_path / "results"
    a, b = "1" * 64, "2" * 64
    record(results, "a", a, "P1")
    record(results, "b", b, "S1")
    record(results, "c", a, "P1", dbms="memgraph", version="Memgraph/3")
    bugs = annotations(tmp_path, [(a, "neo4j", "BUG001", "reproduced", ""),
                                  (b, "neo4j", "BUG001", "confirmed", "https://example.org/1"),
                                  (a, "memgraph", "BUG001", "fixed", "")])
    rows, matrix = summarize(results, bugs, tmp_path / "out")
    neo = [r for r in rows if r["dbms"] == "neo4j"]
    assert len(neo) == 2 and all(r["unique_bugs"] == 1 and r["exclusive_bugs"] == 0 for r in neo)
    assert next(r for r in rows if r["dbms"] == "memgraph")["exclusive_bugs"] == 1
    assert len(matrix) == 2
    assert next(r for r in matrix if r["dbms"] == "neo4j")["status"] == "confirmed"
    assert all(r["budget_coverage"] == "missing_manifest" and r["bugs_per_hour"] == "" for r in rows)


def test_manifest_preserves_patterns_with_no_results(tmp_path):
    results = tmp_path / "results"
    manifest(results, "run", patterns=("P1", "P16"), attempted=0, elapsed=0, reason="unsupported_feature")
    rows, matrix = summarize(results, annotations(tmp_path), tmp_path / "out")
    assert [r["pattern"] for r in rows] == ["P1", "P16"]
    assert all(r["attempted"] == r["cases"] == r["valid"] == 0 for r in rows)
    assert all(r["stop_reasons"] == "unsupported_feature" and r["budget_coverage"] == "recorded" for r in rows)
    assert matrix == []


def test_candidates_are_not_bugs_and_unchecked_case_not_credited(tmp_path):
    results = tmp_path / "results"
    a, b, c = "1" * 64, "2" * 64, "3" * 64
    record(results, "a", a, "P1")
    record(results, "b", b, "P2", status="timeout_candidate")
    record(results, "c", c, "P3")
    bugs = annotations(tmp_path, [(a, "neo4j", "BUG001", "confirmed", ""),
                                  (b, "neo4j", "BUG001", "needs_review", "")])
    rows, matrix = summarize(results, bugs, tmp_path / "out")
    by_pattern = {r["pattern"]: r for r in rows}
    assert by_pattern["P1"]["unique_bugs"] == by_pattern["P1"]["exclusive_bugs"] == 1
    assert by_pattern["P2"]["unique_bugs"] == by_pattern["P2"]["valid"] == 0
    assert by_pattern["P2"]["candidate"] == 1
    assert by_pattern["P3"]["unique_bugs"] == 0
    assert matrix[0]["P2"] == matrix[0]["P3"] == 0


@pytest.mark.parametrize("rows,match", [
    ([("a" * 64, "neo4j", "BUG001", "confirmed", "")], "not recorded"),
    ([("1" * 64, "neo4j", "BUG001", "maybe", "")], "status is invalid"),
    ([("1" * 64, "neo4j", "BUG001", "confirmed", ""),
      ("1" * 64, "neo4j", "BUG001", "rejected", "")], "conflicting status"),
])
def test_invalid_annotations_fail_before_writing_outputs(tmp_path, rows, match):
    results = tmp_path / "results"
    record(results, "a", "1" * 64)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match=match):
        summarize(results, annotations(tmp_path, rows), output)
    assert not output.exists()


def test_prefix_ambiguity_and_exact_record_filters(tmp_path):
    results = tmp_path / "results"
    a, b = "12345678" + "a" * 56, "12345678" + "b" * 56
    record(results, "a", a, "P1")
    record(results, "b", b, "P2")
    with pytest.raises(ValueError, match="ambiguous"):
        summarize(results, annotations(tmp_path, [("12345678", "neo4j", "BUG001", "confirmed", "")]), tmp_path / "bad")
    record(results, "b", b, "P2", version="Neo4j/6")
    bugs = annotations(tmp_path, [(a[:9], "neo4j", "BUG001", "confirmed", "")])
    rows, matrix = summarize(results, bugs, tmp_path / "out", dbms={"neo4j"}, version={"Neo4j/5"})
    assert len(rows) == 1 and rows[0]["pattern"] == "P1" and len(matrix) == 1
    assert main(["--results", str(results), "--bugs", str(bugs), "--output", str(tmp_path / "cli"), "--version", "Neo4j/6"]) == 0
    with (tmp_path / "cli" / "summary.csv").open() as handle:
        assert next(csv.DictReader(handle))["pattern"] == "P2"


def test_legacy_dbms_requires_version_evidence(tmp_path):
    results = tmp_path / "results"
    path = record(results, "a", "1" * 64)
    file = path / "result.json"
    data = json.loads(file.read_text())
    del data["result"]["dbms"]
    file.write_text(json.dumps(data))
    assert summarize(results, annotations(tmp_path), tmp_path / "out")[0][0]["dbms"] == "neo4j"
    del data["result"]["database_version"]
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cannot be inferred"):
        summarize(results, annotations(tmp_path), tmp_path / "bad")


def test_mixed_legacy_results_do_not_claim_complete_manifest_coverage(tmp_path):
    results = tmp_path / "results"
    record(results, "run/a", "1" * 64)
    record(results, "old/a", "2" * 64)
    manifest(results, "run")
    row = summarize(results, annotations(tmp_path), tmp_path / "out")[0][0]
    assert row["budget_coverage"] == "missing_manifest"
    assert row["elapsed_seconds"] == row["bugs_per_hour"] == ""


def test_replay_only_input_cannot_borrow_discovery_time(tmp_path):
    results = tmp_path / "results"
    a, b = "1" * 64, "2" * 64
    record(results, "run/a", a, status="no_candidate")
    # The nested replay manifest owns b, rather than the discovery ancestor.
    record(results, "run/replay/b", b)
    manifest(results, "run", attempted=1, elapsed=10)
    manifest(results, "run/replay", kind="replay", attempted=1, elapsed=20)
    bugs = annotations(tmp_path, [(b, "neo4j", "BUG001", "confirmed", "")])
    row = summarize(results, bugs, tmp_path / "out")[0][0]
    assert row["cases"] == 2 and row["unique_bugs"] == 1
    assert row["budget_coverage"] == "missing_manifest"
    assert row["elapsed_seconds"] == row["bugs_per_hour"] == ""


def test_annotation_requires_actual_manual_check_of_selected_version(tmp_path):
    results = tmp_path / "results"
    identity = "1" * 64
    record(results, "old/a", identity, version="Neo4j/5")
    record(results, "new/a", identity, status="no_candidate", version="Neo4j/6")
    bugs = annotations(tmp_path, [(identity, "neo4j", "BUG001", "confirmed", "", "Neo4j/5")])
    rows, matrix = summarize(results, bugs, tmp_path / "new", version={"Neo4j/6"})
    assert rows[0]["cases"] == 1 and rows[0]["unique_bugs"] == 0 and matrix == []
    rows, matrix = summarize(results, bugs, tmp_path / "old", version={"Neo4j/5"})
    assert rows[0]["unique_bugs"] == 1 and len(matrix) == 1
    bugs = annotations(tmp_path, [(identity, "neo4j", "BUG001", "confirmed", "", "Neo4j/7")])
    with pytest.raises(ValueError, match="not recorded.*Neo4j/7"):
        summarize(results, bugs, tmp_path / "invalid")


def test_checked_versions_of_same_bug_still_count_once(tmp_path):
    results = tmp_path / "results"
    identity = "1" * 64
    record(results, "old/a", identity, version="Neo4j/5")
    record(results, "new/a", identity, version="Neo4j/6")
    bugs = annotations(tmp_path, [(identity, "neo4j", "BUG001", "reproduced", "", "Neo4j/5"),
                                  (identity, "neo4j", "BUG001", "confirmed", "", "Neo4j/6")])
    rows, matrix = summarize(results, bugs, tmp_path / "all")
    assert rows[0]["cases"] == rows[0]["unique_bugs"] == 1 and len(matrix) == 1
    assert summarize(results, bugs, tmp_path / "new", version={"Neo4j/6"})[0][0]["unique_bugs"] == 1


def test_replay_only_new_version_has_no_discovery_yield(tmp_path):
    results = tmp_path / "results"
    identity = "1" * 64
    record(results, "run/a", identity)
    record(results, "replay/a", identity, version="Neo4j/6")
    manifest(results, "run", attempted=1, elapsed=10)
    manifest(results, "replay", kind="replay")
    row = summarize(results, annotations(tmp_path), tmp_path / "out")[0][0]
    assert row["cases"] == 1 and row["budget_coverage"] == "missing_manifest"
    assert row["elapsed_seconds"] == row["bugs_per_hour"] == ""


@pytest.mark.parametrize("reason,status", [("running", "no_candidate"), ("pending", "no_candidate"),
                                           ("case_budget", "started")])
def test_interrupted_run_does_not_report_complete_time_budget(tmp_path, reason, status):
    results = tmp_path / "results"
    record(results, "run/a", "1" * 64, status=status)
    manifest(results, "run", reason=reason)
    row = summarize(results, annotations(tmp_path), tmp_path / "out")[0][0]
    assert row["budget_coverage"] == "incomplete_run"
    assert row["elapsed_seconds"] == row["bugs_per_hour"] == ""
