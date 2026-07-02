from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from graph_sym_perf.core import ConfigError, QueryCase
from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.experiment.resume import CheckpointState
from graph_sym_perf.experiment.runner import _apply_run_overrides, _build_case_tasks, _effective_worker_count
from graph_sym_perf.experiment.shard import ShardSpec


def test_main_effectiveness_manifest_projects_to_run_config() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    run_config = manifest.to_run_config(shard_index=2, num_shards=16)

    assert manifest.experiment_id == "main_effectiveness_2026_01"
    assert manifest.source_kind == "multi"
    assert manifest.source_modes == ("gdsmith", "ldbc")
    assert manifest.total_case_budget(16) == 55000
    assert run_config.query_generation.max_cases == 55000
    assert run_config.query_generation.gdsmith.random_seed == manifest.seed + 2
    assert run_config.paths.artifact_dir.endswith("runs/main_effectiveness_2026_01/{run_id}")


def test_replay_known_manifest_uses_cases_jsonl_source() -> None:
    manifest = load_experiment_manifest("config/experiments/replay_known.yaml")

    assert manifest.source_kind == "cases_jsonl"
    assert manifest.execution.shards == 1
    assert Path(str(manifest.source["cases_path"])).name == "replay_known_cases.jsonl"


def test_experiment_run_overrides_project_to_run_config() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=16)

    updated = _apply_run_overrides(
        run_config,
        systems="neo4j,memgraph",
        max_cases=25,
        feedback_enabled=False,
    )

    assert [system.target.value for system in updated.systems] == ["neo4j", "memgraph"]
    assert updated.query_generation.max_cases == 25
    assert updated.query_generation.gdsmith.num_queries == 25
    assert updated.feedback.enabled is False


def test_experiment_run_overrides_reject_unknown_system() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    run_config = manifest.to_run_config(shard_index=0, num_shards=16)

    with pytest.raises(ConfigError):
        _apply_run_overrides(run_config, systems="unknown", max_cases=None, feedback_enabled=None)


def test_worker_count_uses_manifest_workers_for_non_destructive_cases() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")
    manifest = replace(manifest, execution=replace(manifest.execution, workers_per_shard=4))
    case = QueryCase(
        case_id="seed",
        template_name="unit",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
    )

    tasks = _build_case_tasks(
        manifest,
        ShardSpec(index=0, total=1),
        ((0, case),),
        manifest.systems[:2],
        CheckpointState(),
    )

    assert len(tasks) == 2
    assert _effective_worker_count(manifest, tasks) == 2


def test_worker_count_forces_serial_when_setup_graph_is_destructive() -> None:
    manifest = load_experiment_manifest("config/experiments/replay_known.yaml")
    manifest = replace(manifest, execution=replace(manifest.execution, workers_per_shard=4))
    case = QueryCase(
        case_id="known",
        template_name="unit",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
        target_system="neo4j",
        setup_path="config/experiments/replay_known_setups/neo4j_13783_reproducer_graph.cypher.txt",
    )

    tasks = _build_case_tasks(
        manifest,
        ShardSpec(index=0, total=1),
        ((0, case),),
        manifest.systems,
        CheckpointState(),
    )

    assert len(tasks) == 1
    assert _effective_worker_count(manifest, tasks) == 1
