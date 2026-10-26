"""Real execution of new six-stage cases on an explicitly supplied disposable DB."""

import os

import pytest

from executor import Neo4j, check_case, measurement_settings, run_case
from main import prepare_case, read_case, save_case


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no disposable Neo4j supplied")]

CASES = [(f"P{i}", {}) for i in range(1, 14)] + [(f"S{i}", {}) for i in range(1, 11)]
CASES += [("P10", {"form": "independent"}), ("P10", {"form": "chain"})]


def configuration():
    return {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
            "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
            "dedicated": True, "allow_reset": True}


@pytest.mark.parametrize("pattern,options", CASES)
@pytest.mark.parametrize("seed", [0, 3])
def test_new_pipeline_complete_bags_and_loaded_state_checks(pattern, options, seed):
    settings = measurement_settings({"timeout_seconds": 30})
    case = prepare_case(None, pattern, 7, seed, 3, database=configuration(),
                        ast_options={"pattern_options": options})
    assert not case.get("compile_error"), case.get("compile_error")
    database = Neo4j(configuration(), settings)
    try:
        database.restore(case)
        check_case(database, case, settings)
    finally:
        database.close()


@pytest.mark.parametrize("pattern", ["P3", "S7"])
def test_actual_execution_archive_replay_and_bound_runtime_evidence(tmp_path, pattern):
    settings = measurement_settings({"timeout_seconds": 30, "discovery_pairs": 2, "discovery_warmups": 0,
                                     "confirmation_pairs": 4, "confirmation_warmups": 0,
                                     "control_pairs": 2, "bootstrap_samples": 100,
                                     "screen_ratio": 1_000_000, "collect_plans": True})
    case = prepare_case(None, pattern, 7, 0, 3, database=configuration())
    report = run_case(case, configuration(), settings)
    assert report["status"] == "no_candidate", report
    assert report["dependency_evidence"]["result_relation"] == "proved"
    assert report["dependency_evidence"]["physical_invocations"] == "unknown"
    save_case(tmp_path, case, settings, report)
    restored, protocol = read_case(tmp_path)
    assert restored == case
    replayed = run_case(restored, configuration(), protocol)
    assert replayed["status"] == "no_candidate", replayed


@pytest.mark.parametrize("pattern", ["P3", "P10", "S7"])
def test_large_certified_states_preserve_results_in_the_real_database(pattern):
    settings = measurement_settings({"timeout_seconds": 30})
    case = prepare_case(None, pattern, 10000, 0, 3, database=configuration(), ast_options={
        "grammar": {"clauses": ["RETURN"], "port_kinds": ["rows"], "expression_operators": ["count"]}})
    assert case["dependency_report"]["static"]["input_observation_scope"] == "certified_proof_graph"
    assert case["graph_spec"]["scale"] == 10000
    database = Neo4j(configuration(), settings)
    try:
        database.restore(case)
        check_case(database, case, settings)
    finally:
        database.close()
