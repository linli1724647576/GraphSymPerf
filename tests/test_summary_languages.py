"""Language grouping must not promote unreviewed cases or duplicate roots."""

import csv
import json

import pytest

from summarize import BUG_FIELDS, load_records, main, summarize


def record(root, directory, case_id, *, pattern="P1", language=None, result_language=None,
           dbms="neo4j", version="Neo4j/5"):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    case = {"pattern": pattern}
    result = {"status": "candidate", "dbms": dbms, "database_version": version}
    if language is not None:
        case["language"] = language
    if result_language is not None:
        result["language"] = result_language
    data = {"format_version": 3 if language is not None else 1, "input_hash": case_id,
            "case": case, "result": result}
    (path / "result.json").write_text(json.dumps(data))
    return path


def annotations(root, rows=()):
    path = root / "bugs.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(BUG_FIELDS)
        writer.writerows(rows)
    return path


def annotation(case_id, *, bug="BUG001", status="confirmed", dbms="neo4j", version="Neo4j/5"):
    return (case_id, dbms, bug, status, "", version)


def manifest(root, directory, *, language=None, patterns=("P1",), elapsed=10):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    data = {"dbms": "neo4j", "database_version": "Neo4j/5", "kind": "discovery",
            "patterns": list(patterns), "budgets": {p: {"max_cases": 2, "max_seconds": 30} for p in patterns},
            "per_pattern": {p: {"attempted": 1, "elapsed_seconds": elapsed, "stop_reason": "case_budget"} for p in patterns}}
    if language is not None:
        data["language"] = language
    (path / "run.json").write_text(json.dumps(data))


def test_legacy_records_and_manifests_default_to_cypher(tmp_path):
    results = tmp_path / "results"
    record(results, "old/a", "1" * 64)
    manifest(results, "old")
    rows, matrix = summarize(results, annotations(tmp_path), tmp_path / "out")
    assert rows[0]["language"] == "cypher"
    assert rows[0]["budget_coverage"] == "recorded"
    assert rows[0]["elapsed_seconds"] == 10
    assert matrix == []
    with (tmp_path / "out/summary.csv").open() as handle:
        assert next(csv.DictReader(handle))["language"] == "cypher"
    assert BUG_FIELDS == ("case_id", "dbms", "bug_id", "status", "issue_url", "database_version")


def test_root_deduplication_across_languages_and_local_pattern_exclusivity(tmp_path):
    results = tmp_path / "results"
    inputs = [("1" * 64, "cypher", "P1"), ("2" * 64, "gremlin", "P1"), ("3" * 64, "gql", "P2")]
    for case_id, language, pattern in inputs:
        record(results, language, case_id, language=language, pattern=pattern)
    bugs = annotations(tmp_path, [annotation(case_id) for case_id, _, _ in inputs])
    rows, matrix = summarize(results, bugs, tmp_path / "out")
    assert {(r["language"], r["pattern"]) for r in rows} == {(lang, pat) for _, lang, pat in inputs}
    assert all(r["unique_bugs"] == r["exclusive_bugs"] == 1 for r in rows)
    assert len(matrix) == 1 and matrix[0]["P1"] == matrix[0]["P2"] == 1
    assert "language" not in matrix[0]


def test_exclusive_is_within_language_and_shared_patterns_remain_shared(tmp_path):
    results = tmp_path / "results"
    inputs = [("1" * 64, "cypher", "P1"), ("2" * 64, "cypher", "P2"), ("3" * 64, "gremlin", "P1")]
    for case_id, language, pattern in inputs:
        record(results, case_id, case_id, language=language, pattern=pattern)
    bugs = annotations(tmp_path, [annotation(case_id) for case_id, _, _ in inputs])
    rows, matrix = summarize(results, bugs, tmp_path / "out")
    assert all(r["exclusive_bugs"] == 0 for r in rows if r["language"] == "cypher")
    assert next(r for r in rows if r["language"] == "gremlin")["exclusive_bugs"] == 1
    selected, selected_matrix = summarize(results, bugs, tmp_path / "gremlin", language={"gremlin"})
    assert len(selected) == 1 and selected[0]["exclusive_bugs"] == 1
    assert selected_matrix[0]["P1"] == 1 and "P2" not in selected_matrix[0]
    assert len(matrix) == len(selected_matrix) == 1


