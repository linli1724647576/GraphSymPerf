"""Real execution of independently authored recursive-goal acceptance cases."""

import os

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_legacy_case as prepare_case, read_case, save_case
from test_recursive_goal_acceptance import (count_interval_skeleton, disjunction_skeleton,
                                            local_u_count_guard_skeleton, local_with_guard_skeleton, null_else_skeleton)


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


def loaded(case, config, protocol):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    first, second = case["construction_record"], case["instantiation_record"]
    assert first["algorithm"] == 1 and second["algorithm"] == 2
    assert first["assignment_digest"] == second["uses_construction_assignment"]
    assert first["graph_digest"] == second["graph_digest"]
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


@pytest.mark.parametrize("pattern,extra", [
    ("P10", {"form": "chain", "chain_length": 3, "conditional_depth": 3,
              "branch_path": ["then", "otherwise"], "guard_kind": "count"}),
    ("P10", {"form": "chain", "chain_length": 4, "conditional_depth": 2,
              "branch_path": ["otherwise"], "guard_kind": "exists"}),
    ("S3", {"chain_length": 3, "conditional_depth": 3,
             "branch_path": ["otherwise", "then"], "guard_kind": "count"}),
    ("S3", {"chain_length": 2, "conditional_depth": 2,
             "branch_path": ["otherwise"], "guard_kind": "property"}),
])
def test_matching_and_conditional_productions_mix_in_both_branch_directions(pattern, extra):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 7, 0, 3, database=config,
                        composition={"contexts": ["count", "with"], "threshold": 3, **extra})
    loaded(case, config, protocol)


RAW_CASES = ["count_intersection", "or_backtracking", "literal_null", "property_null", "local_with", "local_u"]


def raw_case(name, monkeypatch, config):
    import symbolic
    makers = {"count_intersection": count_interval_skeleton,
              "or_backtracking": disjunction_skeleton,
              "literal_null": null_else_skeleton,
              "property_null": lambda: null_else_skeleton(physical_property=True),
              "local_with": local_with_guard_skeleton, "local_u": local_u_count_guard_skeleton}
    chosen = makers[name]()
    monkeypatch.setattr(symbolic, "build_symbolic_skeleton", lambda *args, **kwargs: chosen)
    return prepare_case(None, "S3", 7, 0, 3, database=config, composition={"contexts": []})


@pytest.mark.parametrize("name", RAW_CASES)
def test_three_valued_facts_intervals_and_internal_aliases_reach_the_database(name, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(name, monkeypatch, config)
    if name == "count_intersection":
        assert case["expected_rows"][0]["number"] == 4
    if name == "property_null":
        assert any("IS NULL" in check["query"].upper() for check in case["bindings"]["goal_checks"])
        assert any("IS NULL" in check["query"].upper() and "COUNT(" in check["query"].upper()
                   for check in case["checks"])
    if name == "local_u":
        from query import parse_query
        assert all(parse_query(check["query"]).pattern.variable != "u" for check in case["bindings"]["goal_checks"])
    loaded(case, config, protocol)


def test_changing_null_to_false_is_rejected_even_when_the_query_pair_still_agrees(monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case("property_null", monkeypatch, config)
    null_fact, = (check for check in case["bindings"]["goal_checks"]
                  if "u.nullable = true" in check["query"] and "IS NULL" in check["query"])
    assert null_fact in case["bindings"]["required_state_checks"]
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        before = database.execute(null_fact["query"], case["parameters"], 30)
        assert before["complete"] is True
        assert typed_bag(before["rows"]) == typed_bag(null_fact["expected_rows"])

        changed = database.execute("MATCH (u:Anchor {id: 1}) SET u.nullable = false RETURN count(u) AS changed", {}, 30)
        assert changed["complete"] is True and changed["rows"] == [{"changed": 1}]
        after = database.execute(null_fact["query"], case["parameters"], 30)
        assert after["complete"] is True
        assert after["rows"] == [{**row, "premise": False} for row in before["rows"]]
        sides = [database.execute(case[f"query_{side}"], case["parameters"], 30) for side in ("a", "b")]
        assert all(result["complete"] is True for result in sides)
        assert typed_bag(sides[0]["rows"]) == typed_bag(sides[1]["rows"])
        with pytest.raises(CaseFailure) as rejected:
            check_case(database, case, protocol)
        assert rejected.value.status == "cost_check_failed"
    finally:
        database.close()


@pytest.mark.parametrize("seed", [0, 1])
def test_order_contrast_work_search_runs_and_replays_with_one_accepted_assignment(tmp_path, seed):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P10", 7, seed, 3, database=config,
                        composition={"contexts": ["count", "with"], "threshold": 3,
                                     "form": "chain", "chain_length": 4})
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path)
    assert restored == case
    replay = run_case(restored, config, saved_protocol)
    assert replay["status"] == "no_candidate", replay
    assert replay["discovery"]["samples"]


@pytest.mark.parametrize("name", ["property_null", "local_with"])
def test_nullable_state_and_local_producer_checks_survive_full_archive_replay(tmp_path, name, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(name, monkeypatch, config)
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path)
    assert restored == case
    replay = run_case(restored, config, saved_protocol)
    assert replay["status"] == "no_candidate", replay
    assert replay["discovery"]["samples"]
