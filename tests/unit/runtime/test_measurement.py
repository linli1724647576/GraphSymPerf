from __future__ import annotations

from graph_sym_perf.runtime.measurement import RuntimeHarness, paired_schedule
from graph_sym_perf.runtime.models import RuntimePolicy


class ExecuteOnlyFake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, query: str, parameters, **_kwargs) -> float:
        self.calls.append(query)
        return 5.0 if query == "A" else 25.0

    def profile(self, *_args, **_kwargs):  # pragma: no cover - a forbidden call must explode.
        raise AssertionError("RuntimeHarness must never call profile")


def test_paired_schedule_is_seeded_and_balanced_for_discovery_and_validation() -> None:
    discovery = paired_schedule(5, seed=7, require_exact_balance=False)
    validation = paired_schedule(20, seed=7, require_exact_balance=True)

    assert discovery == paired_schedule(5, seed=7, require_exact_balance=False)
    assert abs(discovery.count("AB") - discovery.count("BA")) == 1
    assert validation.count("AB") == validation.count("BA") == 10


def test_warmups_are_not_measurement_samples_and_execution_never_profiles() -> None:
    executor = ExecuteOnlyFake()
    evidence = RuntimeHarness().measure_pair(
        executor,
        "A",
        "B",
        policy=RuntimePolicy(warmups=2, pairs=5, seed=17),
    )

    assert len(evidence.paired_samples) == 5
    assert len(executor.calls) == 14  # 2 warmups each side + 5 measured pairs.
    assert evidence.median_ratio == 5.0
