"""Offline guardrails for the native PROFILE evidence in the SCor gate."""

import json

import pytest

from executor import CaseFailure
import test_gremlin_scor_integration as gate

from test_gremlin_scor_integration import _unselected_work


def _plain_profile(counts):
    return {"plan": [{"metrics": [{"name": "ConstantStep(901)", "id": "child", "counts": counts}]}]}


def test_missing_profile_branch_is_not_fabricated_as_zero_work():
    evidence = _unselected_work({"plan": [{"metrics": []}]}, "ConstantStep(901)")
    assert evidence == {"marker": "ConstantStep(901)", "status": "not_exposed", "steps": []}


def test_missing_native_counts_are_preserved_as_unexposed():
    evidence = _unselected_work(_plain_profile({}), "ConstantStep(901)")
    assert evidence["status"] == "not_exposed"
    assert evidence["steps"][0]["counts"] == {}
    assert evidence["steps"][0]["status"] == "counts_not_exposed"


def test_both_explicit_native_counters_are_required_to_establish_zero_work():
    evidence = _unselected_work(_plain_profile({"elementCount": 0, "traverserCount": 0}), "ConstantStep(901)")
    assert evidence["status"] == "checked"
    partial = _unselected_work(_plain_profile({"elementCount": 0}), "ConstantStep(901)")
    assert partial["status"] == "not_exposed"


def test_typed_janusgraph_metric_map_preserves_integer_counters():
    profile = {"plan": {"@type": "g:List", "@value": [{"@type": "g:TraversalMetrics", "@value": {
        "@type": "g:Map", "@value": ["metrics", {"@type": "g:List", "@value": [{
            "@type": "g:Metrics", "@value": {"@type": "g:Map", "@value": [
                "name", "ConstantStep(901)", "id", "child", "counts", {"@type": "g:Map", "@value": [
                    "elementCount", {"@type": "g:Int64", "@value": 0},
                    "traverserCount", {"@type": "g:Int64", "@value": 0}]}]}}]}]}}]}}
    evidence = _unselected_work(profile, "ConstantStep(901)")
    assert evidence["status"] == "checked"
    assert evidence["steps"][0]["counts"] == {"elementCount": 0, "traverserCount": 0}


@pytest.mark.parametrize("counts", [{"elementCount": 1}, {"traverserCount": 1}, {"elementCount": False}])
def test_nonzero_or_mistyped_native_counters_do_not_pass_as_zero_work(counts):
    with pytest.raises(AssertionError):
        _unselected_work(_plain_profile(counts), "ConstantStep(901)")


def _known_restriction(*, status="unsupported_feature", message=None, stop_target=False):
    return CaseFailure(status, "ApacheHugeGraph_server_400", stop_target=stop_target,
                       details={"server_message": gate._HUGEGRAPH_UNINDEXED_ROOT_MESSAGE if message is None else message})


def test_only_the_exact_observed_hugegraph_restriction_is_eligible_for_an_input_skip():
    assert gate._is_known_hugegraph_unindexed_root("hugegraph", _known_restriction())


@pytest.mark.parametrize("target,failure", [
    ("janusgraph", _known_restriction()),
    ("hugegraph", _known_restriction(status="query_error")),
    ("hugegraph", _known_restriction(status="timeout")),
    ("hugegraph", _known_restriction(stop_target=True)),
    ("hugegraph", _known_restriction(message=gate._HUGEGRAPH_UNINDEXED_ROOT_MESSAGE.replace("[uid]", "[key]"))),
    ("hugegraph", _known_restriction(message=gate._HUGEGRAPH_UNINDEXED_ROOT_MESSAGE.replace("'ScorRoot'", "'Other'"))),
    ("hugegraph", _known_restriction(message="NoIndexException")),
    ("hugegraph", ValueError(gate._HUGEGRAPH_UNINDEXED_ROOT_MESSAGE)),
])
def test_unrelated_unsupported_inputs_query_errors_and_timeouts_are_not_skippable(target, failure):
    assert not gate._is_known_hugegraph_unindexed_root(target, failure)


