"""Integration contracts between compilation, measurement, archive and summary."""

import csv
from copy import deepcopy
import json
import random

import pytest

import executor
import main
from executor import CaseFailure, measurement_settings, typed_bag
from ir import Alias, Call, Literal, Query, to_data
from languages import generate
from query import parse_query
from summarize import BUG_FIELDS, summarize
from test_executor import FakeDatabase, SETTINGS


TARGET = {"dbms": "janusgraph", "language": "gremlin", "verified_features": ["gremlin_null_projection"]}


def literal_case(*, different_results=False, reference=False):
    texts = {"a": "RETURN 1 AS value", "b": "RETURN 2 AS value" if different_results else "RETURN coalesce(null, 1) AS value"}
    if reference:
        texts["ref"] = "RETURN CASE WHEN true THEN 1 ELSE 0 END AS value"
    case = {"pattern": "P1", "seed": 7, "scale": 1, "setup": [], "checks": [],
            "parameters": {"unused": False}, "expected_rows": [{"value": 1}],
            "query_ir": {side: to_data(parse_query(text)) for side, text in texts.items()}}
    return main.compile_case(case, TARGET)


def typed_literal_case(first, second, *, reference=False, equivalent_second=False):
    case = literal_case()
    tree = lambda value: Query(None, None, (Alias(Literal(value), "value"),))
    original = tree(first)
    other = (Query(None, None, (Alias(Call("coalesce", (Literal(None), Literal(first))), "value"),))
             if equivalent_second else tree(second))
    case["expected_rows"] = [{"value": first}]
    case["query_ir"] = {"a": to_data(original), "b": to_data(other)}
    if reference:
        case["query_ir"]["ref"] = to_data(original)
    return main.compile_case(case, TARGET)


def fake_database(monkeypatch, *, echo_binding=False, acknowledged_timeout=False):
    """Reuse the established lifecycle fake, while recording parameter inputs."""
    class Database(FakeDatabase):
        instances = []

        def __init__(self, config, settings):
            super().__init__(config, settings)
            self.requests = []

        def health(self):
            return True

        def execute(self, text, parameters, timeout):
            self.calls.append(text)
            self.requests.append((text, deepcopy(parameters)))
            # The reference and both sides complete first, establishing a
            # valid result and a fast reference before the measurement timeout.
            if acknowledged_timeout and len(self.requests) > 3 and text == self.case["query_a"]:
                raise CaseFailure("timeout", "unit-test-server-598", stop_target=True,
                                  details={"cancellation_verified": True, "elapsed_lower_bound_ms": 1000.0})
            rows = [{"c0": parameters["_gsp_p0"]}] if echo_binding else [{"c0": 1}]
            return {"rows": rows, "complete": True, "elapsed_ms": 10.0 if text == self.case["query_b"] else 1.0}

        def explain(self, text, parameters, analyze=True):
            return {"format": "fake-for-unit-test", "analyze": analyze, "plan": []}

    monkeypatch.setattr(executor, "make_database", Database)
    return Database


def test_same_script_different_bindings_cannot_hide_semantic_mismatch(monkeypatch):
    case = literal_case(different_results=True)
    assert case["query_a"] == case["query_b"]
    assert case["query_bindings"]["a"] != case["query_bindings"]["b"]
    assert "compile_error" not in case
    database = fake_database(monkeypatch, echo_binding=True)
    report = executor.run_case(case, TARGET, SETTINGS)
    assert report["status"] == "result_mismatch"
    assert report["sample_a"] == [{"value": 1}] and report["sample_b"] == [{"value": 2}]
    assert report["discovery"]["samples"] == []
    assert [parameters["_gsp_p0"] for _, parameters in database.instances[0].requests] == [1, 2]
    assert all(parameters["unused"] is False for _, parameters in database.instances[0].requests)


def test_measurement_submits_side_specific_bindings_and_rechecks_results(monkeypatch):
    case = literal_case(different_results=True)
    database = fake_database(monkeypatch, echo_binding=True)
    db = database(TARGET, measurement_settings(SETTINGS))
    db.restore(case)
    executor._bind_outputs(db, case)
    samples = []
    with pytest.raises(CaseFailure, match="result_changed_during_measurement"):
        executor._measure(db, case, typed_bag(case["expected_rows"]), measurement_settings(SETTINGS),
                          random.Random(0), 2, samples)
    assert any(parameters["_gsp_p0"] == 2 for _, parameters in db.requests)
    assert samples[0]["b_status"] == "result_mismatch"


