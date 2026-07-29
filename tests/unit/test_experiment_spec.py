from __future__ import annotations

from pathlib import Path

import pytest

from graph_sym_perf.core import ConfigError, TargetSystem
from graph_sym_perf.experiment.spec import load_experiment_spec


def test_runtime_first_experiment_spec_is_the_exact_seven_by_sixteen_matrix() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    assert spec.schema_version == 2
    assert spec.name == "main_runtime_first"
    assert spec.targets == (
        TargetSystem.NEO4J,
        TargetSystem.MEMGRAPH,
        TargetSystem.FALKORDB,
        TargetSystem.AGENSGRAPH,
        TargetSystem.ARCADEDB,
        TargetSystem.APACHE_AGE,
        TargetSystem.LADYBUGDB,
    )
    assert spec.matrix.shards_per_system == 16
    assert spec.workload.accepted_query_pairs_per_shard == 1000
    assert spec.workload.max_generated_seeds_per_shard == 10000
    assert spec.generation.backend == "gdsmith"
    assert spec.total_accepted_pair_budget == 112_000


def test_schema_v2_accepts_an_explicit_gdsmith_algorithm(tmp_path: Path) -> None:
    source = Path("config/experiment.yaml").read_text(encoding="utf-8")
    source = source.replace("  algorithm: COMPARED3\n", "  algorithm: NON_EMPTY\n", 1)
    path = tmp_path / "experiment.yaml"
    path.write_text(source, encoding="utf-8")
    (tmp_path / "systems.yaml").write_text(Path("config/systems.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    spec = load_experiment_spec(path)

    assert spec.generation.algorithm == "NON_EMPTY"


def test_runtime_first_spec_declares_the_constrained_symmetry_state() -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    assert spec.generation.state_model == "involutive_double"
    assert spec.generation.max_node_num == 1024
    assert spec.generation.label_num == 4
    assert spec.generation.bootstrap_queries == 1


@pytest.mark.parametrize(
    "patch",
    (
        "matrix:\n  shards_per_system: 0\n",
        "workload:\n  accepted_query_pairs_per_shard: 0\n",
        "targets:\n  - neo4j\n  - neo4j\n",
        "generation:\n  backend: ldbc\n",
        "generation:\n  backend: gdsmith\n  source: ldbc_templates\n",
        "unknown_field: true\n",
    ),
)
def test_schema_v2_rejects_invalid_or_unknown_formal_configuration(tmp_path: Path, patch: str) -> None:
    source = Path("config/experiment.yaml").read_text(encoding="utf-8")
    path = tmp_path / "invalid.yaml"
    path.write_text(source + "\n" + patch, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_experiment_spec(path)
