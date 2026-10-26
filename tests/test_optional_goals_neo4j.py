"""Dedicated-database validation of component products and NULL extension."""

import os
from collections import Counter

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
import ir
from main import compile_case, prepare_legacy_case as prepare_case, read_case, save_case
from test_optional_goal_acceptance import (matching_skeleton, optional_follow_skeleton,
                                           nested_optional_skeleton, optional_property_null_skeleton,
                                           optional_skeleton, per_input_population, mixed_scalar_population, walk)
from state import GraphSpec


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
    ("both", lambda: optional_skeleton("both", work=True)),
    ("left_only", lambda: optional_skeleton("left_only", work=True)),
    ("right_only", lambda: optional_skeleton("right_only", work=True)),
    ("neither", lambda: optional_skeleton("neither", work=True)),
    ("follow", optional_follow_skeleton),
    ("entity-alias-follow", lambda: optional_follow_skeleton(alias=True)),
    ("missing-property", optional_property_null_skeleton),
    ("nested-inner", lambda: nested_optional_skeleton("inner")),
    ("nested-outer", lambda: nested_optional_skeleton("outer")),
]


@pytest.mark.parametrize("name,maker", RAW_CASES, ids=[case[0] for case in RAW_CASES])
def test_native_optional_row_bindings_and_nested_sites_reach_the_database(name, maker, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(maker(), monkeypatch, config)
    if name == "neither":
        assert case["expected_rows"] == [{"u.uid": 1, "left.uid": None, "right.uid": None}]
    loaded(case, config, protocol)


@pytest.mark.parametrize("form", ["independent", "shared_anchor"])
def test_matching_component_structure_and_outer_count_demands_reach_the_database(form, monkeypatch):
    config, protocol = configuration(), settings()
    case = raw_case(matching_skeleton(form, threshold=5), monkeypatch, config)
    assert case["expected_rows"][0]["observed"] >= 6
    loaded(case, config, protocol)


def per_input_case(config):
    source, target, populated = per_input_population()
    return compile_case({"pattern": "P11", "paper_pattern": "P10", "pattern_catalog": "paper-v1",
        "pattern_form": "shared_anchor", "scale": 7, "seed": 0, "data_seed": 3, "complexity": 0,
        "state_variant": "original", "parameters": {}, "query_ir": {"a": ir.to_data(source), "b": ir.to_data(target)},
        "graph_spec": populated.spec.to_dict(), "setup": populated.setup, "checks": list(populated.checks),
        "expected_rows": populated.expected_rows, "cost_constraints": populated.constraints,
        "population_evidence": populated.evidence}, config)


def mixed_scalar_case(config):
    source, target, populated = mixed_scalar_population()
    return compile_case({"pattern": "P10", "paper_pattern": "P10", "pattern_catalog": "paper-v1",
        "pattern_form": "independent", "scale": 7, "seed": 0, "data_seed": 3, "complexity": 0,
        "state_variant": "original", "parameters": {}, "query_ir": {"a": ir.to_data(source), "b": ir.to_data(target)},
        "graph_spec": populated.spec.to_dict(), "setup": populated.setup, "checks": list(populated.checks),
        "expected_rows": populated.expected_rows, "cost_constraints": populated.constraints,
        "population_evidence": populated.evidence}, config)


def test_exact_two_by_three_component_counts_are_loaded_for_each_of_two_anchors():
    config, protocol = configuration(), settings()
    case = per_input_case(config)
    assert "compile_error" not in case, case.get("compile_error")
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for side in ("a", "b"):
            actual = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert actual["complete"] is True
            assert typed_bag(actual["rows"]) == typed_bag(case["expected_rows"])
            assert Counter(row["u.uid"] for row in actual["rows"]) == {1: 6, 2: 6}
        observed = database.execute("MATCH (u:InputRoot) RETURN u.uid AS uid, "
            "COUNT { MATCH (u)-[:LEFT_R]->() RETURN 1 } AS left_count, "
            "COUNT { MATCH (u)-[:RIGHT_R]->() RETURN 1 } AS right_count ORDER BY uid", {}, 30)
        assert observed["complete"] is True
        assert observed["rows"] == [{"uid": uid, "left_count": 2, "right_count": 3} for uid in (1, 2)]
    finally:
        database.close()


def test_false_and_zero_in_one_physical_label_domain_load_and_check_as_different_populations():
    config, protocol = configuration(), settings()
    case = mixed_scalar_case(config)
    assert "compile_error" not in case, case.get("compile_error")
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for side in ("a", "b"):
            actual = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert actual["complete"] is True
            assert typed_bag(actual["rows"]) == typed_bag(case["expected_rows"])
            assert len(actual["rows"]) == 6
            assert all(row["a_flag"] is False and type(row["b_flag"]) is int and row["b_flag"] == 0 for row in actual["rows"])
        observed = database.execute("MATCH (n:TypedDomain) RETURN n.flag AS flag, count(n) AS total", {}, 30)
        assert observed["complete"] is True
        assert typed_bag(observed["rows"]) == typed_bag([{"flag": False, "total": 2}, {"flag": 0, "total": 3}])
    finally:
        database.close()


@pytest.mark.parametrize("form", ["independent", "shared_anchor"])
@pytest.mark.parametrize("branch", ["then", "otherwise"])
def test_public_component_binding_policies_survive_recursive_conditionals_and_count(form, branch):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P10", 7, 0, 3, database=config,
        composition={"contexts": ["count", "with"], "threshold": 3, "form": form, "chain_length": 3,
                     "conditional_depth": 2, "guard_kind": "count", "branch_path": [branch]})
    loaded(case, config, protocol)


