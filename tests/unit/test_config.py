from __future__ import annotations

from graph_sym_perf.config import load_run_config


def test_default_config_uses_gdsmith_and_graphsymperf_env() -> None:
    config = load_run_config("config/graph_sym_perf.yaml")

    assert config.query_generation.source == "gdsmith"
    assert config.query_generation.max_cases == 500
    assert config.paths.artifact_dir == "logs/{run_id}"
    assert config.query_generation.gdsmith.jar_env == "GRAPHSYMPERF_GDSMITH_JAR"
    assert config.confirmation.min_latency_ratio == 10.0
    assert config.system_config(config.systems[0].target).required_env[0].startswith("GRAPHSYMPERF_")
