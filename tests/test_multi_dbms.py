"""Opt-in end-to-end checks against explicitly configured, dedicated databases."""

import os
from pathlib import Path

import pytest

from executor import run_case, supported_patterns
from main import prepare_legacy_case as prepare_case, read_case, read_config, save_case


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_CONFIG_DIR"), reason="no dedicated multi-DBMS test configurations supplied")]


@pytest.fixture(scope="module", params=["neo4j", "memgraph", "falkordb", "arcadedb", "age", "ladybug"])
def target(request):
    config = read_config(Path(os.environ["GRAPHSYMPERF_TEST_CONFIG_DIR"]) / f"{request.param}.yaml")
    return config, {}


@pytest.mark.parametrize("pattern", ["P1", "P4", "P15", "P16", "S4", "S6"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_real_variants_references_and_replay_files(target, pattern, seed, tmp_path):
    config, state_cache = target
    if pattern not in supported_patterns(config["database"]):
        pytest.skip("pattern outside this backend's supported fragment")
    case = prepare_case(None, pattern, 3, seed, 0, database=config["database"])
    report = run_case(case, config["database"], config["measurement"], state_cache=state_cache)
    if report["status"] == "unsupported_feature":
        pytest.skip(f"target version rejects feature: {report['reason']}")
    assert report["status"] in {"no_candidate", "not_confirmed", "candidate"}, report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, config["measurement"], report)
    restored, settings = read_case(tmp_path)
    assert restored == case and settings == config["measurement"]
    assert report["dbms"] == config["database"]["dbms"]
    if case.get("query_ref"):
        assert report["reference_comparisons"], report


@pytest.mark.parametrize("pattern,variant,rows", [
    ("P10", "empty_low", 0), ("P11", "empty_low", 0),
    ("P11", "multi_low", 8), ("P13", "multi_low", 10),
    ("P3", "pruning_path", 1),
])
def test_real_cost_state_variants_preserve_full_results(target, pattern, variant, rows):
    config, state_cache = target
    case = prepare_case(None, pattern, 3, 214501591, 0, 2, variant, database=config["database"])
    report = run_case(case, config["database"], config["measurement"], state_cache=state_cache)
    if report["status"] in {"unsupported_feature", "unsupported_transform"}:
        pytest.skip(f"target version rejects feature: {report['reason']}")
    assert report["status"] in {"no_candidate", "not_confirmed", "candidate"}, report
    assert report["result_rows"] == rows
    assert report["comparisons"]["ab"]["pair_kind"] == "symmetry"