_CONFIG = {"database": {"dbms": "hugegraph", "language": "gremlin", "verified_features": ["gremlin_null_projection"]},
           "measurement": {"timeout_seconds": 1}}
_CASE = {"query_a": "g.inject(1).project('c0').by(__.identity())",
         "query_b": "g.inject(1).project('c0').by(__.identity())", "expected_rows": [{"value": 1}],
         "parameters": {}, "query_bindings": {"a": {}, "b": {}},
         "output_columns": {"a": [("c0", "value")], "b": [("c0", "value")]}}


class _FakeDatabase:
    version = "ApacheHugeGraph/test-only"
    closed = False

    def health(self):
        return True

    def restore(self, case):
        self.restored = case

    def close(self):
        self.closed = True


@pytest.mark.parametrize("failure,expected_status,expected_exception", [
    (_known_restriction(), "unsupported_feature", pytest.skip.Exception),
    (_known_restriction(status="query_error"), "failed", CaseFailure),
    (_known_restriction(status="timeout"), "failed", CaseFailure),
    (_known_restriction(message="different unsupported syntax"), "failed", CaseFailure),
])
def test_pattern_skip_preserves_input_hashes_failure_stage_and_original_details(
        monkeypatch, tmp_path, failure, expected_status, expected_exception):
    db = _FakeDatabase()
    monkeypatch.setattr(gate, "prepare_case", lambda *args, **kwargs: _CASE)
    monkeypatch.setattr(gate, "make_database", lambda *args: db)

    def inspect(*args, **kwargs):
        raise failure

    monkeypatch.setattr(gate, "_inspect_case", inspect)
    with pytest.raises(expected_exception):
        gate._check_pattern_interface(_CONFIG, tmp_path, "S1", 9, 0)
    record = json.loads((tmp_path / "pattern-S1-s9-c0.json").read_text())
    assert record["status"] == expected_status
    assert record["case"]["query_a"] == _CASE["query_a"]
    assert record["case"]["output_columns"] == {"a": [["c0", "value"]], "b": [["c0", "value"]]}
    assert record["compiled_query_hashes"] == gate._compiled_query_hashes(_CASE)
    assert record["failure"]["status"] == failure.status
    assert record["failure"]["details"] == failure.details
    assert record["failed_stage"] == "initial_build.check_state_execute_profile"
    assert db.closed


def test_independent_semantic_oracle_does_not_skip_a_provider_restriction(tmp_path):
    class Database(_FakeDatabase):
        def execute(self, *args):
            raise _known_restriction()

    with pytest.raises(CaseFailure):
        gate._check_semantic_case(Database(), _CONFIG, tmp_path, "semantic", "restriction",
                                  "RETURN 1 AS value", [{"value": 1}])
    assert json.loads((tmp_path / "semantic-restriction.json").read_text())["status"] == "failed"


def test_independent_mapping_oracle_does_not_skip_a_provider_restriction(monkeypatch, tmp_path):
    class Database(_FakeDatabase):
        def _reset(self):
            raise _known_restriction()

    monkeypatch.setattr(gate, "make_database", lambda *args: Database())
    with pytest.raises(CaseFailure):
        gate.test_native_property_mapping_is_injective_and_logically_transparent(_CONFIG, tmp_path)
    assert json.loads((tmp_path / "native-property-mapping.json").read_text())["status"] == "failed"


def test_compiled_hash_separates_query_text_from_bindings_and_projection_contracts():
    baseline = gate._compiled_query_hashes(_CASE)["a"]
    rebound = {**_CASE, "parameters": {"p0": 2}}
    renamed = {**_CASE, "output_columns": {**_CASE["output_columns"], "a": [("c0", "different")]}}
    for changed in (rebound, renamed):
        current = gate._compiled_query_hashes(changed)["a"]
        assert current["text_sha256"] == baseline["text_sha256"]
        assert current["program_sha256"] != baseline["program_sha256"]
