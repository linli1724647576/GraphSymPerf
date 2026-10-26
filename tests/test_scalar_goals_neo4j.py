"""Dedicated-database validation of independent scalar goal cases."""

import os

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_legacy_case as prepare_case, read_case, save_case
from test_scalar_goal_acceptance import (PUBLIC_CASES, alias_rebinding_skeleton, boolean_skeleton, case_dual_skeleton,
                                         coalesce_skeleton, nested_case_skeleton, nested_coalesce_skeleton,
                                         nested_query_skeleton)


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no dedicated Neo4j test target supplied")]


def configuration():
    return {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
            "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
            "dedicated": True, "allow_reset": True}


def settings():
    return measurement_settings({"discovery_pairs": 2, "discovery_warmups": 0,
                                 "confirmation_pairs": 4, "confirmation_warmups": 0,
                                 "control_pairs": 2, "bootstrap_samples": 100,
                                 "screen_ratio": 1_000_000, "timeout_seconds": 30})


def raw_case(item, monkeypatch, config):
    import symbolic
    monkeypatch.setattr(symbolic, "build_symbolic_skeleton", lambda *args, **kwargs: item)
    return prepare_case(None, item.paper_pattern, 7, 0, 3, database=config, composition={"contexts": []})


def loaded(case, config, protocol):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    construction, instantiation = case["construction_record"], case["instantiation_record"]
    assert construction["algorithm"] == 1 and instantiation["algorithm"] == 2
    assert construction["assignment_digest"] == instantiation["uses_construction_assignment"]
    assert construction["graph_digest"] == instantiation["graph_digest"]
    assert all(check in case["checks"] for check in case["bindings"].get("required_state_checks", ()))
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for side in ("a", "b"):
            result = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert result["complete"] is True
            assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
    finally:
        database.close()


RAW_CASES = [
    ("S7-zero", lambda: coalesce_skeleton(0), 0),
    ("S7-false", lambda: coalesce_skeleton(False), False),
    ("S7-empty-string", lambda: coalesce_skeleton(""), ""),
    ("S7-property-zero", lambda: coalesce_skeleton(0, physical=True), 0),
    ("S7-property-false", lambda: coalesce_skeleton(False, physical=True), False),
    ("S7-property-empty", lambda: coalesce_skeleton("", physical=True), ""),
    ("S7-float", lambda: coalesce_skeleton(1.5), 1.5),
    ("S7-property-float", lambda: coalesce_skeleton(1.5, physical=True), 1.5),
    ("P13-false-int", lambda: case_dual_skeleton(False, 11), 11),
    ("P13-null-int", lambda: case_dual_skeleton(None, 11), 11),
    ("P13-false-string", lambda: case_dual_skeleton(False, "kept"), "kept"),
    ("P13-null-string", lambda: case_dual_skeleton(None, "kept"), "kept"),
    ("P13-false-null", lambda: case_dual_skeleton(False, None), None),
    ("P13-null-null", lambda: case_dual_skeleton(None, None), None),
    ("P13-null-float", lambda: case_dual_skeleton(None, 1.5), 1.5),
    ("S7-mark-inner", lambda: nested_coalesce_skeleton("inner"), 0),
    ("S7-mark-outer", lambda: nested_coalesce_skeleton("outer"), 0),
    ("S4-mark-inner", lambda: nested_case_skeleton("inner"), 0),
    ("S4-mark-outer", lambda: nested_case_skeleton("outer"), 23),
    ("alias-rebinding", alias_rebinding_skeleton, 0),
    ("nested-projection", lambda: nested_query_skeleton("projection"), 0),
    ("nested-where", lambda: nested_query_skeleton("where"), 0),
    ("P4-and", lambda: boolean_skeleton("P4"), False),
    ("P5-or", lambda: boolean_skeleton("P5"), True),
    ("S1-or", lambda: boolean_skeleton("S1"), True),
    ("S2-and", lambda: boolean_skeleton("S2"), False),
]


@pytest.mark.parametrize("name,maker,value", RAW_CASES, ids=[item[0] for item in RAW_CASES])
def test_general_scalar_values_and_recursive_marks_reach_the_database(name, maker, value, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(maker(), monkeypatch, config)
    assert typed_bag(case["expected_rows"]) == typed_bag([{"u.uid": 1, "value": value}])
    loaded(case, config, protocol)


@pytest.mark.parametrize("pattern,kind,value,wrappers,length", PUBLIC_CASES)
def test_public_scalar_productions_and_row_contexts_share_the_existing_database_pipeline(pattern, kind, value, wrappers, length):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 7, 0, 3, database=config,
                        composition={"contexts": ["count", "with"], "scalar_type": kind,
                                     "scalar_value": value, "scalar_contexts": wrappers, "chain_length": length})
    loaded(case, config, protocol)


@pytest.mark.parametrize("changed_property,value", [("prefix", 0), ("middle", 1)])
def test_loaded_scalar_facts_reject_tampering_even_when_the_query_pair_still_agrees(changed_property, value, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(coalesce_skeleton(0, physical=True), monkeypatch, config)
    fragment = "(u.prefix IS NULL)" if changed_property == "prefix" else "(u.middle = 0)"
    fact, = (check for check in case["bindings"]["goal_checks"] if fragment in check["query"])
    assert fact in case["bindings"]["required_state_checks"]
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        before = database.execute(fact["query"], case["parameters"], 30)
        assert before["complete"] is True
        assert typed_bag(before["rows"]) == typed_bag(fact["expected_rows"])
        changed = database.execute(f"MATCH (u:ScalarAnchor {{id: 1}}) SET u.{changed_property} = $value RETURN count(u) AS changed",
                                   {"value": value}, 30)
        assert changed["complete"] is True and changed["rows"] == [{"changed": 1}]
        after = database.execute(fact["query"], case["parameters"], 30)
        assert after["complete"] is True
        assert after["rows"] == [{**row, "premise": False} for row in before["rows"]]
        sides = [database.execute(case[f"query_{side}"], case["parameters"], 30) for side in ("a", "b")]
        assert all(result["complete"] is True for result in sides)
        assert typed_bag(sides[0]["rows"]) == typed_bag(sides[1]["rows"])
        if changed_property == "prefix":
            assert typed_bag(sides[0]["rows"]) == typed_bag(case["expected_rows"])
        with pytest.raises(CaseFailure) as rejected:
            check_case(database, case, protocol)
        assert rejected.value.status == "cost_check_failed"
    finally:
        database.close()


@pytest.mark.parametrize("name,maker", [
    ("property-zero", lambda: coalesce_skeleton(0, physical=True)),
    ("null-dual-string", lambda: case_dual_skeleton(None, "kept")),
    ("rebound-alias", alias_rebinding_skeleton),
])
def test_scalar_assignment_required_facts_and_values_survive_full_archive_replay(name, maker, tmp_path, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(maker(), monkeypatch, config)
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path / name, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path / name)
    assert restored == case
    assert all(check in restored["checks"] for check in restored["bindings"].get("required_state_checks", ()))
    replay = run_case(restored, config, saved_protocol)
    assert replay["status"] == "no_candidate", replay
    assert replay["discovery"]["samples"]

