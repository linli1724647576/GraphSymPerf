"""Characterization contracts for the pre-refactor experiment surface.

These tests intentionally describe the supported state before schema-v2 replaces
the legacy manifest projection in Task 2.  They are not permission to keep
legacy configuration as a future runtime entry point.
"""

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.manifest import load_experiment_manifest


def test_current_main_manifest_covers_the_seven_declared_targets() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    assert {system.target for system in manifest.systems} == set(TargetSystem)
    assert manifest.source_modes == ("gdsmith",)
    assert manifest.execution.shards == 16


def test_current_manifest_projection_keeps_case_budget_and_shard_identity_separate() -> None:
    manifest = load_experiment_manifest("config/experiments/main_effectiveness.yaml")

    projected = manifest.to_run_config(shard_index=3, num_shards=manifest.execution.shards)

    assert manifest.execution.cases_per_shard > 0
    assert projected.query_generation.gdsmith.random_seed == manifest.seed + 3
    assert manifest.total_case_budget(manifest.execution.shards) == projected.query_generation.max_cases
