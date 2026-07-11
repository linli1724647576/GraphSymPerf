from __future__ import annotations

import pytest

from graph_sym_perf.core import TargetSystem
from graph_sym_perf.experiment.commands import _gate_spec, _previous_gate_complete
from graph_sym_perf.experiment.planner import build_run_plan
from graph_sym_perf.experiment.spec import load_experiment_spec


@pytest.mark.parametrize("accepted_pairs", (10, 100, 1000))
def test_gate_spec_is_one_target_one_shard_with_a_bounded_seed_budget(accepted_pairs: int) -> None:
    formal = load_experiment_spec("config/experiment.yaml")

    gate = _gate_spec(formal, "neo4j", accepted_pairs)

    assert gate.targets == (TargetSystem.NEO4J,)
    assert tuple(gate.systems) == (TargetSystem.NEO4J,)
    assert gate.matrix.shards_per_system == 1
    assert gate.workload.accepted_query_pairs_per_shard == accepted_pairs
    assert gate.workload.max_generated_seeds_per_shard == min(10_000, accepted_pairs * 10)
    plan = build_run_plan(gate)
    assert len(plan.tasks) == 1
    assert plan.total_accepted_pair_budget == accepted_pairs


@pytest.mark.parametrize("accepted_pairs", (0, 11, 999, 10_000))
def test_gate_spec_rejects_unapproved_sizes(accepted_pairs: int) -> None:
    with pytest.raises(ValueError, match="10, 100, or 1000"):
        _gate_spec(load_experiment_spec("config/experiment.yaml"), "neo4j", accepted_pairs)


def test_larger_gate_has_no_implicit_predecessor(tmp_path) -> None:
    assert not _previous_gate_complete(
        tmp_path,
        target=TargetSystem.NEO4J,
        accepted_pairs=10,
    )