def test_validated_fast_reference_and_acknowledged_timeout_never_promote_gremlin(monkeypatch):
    case = literal_case(reference=True)
    database = fake_database(monkeypatch, acknowledged_timeout=True)
    report = executor.run_case(case, TARGET, SETTINGS)
    assert report["status"] == "timeout" and report["stop_target"]
    assert report["elapsed_lower_bound_ms"] == 1000.0
    assert report["cancellation_verified"] is True
    assert report["pair_kind"] == report["comparisons"]["ab"]["pair_kind"] == "explicit_traversal"
    assert report["comparisons"]["ab"]["status"] == "timeout"
    assert "timeout_candidate" not in json.dumps(report)
    assert database.instances[0]._reference_evidence["validated"] is True
    assert database.instances[0].closed


def test_confirmed_timing_signal_is_an_observation_not_a_bug_contribution(tmp_path, monkeypatch):
    case = literal_case()
    database = fake_database(monkeypatch)
    report = executor.run_case(case, TARGET, SETTINGS)
    assert report["status"] == "timing_observation"
    assert report["comparisons"]["ab"]["pair_kind"] == "explicit_traversal"
    assert report["comparisons"]["ab"]["status"] == "timing_observation"
    assert report["summary"]["median_ratio"] == 10.0
    assert len(database.instances) == 3 and all(db.closed for db in database.instances)
    result_root = tmp_path / "results"
    main.save_case(result_root / "case", case, measurement_settings(SETTINGS), report)
    annotations = tmp_path / "bugs.csv"
    with annotations.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(BUG_FIELDS)
    rows, matrix = summarize(result_root, annotations, tmp_path / "summary")
    assert len(rows) == 1 and rows[0]["language"] == "gremlin"
    assert rows[0]["timing_observations"] == 1 and rows[0]["valid"] == 1
    assert rows[0]["candidate"] == rows[0]["symmetry_candidates"] == rows[0]["reference_candidates"] == 0
    assert rows[0]["unique_bugs"] == rows[0]["exclusive_bugs"] == 0 and matrix == []


def test_gremlin_archive_roundtrips_bindings_and_same_target_replay_keeps_identity(tmp_path, monkeypatch):
    case = literal_case()
    original = tmp_path / "original"
    settings = measurement_settings(SETTINGS)
    main.save_case(original, case, settings, {"status": "no_candidate", "language": "gremlin"})
    assert {path.name for path in original.iterdir()} == {"query_a.gremlin", "query_b.gremlin", "setup.cypher", "result.json"}
    before = (original / "result.json").read_bytes()
    before_record = json.loads(before)
    restored, protocol = main.read_case(original)
    assert restored == case and protocol == settings
    assert restored["query_bindings"] == case["query_bindings"]
    fake_database(monkeypatch)
    assert main.replay(original, {"database": TARGET, "output": str(tmp_path / "replay")}) == 0
    replay_record = json.loads(next((tmp_path / "replay").rglob("result.json")).read_text())
    assert replay_record["input_hash"] == before_record["input_hash"]
    assert replay_record["case"] == before_record["case"]
    assert (original / "result.json").read_bytes() == before


def test_editing_archived_bindings_invalidates_the_input_hash(tmp_path):
    case = literal_case()
    main.save_case(tmp_path, case, {}, {"status": "no_candidate"})
    path = tmp_path / "result.json"
    record = json.loads(path.read_text())
    record["case"]["query_bindings"]["a"]["_gsp_p0"] = 999
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="saved inputs have changed"):
        main.read_case(tmp_path)


def test_recomputed_archive_hash_cannot_override_ir_literal_bindings(tmp_path):
    case = literal_case()
    main.save_case(tmp_path, case, {}, {"status": "no_candidate"})
    path = tmp_path / "result.json"
    record = json.loads(path.read_text())
    changed = deepcopy(case)
    changed["query_bindings"]["a"]["_gsp_p0"] = 999
    record["case"]["query_bindings"] = changed["query_bindings"]
    record["input_hash"] = main.case_digest(changed)
    record["extension_hash"] = main.extension_digest(changed)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="saved bindings differ from their query IR"):
        main.read_case(tmp_path)


