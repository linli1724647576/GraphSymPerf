"""Characterization of the formal experiment surface at the refactor baseline."""

from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.state import ShardState


ROOT = Path(__file__).resolve().parents[2]
FORMAL_CONFIG = ROOT / "config" / "experiment.yaml"


def test_formal_experiment_expands_to_seven_by_sixteen_by_one_thousand() -> None:
    spec = load_experiment_spec(FORMAL_CONFIG)
    plan = build_run_plan(spec, repository_root=ROOT)
    repeated = build_run_plan(spec, repository_root=ROOT)

    assert spec.generation.backend == "gdsmith"
    assert spec.targets == (
        TargetSystem.NEO4J,
        TargetSystem.MEMGRAPH,
        TargetSystem.FALKORDB,
        TargetSystem.AGENSGRAPH,
        TargetSystem.ARCADEDB,
        TargetSystem.APACHE_AGE,
        TargetSystem.LADYBUGDB,
    )
    assert len(plan.tasks) == 112
    assert plan.total_accepted_pair_budget == 112_000
    assert {task.accepted_query_pairs for task in plan.tasks} == {1_000}
    assert len({task.seed for task in plan.tasks}) == 112
    assert plan.tasks == repeated.tasks
    for target in spec.targets:
        assert {task.shard_index for task in plan.tasks if task.target is target} == set(range(16))


def test_resolved_manifest_preserves_task_identity_and_resource_limits(tmp_path: Path) -> None:
    spec = load_experiment_spec(FORMAL_CONFIG)
    plan = build_run_plan(spec, repository_root=ROOT)

    destination = write_resolved_manifest(plan, tmp_path / "resolved_manifest.json")
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert payload["total_accepted_pair_budget"] == 112_000
    assert len(payload["tasks"]) == 112
    expected_task_fields = {
        "accepted_query_pairs",
        "generation_backend",
        "max_generated_seeds",
        "max_runtime_seconds",
        "seed",
        "shard_index",
        "target",
    }
    assert all(set(task) == expected_task_fields for task in payload["tasks"])
    assert {task["max_generated_seeds"] for task in payload["tasks"]} == {10_000}
    assert {task["max_runtime_seconds"] for task in payload["tasks"]} == {6 * 60 * 60}


def test_shard_status_contract_distinguishes_success_shortfall_and_failure() -> None:
    assert ShardState.COMPLETED is not ShardState.INCOMPLETE
    assert ShardState.INCOMPLETE is not ShardState.FAILED_FINAL
    assert {ShardState.COMPLETED, ShardState.INCOMPLETE, ShardState.FAILED_FINAL}.isdisjoint(
        {ShardState.PLANNED, ShardState.GENERATING, ShardState.SCREENING}
    )