@pytest.mark.parametrize("state", ["both", "left_only", "right_only", "neither"])
def test_public_optional_states_preserve_the_exposed_null_uid_projection(state):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P11", 7, 0, 3, database=config,
                        composition={"contexts": [], "match_state": state})
    loaded(case, config, protocol)


@pytest.mark.parametrize("state", ["left_only", "neither"])
def test_public_optional_entity_facts_survive_else_nesting_and_count(state):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P11", 7, 0, 3, database=config,
        composition={"contexts": ["count", "with"], "threshold": 3, "chain_length": 3,
                     "match_state": state, "conditional_depth": 2, "guard_kind": "exists",
                     "branch_path": ["otherwise"]})
    loaded(case, config, protocol)


def test_an_unmoved_optional_supplies_ten_rows_without_faking_repeated_marked_work():
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P11", 7, 0, 3, database=config,
        composition={"contexts": ["count", "with"], "threshold": 9,
                     "chain_length": 3, "match_state": "neither"})
    work = case["construction_record"]["requirements"]["derivation"]["work"]
    assert work["repetitions"] == 1 and not work["nontrivial_repetition"]
    loaded(case, config, protocol)


def test_binding_facts_reject_a_new_optional_witness_even_when_full_count_results_stay_ten():
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P11", 7, 0, 3, database=config,
        composition={"contexts": ["count", "with"], "threshold": 9, "chain_length": 3, "match_state": "neither"})
    source = ir.from_data(case["query_ir"]["a"])
    branches = [part for _, part in walk(source) if isinstance(part, ir.Match) and part.optional][:2]
    graph = GraphSpec.from_dict(case["graph_spec"])
    nodes = {node["uid"]: node for node in graph.iter_nodes()}
    rejected = []
    for branch in branches:
        key, = {term.key for _, term in walk(branch.where)
                if isinstance(term, ir.Property) and term.variable == branch.pattern.right.variable}
        for edge in graph.iter_edges():
            candidate = nodes[edge["target"]]
            if (edge["type"] == branch.pattern.relationship.type
                    and set(branch.pattern.right.labels).issubset(candidate["labels"])
                    and candidate["properties"].get(key) is not True):
                rejected.append((edge["target"], key))
    assert rejected, "the accepted work axis must contain a real rejected candidate"
    uid, key = rejected[0]
    assert key.replace("_", "").isalnum()
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for check in case["bindings"]["binding_checks"]:
            before = database.execute(check["query"], case["parameters"], 30)
            assert before["complete"] is True
            assert typed_bag(before["rows"]) == typed_bag(check["expected_rows"])
            assert all(row["binding_fact"] is True for row in before["rows"])
        changed = database.execute(f"MATCH (n {{uid: $uid}}) SET n.{key} = true RETURN count(n) AS changed", {"uid": uid}, 30)
        assert changed["complete"] is True and changed["rows"] == [{"changed": 1}]
        binding_facts = [database.execute(check["query"], case["parameters"], 30)
                         for check in case["bindings"]["binding_checks"]]
        assert all(result["complete"] is True for result in binding_facts)
        assert any(row["binding_fact"] is False for result in binding_facts for row in result["rows"])
        for side in ("a", "b"):
            actual = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert actual["complete"] is True
            assert typed_bag(actual["rows"]) == typed_bag(case["expected_rows"])
        with pytest.raises(CaseFailure) as failure:
            check_case(database, case, protocol)
        assert failure.value.status == "cost_check_failed"
    finally:
        database.close()


@pytest.mark.parametrize("name", ["entity-alias-follow", "nested-inner", "third-output"])
def test_optional_binding_proofs_and_multiplicity_survive_run_archive_and_replay(name, tmp_path, monkeypatch):
    config, protocol = configuration(), settings()
    if name == "third-output":
        case = prepare_case(None, "P11", 7, 0, 3, database=config,
            composition={"contexts": ["count", "with"], "threshold": 9, "chain_length": 3, "match_state": "neither"})
    else:
        maker = dict(RAW_CASES)[name]
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

