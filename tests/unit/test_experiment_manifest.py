from __future__ import annotations

from pathlib import Path

from graph_sym_perf.experiment.manifest import load_experiment_manifest


def test_main_effectiveness_manifest_projects_to_run_config() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    run_config = manifest.to_run_config(shard_index=2, num_shards=16)

    assert manifest.experiment_id == "main_effectiveness_2026_01"
    assert manifest.source_kind == "gdsmith"
    assert manifest.total_case_budget(16) == 16000
    assert run_config.query_generation.max_cases == 16000
    assert run_config.query_generation.gdsmith.random_seed == manifest.seed + 2
    assert run_config.paths.artifact_dir.endswith("runs/main_effectiveness_2026_01/{run_id}")


def test_replay_known_manifest_uses_cases_jsonl_source() -> None:
    manifest = load_experiment_manifest("config/experiments/replay_known.yaml")

    assert manifest.source_kind == "cases_jsonl"
    assert manifest.execution.shards == 1
    assert Path(str(manifest.source["cases_path"])).name == "replay_known_cases.jsonl"
