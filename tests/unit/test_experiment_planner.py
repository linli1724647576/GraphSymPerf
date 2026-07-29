from __future__ import annotations

import json

from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.spec import load_experiment_spec


def test_planner_is_deterministic_and_assigns_one_unique_seed_per_shard(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")

    first = build_run_plan(spec, repository_root=tmp_path)
    second = build_run_plan(spec, repository_root=tmp_path)

    assert len(first.tasks) == 112
    assert first.total_accepted_pair_budget == 112_000
    assert first == second
    assert len({task.seed for task in first.tasks}) == 112
    assert all(0 <= task.seed <= (2**63 - 1) for task in first.tasks)
    assert {(task.target, task.shard_index) for task in first.tasks} == {
        (target, shard) for target in spec.targets for shard in range(16)
    }


def test_resolved_manifest_is_a_secret_free_auditable_plan(tmp_path) -> None:
    spec = load_experiment_spec("config/experiment.yaml")
    plan = build_run_plan(spec, repository_root=tmp_path)
    path = tmp_path / "resolved_manifest.json"

    write_resolved_manifest(plan, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["total_accepted_pair_budget"] == 112_000
    assert len(payload["tasks"]) == 112
    assert "password" not in path.read_text(encoding="utf-8").lower()
