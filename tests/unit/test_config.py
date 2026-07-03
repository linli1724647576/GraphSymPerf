from __future__ import annotations

import tomllib
from pathlib import Path

from graph_sym_perf.config import load_run_config
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.feedback.state import BOOL_REDUNDANCY_RULES, SYMMETRY_RULES


RULES_BY_FAMILY = {
    "bool_redundancy": BOOL_REDUNDANCY_RULES,
    "symmetry": SYMMETRY_RULES,
}

CURRENT_TARGETS = ("neo4j", "memgraph", "falkordb", "agensgraph", "arcadedb", "apache_age", "ladybugdb")


def test_default_config_uses_gdsmith_and_graphsymperf_env() -> None:
    config = load_run_config("config/graph_sym_perf.yaml")

    assert tuple(system.target.value for system in config.systems) == CURRENT_TARGETS
    assert config.query_generation.source == "gdsmith"
    assert config.query_generation.max_cases == 500
    assert config.paths.artifact_dir == "logs/{run_id}"
    assert config.query_generation.gdsmith.jar_env == "GRAPHSYMPERF_GDSMITH_JAR"
    assert config.query_generation.gdsmith.no_proxy_launcher_env == "GRAPHSYMPERF_GDSMITH_NO_PROXY_LAUNCHER"
    assert config.confirmation.min_latency_ratio == 10.0
    assert config.coverage.enabled
    assert config.coverage.ratio_buckets[0] == 1.5
    assert not config.feedback.enabled
    assert config.feedback.max_rules_per_case == 4
    assert config.clustering.enabled
    assert config.clustering.write_signatures
    assert config.system_config(TargetSystem.AGENSGRAPH) is not None
    assert config.system_config(TargetSystem.LADYBUGDB).required_env == ("GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH",)
    assert config.system_config(config.systems[0].target).required_env[0].startswith("GRAPHSYMPERF_")
    assert config.system_config(config.systems[0].target).capabilities.supports_execute


def test_empty_config_defaults_to_current_six_system_stack(tmp_path: Path) -> None:
    path = tmp_path / "minimal.yaml"
    path.write_text("project: {}\n", encoding="utf-8")

    config = load_run_config(path)

    assert tuple(system.target.value for system in config.systems) == CURRENT_TARGETS


def test_run_configs_cover_enabled_rule_spaces() -> None:
    paths = (
        Path("config/graph_sym_perf.yaml"),
        Path("config/graph_sym_perf_2000.yaml"),
        Path("config/graph_sym_perf_10000.yaml"),
        Path("config/graph_sym_perf_500_longtimeout.yaml"),
    )
    for path in paths:
        config = load_run_config(path)
        assert tuple(system.target.value for system in config.systems) == CURRENT_TARGETS
        enabled = set(config.transforms.enabled_rules)
        missing = [
            rule
            for family in config.transforms.enabled_families
            for rule in RULES_BY_FAMILY.get(family, ())
            if rule not in enabled
        ]

        assert not missing, f"{path} misses enabled transform rules: {missing}"


def test_memgraph_extra_installs_adapter_driver() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    memgraph_deps = pyproject["project"]["optional-dependencies"]["memgraph"]

    assert any(dep.startswith("neo4j") for dep in memgraph_deps)


def test_ladybugdb_extra_installs_adapter_driver() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_deps = pyproject["project"]["optional-dependencies"]

    assert "ladybugdb" in optional_deps
    assert any(dep == "ladybug" or dep.startswith("ladybug>=") for dep in optional_deps["ladybugdb"])
    assert any(dep == "ladybug" or dep.startswith("ladybug>=") for dep in optional_deps["db"])