def test_filter_does_not_credit_an_unreviewed_language_case(tmp_path):
    results = tmp_path / "results"
    record(results, "cypher", "1" * 64, language="cypher")
    record(results, "gremlin", "2" * 64, language="gremlin")
    bugs = annotations(tmp_path, [annotation("1" * 64), annotation("2" * 64, status="needs_review")])
    rows, matrix = summarize(results, bugs, tmp_path / "gremlin", language={"gremlin"})
    assert rows[0]["candidate"] == 1 and rows[0]["unique_bugs"] == rows[0]["exclusive_bugs"] == 0
    assert matrix == []
    assert summarize(results, bugs, tmp_path / "cypher", language={"cypher"})[0][0]["unique_bugs"] == 1


def test_language_filter_combines_with_dbms_and_checked_version(tmp_path):
    results = tmp_path / "results"
    record(results, "a", "1" * 64, language="gql", version="Neo4j/5")
    record(results, "b", "1" * 64, language="gql", version="Neo4j/6")
    record(results, "c", "2" * 64, language="cypher", version="Neo4j/6")
    record(results, "d", "3" * 64, language="gql", dbms="other", version="Other/1")
    bugs = annotations(tmp_path, [annotation("1" * 64), annotation("3" * 64, dbms="other", version="Other/1")])
    rows, matrix = summarize(results, bugs, tmp_path / "selected", dbms={"neo4j"}, version={"Neo4j/6"}, language={"gql"})
    assert len(rows) == 1 and rows[0]["language"] == "gql" and rows[0]["cases"] == 1
    assert rows[0]["unique_bugs"] == 0 and matrix == []


def test_language_runs_have_separate_detection_time_and_empty_coverage(tmp_path):
    results = tmp_path / "results"
    record(results, "cypher/a", "1" * 64, language="cypher")
    record(results, "gremlin/a", "2" * 64, language="gremlin")
    manifest(results, "cypher", language="cypher", elapsed=11)
    manifest(results, "gremlin", language="gremlin", elapsed=22)
    manifest(results, "gql", language="gql", patterns=("P16",), elapsed=0)
    rows, _ = summarize(results, annotations(tmp_path), tmp_path / "out")
    by_language = {r["language"]: r for r in rows}
    assert by_language["cypher"]["elapsed_seconds"] == 11
    assert by_language["gremlin"]["elapsed_seconds"] == 22
    assert by_language["gql"]["cases"] == 0 and by_language["gql"]["pattern"] == "P16"
    assert all(row["budget_coverage"] == "recorded" for row in rows)


def test_record_does_not_borrow_other_language_manifest_time(tmp_path):
    results = tmp_path / "results"
    record(results, "run/a", "1" * 64, language="gremlin")
    manifest(results, "run", language="cypher")
    rows, _ = summarize(results, annotations(tmp_path), tmp_path / "out", language={"gremlin"})
    assert len(rows) == 1 and rows[0]["budget_coverage"] == "missing_manifest"
    assert rows[0]["elapsed_seconds"] == ""


def test_same_hash_in_different_languages_is_not_annotatable(tmp_path):
    results = tmp_path / "results"
    record(results, "a", "1" * 64, language="cypher")
    record(results, "b", "1" * 64, language="gql")
    with pytest.raises(ValueError, match="conflicting languages"):
        summarize(results, annotations(tmp_path), tmp_path / "out", language={"cypher"})
    assert not (tmp_path / "out").exists()


def test_result_language_is_read_and_case_language_is_checked(tmp_path):
    results = tmp_path / "results"
    record(results, "a", "1" * 64, result_language="GQL")
    assert load_records(results)[0][0]["language"] == "gql"
    record(results, "a", "1" * 64, language="cypher", result_language="gremlin")
    with pytest.raises(ValueError, match="conflicting case and result languages"):
        load_records(results)


