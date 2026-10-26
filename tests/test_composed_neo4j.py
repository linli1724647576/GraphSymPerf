"""Real execution only on an explicitly supplied disposable Neo4j database."""

import os

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case
from main import prepare_case, read_case, save_case


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
                                 "screen_ratio": 1_000_000, "timeout_seconds": 20})


@pytest.mark.parametrize("contexts,threshold", [
    (["exists"], 0), (["count"], 0), (["with"], 2), (["exists", "with"], 0),
    (["count", "with"], 2), (["exists", "count", "with"], 0),
    (["with", "exists", "count", "with"], 0),
])
def test_recursive_queries_restore_twice_and_keep_complete_results(contexts, threshold):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P3", 9, 2, 9, composition={
        "contexts": contexts, "threshold": threshold}, database=config)
    db = Neo4j(config, protocol)
    try:
        for _ in range(2):
            db.restore(case)
            check_case(db, case, protocol)
        db.execute("MATCH (n) WHERE n.keep = false SET n.keep = true", {}, 20)
        with pytest.raises(CaseFailure, match="cost_check"):
            check_case(db, case, protocol)
    finally:
        db.close()


def test_composed_case_runs_and_replays_through_the_existing_validation_pipeline(tmp_path):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P3", 12, 1, 3,
                        composition={"contexts": ["count", "with"], "threshold": 2}, database=config)
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, protocol = read_case(tmp_path)
    assert restored == case
    replayed = run_case(restored, config, protocol)
    assert replayed["status"] == "no_candidate", replayed
    assert replayed["discovery"]["samples"]


def test_parameterized_large_graph_matches_the_small_model_result():
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P3", 10_000, 0, 8,
                        composition={"contexts": ["count", "with"], "threshold": 2}, database=config)
    db = Neo4j(config, protocol)
    try:
        db.restore(case)
        check_case(db, case, protocol)
        assert case["expected_rows"] == [{"u.uid": 1, "gsp_value_0": 3}]
    finally:
        db.close()
