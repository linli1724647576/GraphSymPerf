from __future__ import annotations

import pytest

from graph_sym_perf.runtime.measurement import RuntimeHarness, paired_schedule
from graph_sym_perf.runtime.models import RuntimePolicy, RuntimeVerdict


class ExecuteOnlyFake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, query: str, parameters, **_kwargs) -> float:
        self.calls.append(query)
        return 5.0 if query == "A" else 25.0

    def profile(self, *_args, **_kwargs):  # pragma: no cover - a forbidden call must explode.
        raise AssertionError("RuntimeHarness must never call profile")


class QueryRuntimeFake:
    def __init__(self, runtimes: dict[str, float]) -> None:
        self.runtimes = runtimes

    def execute(self, query: str, parameters, **_kwargs) -> float:
        return self.runtimes[query]


def test_paired_schedule_is_seeded_and_balanced_for_discovery_and_validation() -> None:
    discovery = paired_schedule(5, seed=7, require_exact_balance=False)
    validation = paired_schedule(20, seed=7, require_exact_balance=True)

    assert discovery == paired_schedule(5, seed=7, require_exact_balance=False)
    assert abs(discovery.count("AB") - discovery.count("BA")) == 1
    assert validation.count("AB") == validation.count("BA") == 10


def test_discovery_public_path_requires_five_approximately_balanced_pairs() -> None:
    harness = RuntimeHarness()
    evidence = harness.measure_discovery(
        QueryRuntimeFake({"A": 10.0, "B": 50.0}),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=7),
    )

    assert len(evidence.paired_samples) == 5
    assert abs(sum(sample.order == "AB" for sample in evidence.paired_samples) - sum(sample.order == "BA" for sample in evidence.paired_samples)) == 1
    with pytest.raises(ValueError, match="discovery requires exactly 5 pairs"):
        harness.measure_discovery(
            QueryRuntimeFake({"A": 10.0, "B": 50.0}),
            "A",
            "B",
            policy=RuntimePolicy(warmups=0, pairs=20, seed=7, require_exact_balance=True),
        )


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


def test_discovery_fixes_slow_fast_direction_instead_of_assuming_b_is_slow() -> None:
    evidence = RuntimeHarness().measure_pair(
        QueryRuntimeFake({"A": 50.0, "B": 10.0}),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=4),
    )

    assert evidence.median_ratio == 5.0
    assert evidence.median_delta_ms == 40.0
    assert evidence.direction_consistency == 1.0
    assert evidence.slow_query == "a"


def test_validation_keeps_discovery_direction_when_runtime_order_reverses() -> None:
    evidence = RuntimeHarness().measure_pair(
        QueryRuntimeFake({"A": 10.0, "B": 50.0}),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=20, seed=4, require_exact_balance=True),
        expected_slow_query="a",
    )

    assert evidence.slow_query == "a"
    assert evidence.median_ratio == 0.2
    assert evidence.median_delta_ms == -40.0
    assert evidence.direction_consistency == 0.0
    assert "direction_mismatch" in evidence.reason_codes


@pytest.mark.parametrize("invalid_elapsed", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_execute_elapsed_is_invalid(invalid_elapsed: float) -> None:
    evidence = RuntimeHarness().measure_pair(
        QueryRuntimeFake({"A": 10.0, "B": invalid_elapsed}),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=4),
    )

    assert evidence.verdict is RuntimeVerdict.INVALID
    assert evidence.reason_codes == ("non_finite_elapsed",)


def test_execution_failure_is_invalid_not_runtime_evidence() -> None:
    class CrashFake:
        def execute(self, query: str, parameters, **_kwargs) -> float:
            raise RuntimeError("connection dropped")

    evidence = RuntimeHarness().measure_pair(
        CrashFake(),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=4),
    )

    assert evidence.verdict is RuntimeVerdict.INVALID
    assert evidence.reason_codes == ("execution_error:RuntimeError",)


@pytest.mark.parametrize("error_type", [TypeError, AssertionError])
def test_programming_errors_from_execute_are_not_swallowed(error_type: type[Exception]) -> None:
    class ProgrammingErrorFake:
        def execute(self, query: str, parameters, **_kwargs) -> float:
            raise error_type("bad adapter call")

    with pytest.raises(error_type, match="bad adapter call"):
        RuntimeHarness().measure_pair(
            ProgrammingErrorFake(),
            "A",
            "B",
            policy=RuntimePolicy(warmups=0, pairs=5, seed=4),
        )