@pytest.mark.parametrize("value", [None, "", "  ", True, 3])
def test_invalid_explicit_language_does_not_silently_default(tmp_path, value):
    results = tmp_path / "results"
    path = record(results, "a", "1" * 64)
    data = json.loads((path / "result.json").read_text())
    data["result"]["language"] = value
    (path / "result.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="language must be a nonempty string"):
        load_records(results)


def test_cli_languages_are_repeatable_and_combine_with_existing_filters(tmp_path):
    results = tmp_path / "results"
    for number, language in enumerate(("cypher", "gremlin", "gql"), 1):
        record(results, language, str(number) * 64, language=language)
    bugs = annotations(tmp_path)
    output = tmp_path / "cli"
    assert main(["--results", str(results), "--bugs", str(bugs), "--output", str(output),
                 "--language", "Cypher", "--language", "GQL", "--dbms", "Neo4j", "--version", "Neo4j/5"]) == 0
    with (output / "summary.csv").open() as handle:
        assert {row["language"] for row in csv.DictReader(handle)} == {"cypher", "gql"}


def exclude(directory, value=False):
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / 'EXCLUDED.json'
    marker.write_text(json.dumps({'include_in_analysis': value, 'reason': 'isolated measurement'}))
    return marker


def test_explicit_exclusion_removes_cases_and_run_time_without_name_rules(tmp_path):
    results = tmp_path / 'results'
    record(results, 'actual/a', '1' * 64)
    manifest(results, 'actual', elapsed=10)
    record(results, 'arbitrary-name/deep/a', '2' * 64, pattern='P2')
    manifest(results, 'arbitrary-name', patterns=('P2',), elapsed=999)
    exclude(results / 'arbitrary-name')
    # Merely naming a folder like an archive must not discard valid history.
    record(results, 'pre-transport-fix/a', '3' * 64)
    manifest(results, 'pre-transport-fix', elapsed=20)
    rows, _ = summarize(results, annotations(tmp_path), tmp_path / 'out')
    assert len(rows) == 1 and rows[0]['pattern'] == 'P1'
    assert rows[0]['cases'] == 2 and rows[0]['elapsed_seconds'] == 30
    assert rows[0]['case_budget'] == 4 and rows[0]['budget_coverage'] == 'recorded'


def test_exclusion_at_selected_root_applies_to_all_records_and_runs(tmp_path):
    results = tmp_path / 'results'
    record(results, 'a', '1' * 64)
    manifest(results, 'run')
    exclude(results)
    assert load_records(results) == ([], [])


def test_parent_exclusion_cannot_be_overridden_by_nested_include(tmp_path):
    results = tmp_path / 'results'
    record(results, 'excluded/nested/a', '1' * 64)
    manifest(results, 'excluded/nested')
    exclude(results / 'excluded')
    exclude(results / 'excluded/nested', True)
    assert load_records(results) == ([], [])


def test_excluded_corrupt_result_and_run_are_not_loaded(tmp_path):
    results = tmp_path / 'results'
    case = record(results, 'isolated/a', '1' * 64)
    manifest(results, 'isolated')
    (case / 'result.json').write_text('not JSON')
    (results / 'isolated/run.json').write_text('not JSON')
    exclude(results / 'isolated')
    assert load_records(results) == ([], [])


def test_exclusion_above_requested_root_does_not_apply(tmp_path):
    results = tmp_path / 'outer/results'
    record(results, 'a', '1' * 64)
    manifest(results, 'run')
    exclude(results.parent)
    records, runs = load_records(results)
    assert len(records) == len(runs) == 1


def test_explicit_include_keeps_records(tmp_path):
    results = tmp_path / 'results'
    record(results, 'a', '1' * 64)
    exclude(results, True)
    assert len(load_records(results)[0]) == 1


@pytest.mark.parametrize('body', ['not JSON', '[]', '{}', '{"include_in_analysis": "false"}',
                                 '{"include_in_analysis": 0}', '{"include_in_analysis": null}'])
def test_malformed_exclusion_manifest_is_an_error(tmp_path, body):
    results = tmp_path / 'results'
    record(results, 'a', '1' * 64)
    (results / 'EXCLUDED.json').write_text(body)
    with pytest.raises(ValueError, match='EXCLUDED.json'):
        load_records(results)


def test_malformed_nested_marker_is_not_hidden_by_parent_exclusion(tmp_path):
    results = tmp_path / 'results'
    record(results, 'outer/nested/a', '1' * 64)
    exclude(results / 'outer')
    (results / 'outer/nested/EXCLUDED.json').write_text('{}')
    with pytest.raises(ValueError, match='include_in_analysis'):
        load_records(results)


def test_each_exclusion_manifest_is_read_once_for_many_descendant_cases(tmp_path, monkeypatch):
    import summarize as reporting

    results = tmp_path / 'results'
    for i in range(1, 5):
        record(results, f'run/nested/{i}', str(i) * 64)
    manifest(results, 'run')
    marker = exclude(results / 'run', True)
    calls, original = [], reporting._json

    def tracked(path):
        if path.name == 'EXCLUDED.json':
            calls.append(path)
        return original(path)

    monkeypatch.setattr(reporting, '_json', tracked)
    records, runs = load_records(results)
    assert len(records) == 4 and len(runs) == 1
    assert calls == [marker]
