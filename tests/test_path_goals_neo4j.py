"""Dedicated Neo4j checks for native bounded-path semantic goals."""

import os

import pytest

import ir
from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_legacy_case as prepare_case, read_case, save_case
from state import GraphSpec
from test_path_goal_acceptance import (aliased_truth_skeleton, counted_skeleton, path_body,
                                      skeleton, truth_skeleton)


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no dedicated Neo4j test target supplied")]


def configuration():
    return {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
            "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
            "dedicated": True, "allow_reset": True}


def settings():
    return measurement_settings({"discovery_pairs": 2, "discovery_warmups": 0,
        "confirmation_pairs": 4, "confirmation_warmups": 0, "control_pairs": 2,
        "bootstrap_samples": 100, "screen_ratio": 1_000_000, "timeout_seconds": 30})


def raw_case(item, monkeypatch, *, scale=7):
    import symbolic
    monkeypatch.setattr(symbolic, "build_symbolic_skeleton", lambda *args, **kwargs: item)
    return prepare_case(None, "P3", scale, 0, 3, database=configuration(), composition={"contexts": []})


def loaded(case):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    assert case["construction_record"]["assignment_digest"] == case["instantiation_record"]["uses_construction_assignment"]
    assert case["construction_record"]["graph_digest"] == case["instantiation_record"]["graph_digest"]
    assert all(check in case["checks"] for check in case["bindings"].get("required_state_checks", ()))
    database = Neo4j(configuration(), settings())
    try:
        database.restore(case)
        check_case(database, case, settings())
        for side in ("a", "b"):
            result = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert result["complete"] is True
            assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
    finally:
        database.close()


@pytest.mark.parametrize("contexts", [[], ["exists"], ["count"], ["count", "with"]])
@pytest.mark.parametrize("bounds", [(1, 3), (2, 4)])
def test_public_path_semantic_goals_and_outer_observers_reach_the_database(contexts, bounds):
    case = prepare_case(None, "P3", 7, 0, 3, database=configuration(),
        composition={"contexts": contexts, "path_bounds": bounds, "threshold": 3})
    loaded(case)


@pytest.mark.parametrize("branch", ["then", "otherwise"])
@pytest.mark.parametrize("guard", ["count", "exists", "property"])
def test_public_paths_survive_recursive_conditionals_on_either_child(branch, guard):
    case = prepare_case(None, "P3", 7, 0, 3, database=configuration(), composition={
        "contexts": ["count", "with"], "threshold": 3, "path_bounds": [2, 4],
        "conditional_depth": 2, "branch_path": [branch], "guard_kind": guard})
    loaded(case)


@pytest.mark.parametrize("desired", [True, False, None])
@pytest.mark.parametrize("negated", [False, True])
def test_all_true_false_and_unknown_match_neo4j_on_actual_path_nodes(desired, negated, monkeypatch):
    case = raw_case(truth_skeleton(desired, negated=negated), monkeypatch)
    assert all(row["truth"] is desired for row in case["expected_rows"])
    loaded(case)


@pytest.mark.parametrize("desired", [True, False, None])
def test_all_truth_is_preserved_by_real_with_aliases(desired, monkeypatch):
    case = raw_case(aliased_truth_skeleton(desired), monkeypatch)
    assert all(row["observed"] is desired for row in case["expected_rows"])
    loaded(case)


def test_exact_path_length_and_count_lower_bound_share_one_real_population(monkeypatch):
    item = counted_skeleton(threshold=4, length=2)
    case = raw_case(item, monkeypatch)
    assert case["expected_rows"][0]["n"] >= 5
    loaded(case)


def test_negated_node_predicate_is_not_special_cased_to_keep_false(monkeypatch):
    predicate = ir.Negation(ir.Comparison(ir.Property("x", "disabled"), "=", ir.Literal(True)))
    case = raw_case(skeleton(path_body(predicate=predicate, bounds=(2, 4), projection="bindings")), monkeypatch)
    loaded(case)


def test_expanded_six_thousand_candidate_family_preserves_real_count_and_length(monkeypatch):
    case = raw_case(counted_skeleton(threshold=4, length=2), monkeypatch, scale=6000)
    assert GraphSpec.from_dict(case["graph_spec"]).counts()["nodes"] > 5000
    loaded(case)


def test_false_prefix_tampering_is_rejected_even_while_both_outer_queries_still_pass(monkeypatch):
    item = counted_skeleton(threshold=4, length=2, alias=False)
    case = raw_case(item, monkeypatch)
    nodes = list(GraphSpec.from_dict(case["graph_spec"]).iter_nodes())
    failed = [node for node in nodes if node["properties"].get("keep") is False]
    assert failed, "the grown family must be rejected by an actual predicate valuation"
    database = Neo4j(configuration(), settings())
    try:
        database.restore(case)
        check_case(database, case, settings())
        path_checks = case["bindings"]["path_checks"]
        assert path_checks
        for check in path_checks:
            before = database.execute(check["query"], case["parameters"], 30)
            assert before["complete"] is True
            assert typed_bag(before["rows"]) == typed_bag(check["expected_rows"])
        changed = database.execute("MATCH (n) WHERE n.uid IN $uids SET n.keep = true RETURN count(n) AS changed",
                                   {"uids": [node["uid"] for node in failed]}, 30)
        assert changed["complete"] is True and changed["rows"] == [{"changed": len(failed)}]
        facts = [database.execute(check["query"], case["parameters"], 30) for check in path_checks]
        assert all(fact["complete"] is True for fact in facts)
        assert any(typed_bag(fact["rows"]) != typed_bag(check["expected_rows"])
                   for fact, check in zip(facts, path_checks))
        for side in ("a", "b"):
            result = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert result["complete"] is True
            assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
        with pytest.raises(CaseFailure):
            check_case(database, case, settings())
    finally:
        database.close()


def test_generated_case_survives_the_existing_run_save_read_and_replay_flow(tmp_path):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P3", 7, 0, 3, database=config, composition={
        "contexts": ["count", "with"], "threshold": 3, "path_bounds": [2, 4],
        "conditional_depth": 2, "branch_path": ["otherwise"], "guard_kind": "count"})
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    save_case(tmp_path / "path", case, protocol, report)
    restored, restored_protocol = read_case(tmp_path / "path")
    assert restored == case
    replay = run_case(restored, config, restored_protocol)
    assert replay["status"] == "no_candidate", replay
