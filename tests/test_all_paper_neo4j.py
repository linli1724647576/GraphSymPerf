"""Paper-wide semantic validation on an explicitly supplied disposable Neo4j."""

import os

import pytest

from executor import Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_case, read_case, save_case


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no dedicated Neo4j test target supplied")]

PAPER_CASES = [(f"P{i}", None) for i in range(1, 14)] + [(f"S{i}", None) for i in range(1, 11)]
PAPER_CASES += [("P10", "independent"), ("P10", "chain")]
CONTEXTS = ((), ("count",), ("exists",), ("with",), ("with", "exists", "count", "with"))


def configuration():
    return {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
            "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
            "dedicated": True, "allow_reset": True}


def settings():
    return measurement_settings({"discovery_pairs": 2, "discovery_warmups": 0,
                                 "confirmation_pairs": 4, "confirmation_warmups": 0,
                                 "control_pairs": 2, "bootstrap_samples": 100,
                                 "screen_ratio": 1_000_000, "timeout_seconds": 30})


@pytest.mark.parametrize("pattern,form", PAPER_CASES)
@pytest.mark.parametrize("contexts", CONTEXTS)
def test_each_paper_rule_and_p10_form_preserves_complete_results_after_loading(pattern, form, contexts):
    config, protocol = configuration(), settings()
    composition = {"contexts": list(contexts), "threshold": 0}
    if form is not None:
        composition["form"] = form
    case = prepare_case(None, pattern, 7, 0, 3, composition=composition, database=config)
    assert "compile_error" not in case, case.get("compile_error")
    assert case["pattern_catalog"] == "paper-v1"
    assert case["paper_pattern"] == pattern
    assert case["instantiation_record"]["unchanged_graph"] is True
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


@pytest.mark.parametrize("match_state", ["both", "left_only", "right_only", "neither"])
def test_p11_null_extension_states_match_the_constructed_complete_bag(match_state):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P11", 7, 0, 3,
                        composition={"contexts": [], "match_state": match_state}, database=config)
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
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
    ("P2", {}), ("P11", {"match_state": "left_only"}),
    ("S3", {}), ("S5", {}), ("S10", {}),
])
@pytest.mark.parametrize("contexts", [(), ("with", "exists", "count", "with")])
def test_new_semantic_families_run_archive_and_replay_without_regeneration(tmp_path, pattern, extra, contexts):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 7, 0, 3,
                        composition={"contexts": list(contexts), **extra}, database=config)
    assert "compile_error" not in case, case.get("compile_error")
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path)
    assert restored == case
    replayed = run_case(restored, config, saved_protocol)
    assert replayed["status"] == "no_candidate", replayed
    assert replayed["discovery"]["samples"]
