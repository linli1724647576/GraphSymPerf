"""Explicit short-circuit direction and bound runtime evidence gates."""

from copy import deepcopy

import pytest

import executor
from ast_pattern.contracts import StageFailure, digest
from ast_pattern.evidence_adapter import runtime_evidence, validate_dependency_case
from executor import run_case
from test_executor import CASE, SETTINGS, FakeDatabase


@pytest.fixture
def database(monkeypatch):
    class Database(FakeDatabase):
        instances = []
    monkeypatch.setattr(executor, "Neo4j", Database)
    return Database


@pytest.mark.parametrize("direction,roles,reference,duration_b,status,slow", [
    ("L_to_H", {"a": "L", "b": "H"}, "a", 5.0, "candidate", "b"),
    ("H_to_R", {"a": "H", "b": "R"}, "b", .2, "candidate", "a"),
    ("H_to_R", {"a": "H", "b": "R"}, "b", 5.0, "no_candidate", None),
    ("L_to_H", {"a": "L", "b": "H"}, "a", .2, "no_candidate", None),
])
def test_reference_gate_uses_declared_side_roles(database, direction, roles, reference, duration_b, status, slow):
    case = {**deepcopy(CASE), "pattern": "S7", "mutation_direction": direction,
            "side_roles": roles, "reference_side": reference}
    database.duration_b = duration_b
    report = run_case(case, {}, SETTINGS)
    assert report["status"] == status, report
    assert report["comparisons"]["ab"]["expected_slow_side"] == ("b" if direction == "L_to_H" else "a")
    if slow:
        assert report["summary"]["slow_side"] == slow


def test_legacy_short_circuit_records_keep_the_original_h_to_reference_direction(database):
    case = {**deepcopy(CASE), "pattern": "S7"}
    report = run_case(case, {}, SETTINGS)
    assert report["status"] == "no_candidate"
    assert report["reference_direction"] == "reference_not_faster"


def test_conflicting_direction_and_reference_are_rejected_before_loading(database):
    case = {**deepcopy(CASE), "mutation_direction": "L_to_H", "reference_side": "b"}
    report = run_case(case, {}, SETTINGS)
    assert report["status"] == "invalid_case"
    assert database.instances == []


def bound_case():
    from main import prepare_case
    return prepare_case(None, "P4", 3, 0, 3, ast_options={"max_attempts": 64})


def test_new_case_needs_semantic_certificate_before_database_creation(database):
    case = {**deepcopy(CASE), "method_version": "ast-pattern-v1"}
    assert run_case(case, {}, SETTINGS)["status"] == "invalid_case"
    assert database.instances == []


def test_tampered_query_or_binding_cannot_reuse_a_bound_dependency_certificate():
    for field, value in (("bindings", {"changed": True}), ("query_ir", {}), ("graph_spec", {"version": 2})):
        case = bound_case()
        case[field] = value
        with pytest.raises(StageFailure, match="contract_drift"):
            validate_dependency_case(case)


def test_semantic_unknown_cannot_enter_the_normal_performance_protocol(database):
    case = bound_case()
    static = case["dependency_report"]["static"]
    static["result_relation"] = "unknown"
    static["certificate_digest"] = digest({key: value for key, value in static.items() if key != "certificate_digest"})
    assert run_case(case, {}, SETTINGS)["status"] == "invalid_case"
    assert database.instances == []


def test_completed_execution_attaches_evidence_but_keeps_physical_mapping_unknown(database):
    case = bound_case()
    before = deepcopy(case)
    report = run_case(case, {}, SETTINGS)
    assert report["status"] == "candidate", report
    evidence = report["dependency_evidence"]
    assert evidence["result_relation"] == "proved"
    assert evidence["physical_plan_mapping"] == "unknown"
    assert evidence["physical_invocations"] == "unknown"
    assert case == before


def test_unbound_or_incomplete_runtime_evidence_cannot_claim_observed_equivalence():
    with pytest.raises(StageFailure, match="contract_drift"):
        runtime_evidence({"report": {}}, graph_digest="g", query_digest="q", target_id="t")
    evidence = runtime_evidence({"graph_digest": "g", "query_digest": "q", "report": {"status": "incomplete_result"}},
                                graph_digest="g", query_digest="q", target_id="t")
    assert evidence["result_relation"] == "unknown"
    assert evidence["status"] == "unverified"


def test_later_result_failure_overrides_an_initial_successful_result_check():
    evidence = runtime_evidence({"graph_digest": "g", "query_digest": "q", "report": {
        "status": "result_mismatch", "result_check": "passed"}},
        graph_digest="g", query_digest="q", target_id="t")
    assert evidence["result_relation"] == "refuted"


@pytest.mark.parametrize("field,value", [
    ("query_a", "RETURN 999 AS forged"), ("query_b", "RETURN 999 AS forged"),
    ("parameters", {"threshold": 99}), ("query_bindings", {"a": {"literal": 99}}),
    ("output_columns", {"a": [["forged", "forged"]]}),
    ("required_features", {"a": ["invented_feature"]}),
])
def test_changed_executable_text_parameters_or_compiler_metadata_never_connects(database, field, value):
    case = bound_case()
    case[field] = value
    report = run_case(case, {"dbms": "neo4j", "language": "cypher"}, SETTINGS)
    assert report["status"] == "invalid_case", report
    assert report["reason"] == "M6/contract_drift"
    assert database.instances == []


@pytest.mark.parametrize("mutation", ["delete_stage", "change_stage_hash", "drop_all_stages", "drop_method_version"])
def test_a_valid_m6_certificate_cannot_replace_the_complete_stage_chain(database, mutation):
    case = bound_case()
    if mutation == "delete_stage":
        case["stage_records"].pop(1)
    elif mutation == "change_stage_hash":
        case["stage_records"][0]["output_digest"] = "changed"
    elif mutation == "drop_all_stages":
        case.pop("stage_records")
    else:
        case.pop("method_version")
    report = run_case(case, {}, SETTINGS)
    assert report["status"] == "invalid_case", report
    assert report["reason"] == "M6/contract_drift"
    assert database.instances == []
