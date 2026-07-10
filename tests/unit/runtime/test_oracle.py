from __future__ import annotations

from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import RuntimePolicy, RuntimeVerdict
from graph_sym_perf.runtime.noise import calibrate_aa_noise
from graph_sym_perf.runtime.oracle import evaluate_runtime


class TimeoutFake:
    def execute(self, query: str, parameters, **_kwargs) -> float:
        if query == "timeout":
            raise TimeoutError("deadline")
        return 10.0 if query == "A" else 50.0


def test_one_sided_timeouts_produce_lower_bound_evidence_but_double_timeout_is_inconclusive() -> None:
    harness = RuntimeHarness()
    one_sided = harness.measure_pair(TimeoutFake(), "A", "timeout", policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0))
    both = harness.measure_pair(TimeoutFake(), "timeout", "timeout", policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0))

    assert one_sided.timeout_lower_bound_ratio == 10.0
    assert "one_sided_timeout" in one_sided.reason_codes
    assert both.verdict is RuntimeVerdict.INCONCLUSIVE


def test_noise_preflight_requires_two_hundred_deltas_and_runtime_uses_its_p99() -> None:
    executor = TimeoutFake()
    noise = calibrate_aa_noise(RuntimeHarness(), executor, tuple("A" for _ in range(20)), pairs_per_query=10)
    evidence = RuntimeHarness().measure_pair(executor, "A", "B", policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True))
    verdict = evaluate_runtime(evidence, noise_floor_ms=noise.p99_delta_ms, min_ratio=3.0, min_ci_low=2.0)

    assert noise.valid
    assert noise.sample_count == 200
    assert verdict.verdict is RuntimeVerdict.PASS