def test_public_language_generator_consumes_verified_feature_iterator_once():
    compiled = generate(parse_query("MATCH (u:A) RETURN u.uid"), "janusgraph", "gremlin",
                        verified_features=iter(("gremlin_null_projection",)))
    assert compiled.columns == (("c0", "u.uid"),)
    assert "gremlin_null_projection" in compiled.required_features


@pytest.mark.parametrize("first,second", [(True, 1), (1, 1.0)])
def test_equal_python_values_with_different_types_are_different_query_inputs(first, second):
    case = typed_literal_case(first, second)
    assert case["query_a"] == case["query_b"]
    assert "compile_error" not in case
    assert not executor._same_query(executor._query_input(case, "a"), executor._query_input(case, "b"))


@pytest.mark.parametrize("first,second", [(True, 1), (1, 1.0)])
def test_reference_with_equal_python_value_cannot_replace_differently_typed_side(monkeypatch, first, second):
    case = typed_literal_case(first, second, reference=True)
    database = fake_database(monkeypatch, echo_binding=True)
    report = executor.run_case(case, TARGET, SETTINGS)
    assert report["status"] == "result_mismatch"
    assert type(report["sample_a"][0]["value"]) is type(first)
    assert type(report["sample_b"][0]["value"]) is type(second)
    # The B request must execute after the reference and A despite Python's
    # True == 1 and 1 == 1.0 equality rules.
    assert len(database.instances[0].requests) == 3
    assert type(database.instances[0].requests[-1][1]["_gsp_p0"]) is type(second)


@pytest.mark.parametrize("first,second", [(True, 1), (1, 1.0)])
def test_typed_binding_tamper_is_rejected_before_database_connection(monkeypatch, first, second):
    case = typed_literal_case(first, first, equivalent_second=True)
    case["query_bindings"]["a"]["_gsp_p0"] = second
    monkeypatch.setattr(executor, "make_database", lambda *args: pytest.fail("invalid binding reached database"))
    report = executor.run_case(case, TARGET, SETTINGS)
    assert report["status"] == "invalid_case"
    assert report["reason"] == "gremlin_text_or_bindings_differ_from_ir"


@pytest.mark.parametrize("first,second", [(True, 1), (1, 1.0)])
def test_recomputed_hash_still_cannot_change_binding_scalar_type(tmp_path, first, second):
    case = typed_literal_case(first, first, equivalent_second=True)
    main.save_case(tmp_path, case, {}, {"status": "no_candidate"})
    path = tmp_path / "result.json"
    record = json.loads(path.read_text())
    changed = deepcopy(case)
    changed["query_bindings"]["a"]["_gsp_p0"] = second
    record["case"]["query_bindings"] = changed["query_bindings"]
    record["input_hash"] = main.case_digest(changed)
    record["extension_hash"] = main.extension_digest(changed)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="saved bindings differ from their query IR"):
        main.read_case(tmp_path)


def test_measurement_does_not_attribute_differently_typed_query_time_to_reference(monkeypatch):
    # Both OR predicates always hold and return the same constant, but their
    # property comparisons retain different scalar binding types.
    trees = {"a": parse_query("MATCH (u) WHERE u.n = true OR true RETURN 1 AS value"),
             "b": parse_query("MATCH (u) WHERE u.n = 1 OR true RETURN 1 AS value")}
    case = literal_case()
    case["query_ir"] = {side: to_data(tree) for side, tree in trees.items()}
    case = main.compile_case(case, TARGET)
    assert case["query_a"] == case["query_b"]
    database = fake_database(monkeypatch)
    db = database(TARGET, measurement_settings(SETTINGS))
    db.restore(case)
    executor._bind_outputs(db, case)
    db._reference_query = executor._query_input(case, "a")
    db._reference_evidence = {"elapsed_ms": 1.0, "validated": True}
    samples = []
    slower = executor._query_input(case, "b")
    executor._measure(db, case, typed_bag(case["expected_rows"]), measurement_settings(SETTINGS),
                      random.Random(0), 2, samples, queries=(slower, slower))
    assert all(sample["a_ms"] == sample["b_ms"] == 10.0 for sample in samples)
    assert db._reference_evidence == {"elapsed_ms": 1.0, "validated": True}
