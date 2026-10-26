"""New paper patterns run only against an explicitly supplied disposable DB."""

import os

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_legacy_case as prepare_case, read_case, save_case
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


@pytest.mark.parametrize("pattern,contexts,threshold", [
    ("P4", [], 0), ("P4", ["count"], 0), ("P4", ["with"], 0),
    ("P4", ["exists", "count", "with"], 0),
    ("P10", [], 0), ("P10", ["count"], 2), ("P10", ["with"], 2),
    ("P10", ["exists", "count", "with"], 0),
])
def test_paper_patterns_restore_and_return_the_complete_typed_bag(pattern, contexts, threshold):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 11, 0, 3,
                        composition={"contexts": contexts, "threshold": threshold}, database=config)
    assert case["paper_pattern"] == pattern
    assert case["pattern_catalog"] == "paper-v1"
    db = Neo4j(config, protocol)
    try:
        for _ in range(2):
            db.restore(case)
            check_case(db, case, protocol)
            for side in ("a", "b"):
                result = db.execute(case[f"query_{side}"], case["parameters"], 30)
                assert result["complete"] is True
                assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
        db.execute("MATCH ()-[r]->() DELETE r", {}, 30)
        with pytest.raises(CaseFailure, match="cost_check"):
            check_case(db, case, protocol)
    finally:
        db.close()


@pytest.mark.parametrize("pattern,threshold", [("P4", 0), ("P10", 2)])
def test_paper_patterns_run_archive_and_replay_with_the_same_evidence(tmp_path, pattern, threshold):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 13, 0, 3,
                        composition={"contexts": ["count", "with"], "threshold": threshold}, database=config)
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, protocol = read_case(tmp_path)
    assert restored == case
    assert restored["mutation_record"] == case["mutation_record"]
    assert restored["population_evidence"] == case["population_evidence"]
    replayed = run_case(restored, config, protocol)
    assert replayed["status"] == "no_candidate", replayed
    assert replayed["discovery"]["samples"]


@pytest.mark.parametrize("pattern,threshold,work_budget", [("P4", 0, 10_000), ("P10", 2, 30_000)])
def test_large_loading_restores_declared_indexes_after_temporary_uid_indexes(pattern, threshold, work_budget):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, work_budget, 0, 3,
                        composition={"contexts": ["count", "with"], "threshold": threshold}, database=config)
    spec = GraphSpec.from_dict(case["graph_spec"])
    assert spec.counts()["nodes"] >= 10_000
    work = case["construction_record"]["requirements"]["derivation"]["work"]
    assert work["target"] == work_budget
    assert work["potential_extra_candidate_work"] >= work_budget
    db = Neo4j(config, protocol)
    try:
        db.restore(case)
        check_case(db, case, protocol)
        actual = db.execute("SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties "
                            "WHERE type <> 'LOOKUP' RETURN name, state, labelsOrTypes, properties", {}, 30)
        expected = [{"name": index["name"], "state": "ONLINE", "labelsOrTypes": [index["label"]],
                     "properties": [index["property"]]} for index in spec.indexes]
        assert actual["complete"] is True
        assert sorted(actual["rows"], key=lambda row: row["name"]) == sorted(expected, key=lambda row: row["name"])
    finally:
        db.close()
