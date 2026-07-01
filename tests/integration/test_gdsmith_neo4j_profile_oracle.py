from __future__ import annotations

import os

import pytest

from graph_sym_perf.app.gdsmith_mining import run_gdsmith_mining


pytestmark = pytest.mark.skipif(
    os.environ.get("GRAPHSYMPERF_RUN_INTEGRATION") != "1",
    reason="integration tests require GRAPHSYMPERF_RUN_INTEGRATION=1",
)


def test_gdsmith_neo4j_writes_symmetry_artifacts() -> None:
    required = (
        "GRAPHSYMPERF_NEO4J_URI",
        "GRAPHSYMPERF_NEO4J_USER",
        "GRAPHSYMPERF_NEO4J_PASSWORD",
        "GRAPHSYMPERF_GDSMITH_JAR",
        "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip(f"missing integration environment variables: {', '.join(missing)}")

    exit_code = run_gdsmith_mining(
        config_path="config/graph_sym_perf.yaml",
        systems="neo4j",
        max_cases=20,
        run_id="integration-gdsmith-neo4j-symmetry-oracle",
        seed=20260522,
    )

    assert exit_code == 0
