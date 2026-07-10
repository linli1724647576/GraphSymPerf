import pytest

from graph_sym_perf.sampling.adaptive import AdaptiveRuleSampler
from graph_sym_perf.sampling.yield_store import YieldSnapshot


def test_sampler_uses_exploration_floor_then_validated_yield_only() -> None:
    sampler = AdaptiveRuleSampler(("r1", "r2"), exploration_floor=2, epoch_size=100)
    assert sampler.select("neo4j", 0, 0, YieldSnapshot({"r1": (0, 0), "r2": (0, 0)})).rule_id == "r1"
    with pytest.raises(ValueError):
        sampler.select("neo4j", 0, 100, {"r1": {"accepted": 1, "profile": 99}})  # type: ignore[arg-type]
