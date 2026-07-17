from __future__ import annotations

from dataclasses import replace

import pytest

import graph_sym_perf.runtime.oracle as oracle_module

from graph_sym_perf.core import QueryTimeoutError
from graph_sym_perf.runtime.measurement import RuntimeHarness, paired_schedule
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.runtime.noise import calibrate_aa_noise
from graph_sym_perf.runtime.oracle import evaluate_runtime, validate_stage2_evidence
from graph_sym_perf.runtime.screening import DEFAULT_SCREENING_POLICY


class TimeoutFake:
    def execute(self, query: str, parameters, **_kwargs) -> float:
        if query == "timeout":
            raise QueryTimeoutError("deadline")
        return 10.0 if query == "A" else 50.0


class BelowRatioFake:
    def execute(self, query: str, parameters, **_kwargs) -> float:
        return 10.0 if query == "A" else 29.0


def test_one_sided_timeouts_produce_lower_bound_evidence_but_double_timeout_is_inconclusive() -> None:
    harness = RuntimeHarness()
    one_sided = harness.measure_pair(
        TimeoutFake(),
        "A",
        "timeout",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0),
        expected_slow_query="b",
    )
    both = harness.measure_pair(TimeoutFake(), "timeout", "timeout", policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0))

    assert one_sided.timeout_lower_bound_ratio == 10.0
    assert one_sided.slow_query == "b"
    assert "one_sided_timeout" in one_sided.reason_codes
    assert both.verdict is RuntimeVerdict.INCONCLUSIVE


def test_single_timeout_does_not_claim_a_repeated_timeout_lower_bound() -> None:
    class OneTimeoutFake:
        def __init__(self) -> None:
            self.b_calls = 0

        def execute(self, query: str, parameters, **_kwargs) -> float:
            if query == "B":
                self.b_calls += 1
                if self.b_calls == 1:
                    raise QueryTimeoutError("one outlier")
                return 50.0
            return 10.0

    evidence = RuntimeHarness().measure_pair(
        OneTimeoutFake(),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=5, seed=1, timeout_ms=100.0),
        expected_slow_query="b",
    )

    assert evidence.verdict is RuntimeVerdict.INCONCLUSIVE
    assert evidence.timeout_lower_bound_ratio is None
    assert evidence.reason_codes == ("timeout_inconclusive",)


def test_noise_preflight_requires_two_hundred_deltas_and_runtime_uses_its_p99() -> None:
    executor = TimeoutFake()
    noise = calibrate_aa_noise(RuntimeHarness(), executor, tuple("A" for _ in range(20)), pairs_per_query=10)
    evidence = RuntimeHarness().measure_pair(executor, "A", "B", policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True))
    verdict = evaluate_runtime(evidence, noise_floor_ms=noise.p99_delta_ms, min_ratio=3.0, min_ci_low=2.0)

    assert noise.valid
    assert noise.sample_count == 200
    assert noise.p95_delta_ms == 0.0
    assert noise.p99_delta_ms == 0.0
    assert noise.median_order_bias_ms == 0.0
    assert noise.long_tail_ratio == 0.0
    assert verdict.verdict is RuntimeVerdict.PASS


def test_noise_preflight_is_invalid_when_fewer_than_two_hundred_deltas_are_valid() -> None:
    noise = calibrate_aa_noise(
        RuntimeHarness(),
        TimeoutFake(),
        tuple("A" for _ in range(19)),
        pairs_per_query=10,
    )

    assert not noise.valid
    assert noise.sample_count == 190
    assert noise.reason_codes == ("insufficient_aa_samples",)


def test_noise_preflight_requires_exactly_twenty_queries_by_ten_pairs() -> None:
    noise = calibrate_aa_noise(
        RuntimeHarness(),
        TimeoutFake(),
        tuple("A" for _ in range(21)),
        pairs_per_query=10,
    )

    assert not noise.valid
    assert noise.sample_count == 210
    assert noise.reason_codes == ("invalid_aa_protocol",)


def test_noise_preflight_rejects_consistent_first_second_execution_bias() -> None:
    class SecondExecutionSlowerFake:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, query: str, parameters, **_kwargs) -> float:
            self.calls += 1
            return 10.0 if self.calls % 2 else 20.0

    noise = calibrate_aa_noise(
        RuntimeHarness(),
        SecondExecutionSlowerFake(),
        tuple(f"probe-{index}" for index in range(20)),
        pairs_per_query=10,
    )

    assert not noise.valid
    assert noise.sample_count == 200
    assert noise.p95_delta_ms == 10.0
    assert noise.p99_delta_ms == 10.0
    assert noise.median_order_bias_ms == 20.0
    assert noise.reason_codes == ("aa_order_bias",)


def test_noise_preflight_rejects_a_long_tail_above_five_times_p95() -> None:
    class LongTailFake:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def execute(self, query: str, parameters, **_kwargs) -> float:
            self.calls[query] = self.calls.get(query, 0) + 1
            # Exactly four of 200 pairs have a large second observation. That
            # keeps p95 near 1ms while moving the interpolated p99 above 5x.
            probe_index = int(query.removeprefix("probe-"))
            call = self.calls[query]
            measured_call = call - 4
            is_second = measured_call > 0 and measured_call % 2 == 0
            return 100.0 if is_second and probe_index in {0, 5, 10, 15} and measured_call == 2 else (11.0 if is_second else 10.0)

    noise = calibrate_aa_noise(
        RuntimeHarness(),
        LongTailFake(),
        tuple(f"probe-{index}" for index in range(20)),
        pairs_per_query=10,
    )

    assert not noise.valid
    assert noise.long_tail_ratio is not None
    assert noise.long_tail_ratio > 5.0
    assert "aa_long_tail" in noise.reason_codes


def test_noise_preflight_warms_each_probe_before_collecting_samples() -> None:
    class ColdFirstExecutionFake:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def execute(self, query: str, parameters, **_kwargs) -> float:
            self.calls[query] = self.calls.get(query, 0) + 1
            return 1000.0 if self.calls[query] == 1 else 10.0

    noise = calibrate_aa_noise(
        RuntimeHarness(),
        ColdFirstExecutionFake(),
        tuple(f"probe-{index}" for index in range(20)),
        pairs_per_query=10,
    )

    assert noise.valid
    assert noise.sample_count == 200
    assert noise.p99_delta_ms == 0.0


def test_runtime_gate_rejects_evidence_without_a_stable_majority_direction() -> None:
    evidence = RuntimeHarness().measure_pair(
        TimeoutFake(),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
        expected_slow_query="b",
    )

    verdict = evaluate_runtime(replace(evidence, direction_consistency=0.5), noise_floor_ms=0.0)

    assert verdict.verdict is RuntimeVerdict.FAIL
    assert "direction_unstable" in verdict.reason_codes


def test_validation_public_path_forces_twenty_balanced_pairs_and_strict_gate() -> None:
    harness = RuntimeHarness()
    pair = EquivalentQueryPair("A", "B", {"tenant": 7}, "graph-hash", "dbms-hash")
    passing = harness.measure_validation(
        TimeoutFake(),
        pair,
        policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )
    failing = harness.measure_validation(
        BelowRatioFake(),
        pair,
        policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
        expected_slow_query="b",
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
    )

    assert passing.verdict is RuntimeVerdict.PASS
    assert len(passing.paired_samples) == 20
    assert sum(sample.order == "AB" for sample in passing.paired_samples) == 10
    assert sum(sample.order == "BA" for sample in passing.paired_samples) == 10
    assert passing.graph_snapshot_hash == "graph-hash"
    assert passing.dbms_fingerprint == "dbms-hash"
    assert failing.verdict is RuntimeVerdict.FAIL
    assert "ratio_below_threshold" in failing.reason_codes
    with pytest.raises(TypeError, match="unexpected keyword argument 'parameters'"):
        harness.measure_validation(
            TimeoutFake(),
            pair,
            policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
            expected_slow_query="b",
            noise_floor_ms=0.0,
            timer_resolution_ms=0.001,
            parameters={"tenant": 999},
        )
    with pytest.raises(ValueError, match="validation requires exactly 20 pairs"):
        harness.measure_validation(
            TimeoutFake(),
            pair,
            policy=RuntimePolicy(warmups=0, pairs=5, seed=2),
            expected_slow_query="b",
            noise_floor_ms=0.0,
            timer_resolution_ms=0.001,
        )


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"median_ratio": 2.99}, "ratio_below_threshold"),
        ({"ci_ratio_low": 1.99}, "ci_lower_below_threshold"),
        ({"median_delta_ms": 20.0}, "delta_within_noise"),
    ],
)
def test_runtime_gate_requires_every_issue_grade_threshold(changes: dict[str, float], expected_reason: str) -> None:
    evidence = RuntimeHarness().measure_pair(
        TimeoutFake(),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
        expected_slow_query="b",
    )

    verdict = evaluate_runtime(
        replace(evidence, **changes),
        noise_floor_ms=10.0,
        timer_resolution_ms=2.0,
    )

    assert verdict.verdict is RuntimeVerdict.FAIL
    assert expected_reason in verdict.reason_codes


def test_direction_mismatch_cannot_be_reoriented_into_a_pass() -> None:
    mismatched = RuntimeHarness().measure_pair(
        TimeoutFake(),
        "A",
        "B",
        policy=RuntimePolicy(warmups=0, pairs=20, seed=2, require_exact_balance=True),
        expected_slow_query="a",
    )
    apparently_large = replace(
        mismatched,
        median_ratio=5.0,
        median_delta_ms=40.0,
        ci_ratio_low=4.0,
        ci_ratio_high=6.0,
        direction_consistency=1.0,
    )

    verdict = evaluate_runtime(apparently_large, noise_floor_ms=0.0)

    assert verdict.verdict is RuntimeVerdict.FAIL
    assert "direction_mismatch" in verdict.reason_codes


def test_stage2_canonical_recompute_uses_the_same_nondefault_bootstrap_policy(
    monkeypatch,
) -> None:
    policy = replace(
        DEFAULT_SCREENING_POLICY,
        stage2_warmups=0,
        stage2_pairs=4,
        stage2_bootstrap_resamples=7,
    )
    runtime_policy = RuntimePolicy(
        warmups=policy.stage2_warmups,
        pairs=policy.stage2_pairs,
        seed=17,
        require_exact_balance=policy.stage2_require_exact_balance,
        timeout_ms=100.0,
        bootstrap_resamples=policy.stage2_bootstrap_resamples,
    )
    evidence = RuntimeHarness().measure_pair(
        TimeoutFake(),
        "A",
        "B",
        policy=runtime_policy,
        expected_slow_query="b",
    )
    observed_resamples: list[int] = []
    real_ratio = oracle_module.bootstrap_ratio_ci
    real_delta = oracle_module.bootstrap_delta_ci

    def capture_ratio(samples, *, resamples, seed):
        observed_resamples.append(resamples)
        return real_ratio(samples, resamples=resamples, seed=seed)

    def capture_delta(samples, *, slower_side, resamples, seed):
        observed_resamples.append(resamples)
        return real_delta(
            samples,
            slower_side=slower_side,
            resamples=resamples,
            seed=seed,
        )

    monkeypatch.setattr(oracle_module, "bootstrap_ratio_ci", capture_ratio)
    monkeypatch.setattr(oracle_module, "bootstrap_delta_ci", capture_delta)

    validate_stage2_evidence(
        evidence,
        query_a_hash=evidence.query_a_hash,
        query_b_hash=evidence.query_b_hash,
        expected_orders=paired_schedule(
            policy.stage2_pairs,
            seed=runtime_policy.seed,
            require_exact_balance=policy.stage2_require_exact_balance,
        ),
        expected_slow_query="b",
        seed=runtime_policy.seed,
        timeout_ms=runtime_policy.timeout_ms,
        policy=policy,
    )

    assert observed_resamples == [7, 7]


def test_stage2_replay_uses_paired_delta_for_direction_like_runtime_harness() -> None:
    class SequenceFake:
        def __init__(self) -> None:
            self.values = {
                "A": iter((1.0, 1.0, 3.0, 3.0)),
                "B": iter((1.0, 2.0, 1.0, 4.0)),
            }

        def execute(self, query: str, parameters, **_kwargs) -> float:
            return next(self.values[query])

    policy = replace(
        DEFAULT_SCREENING_POLICY,
        stage2_warmups=0,
        stage2_pairs=4,
        stage2_bootstrap_resamples=7,
    )
    runtime_policy = RuntimePolicy(
        warmups=policy.stage2_warmups,
        pairs=policy.stage2_pairs,
        seed=23,
        require_exact_balance=policy.stage2_require_exact_balance,
        timeout_ms=100.0,
        bootstrap_resamples=policy.stage2_bootstrap_resamples,
    )
    evidence = RuntimeHarness().measure_pair(
        SequenceFake(),
        "A",
        "B",
        policy=runtime_policy,
        expected_slow_query="a",
    )

    assert evidence.reason_codes == ("direction_mismatch",)
    validate_stage2_evidence(
        evidence,
        query_a_hash=evidence.query_a_hash,
        query_b_hash=evidence.query_b_hash,
        expected_orders=paired_schedule(
            policy.stage2_pairs,
            seed=runtime_policy.seed,
            require_exact_balance=policy.stage2_require_exact_balance,
        ),
        expected_slow_query="a",
        seed=runtime_policy.seed,
        timeout_ms=runtime_policy.timeout_ms,
        policy=policy,
    )


def test_stage2_replay_accepts_completed_elapsed_above_requested_query_timeout() -> None:
    class CompletedAfterDeadlineFake:
        def execute(self, query: str, parameters, **_kwargs) -> float:
            return 101.0 if query == "A" else 151.0

    policy = replace(
        DEFAULT_SCREENING_POLICY,
        stage2_warmups=0,
        stage2_pairs=4,
        stage2_bootstrap_resamples=7,
    )
    runtime_policy = RuntimePolicy(
        warmups=policy.stage2_warmups,
        pairs=policy.stage2_pairs,
        seed=29,
        require_exact_balance=policy.stage2_require_exact_balance,
        timeout_ms=100.0,
        bootstrap_resamples=policy.stage2_bootstrap_resamples,
    )
    evidence = RuntimeHarness().measure_pair(
        CompletedAfterDeadlineFake(),
        "A",
        "B",
        policy=runtime_policy,
        expected_slow_query="b",
    )

    assert all(sample.a_ms == 101.0 for sample in evidence.paired_samples)
    assert all(sample.b_ms == 151.0 for sample in evidence.paired_samples)
    assert not any(sample.a_timeout or sample.b_timeout for sample in evidence.paired_samples)
    validate_stage2_evidence(
        evidence,
        query_a_hash=evidence.query_a_hash,
        query_b_hash=evidence.query_b_hash,
        expected_orders=paired_schedule(
            policy.stage2_pairs,
            seed=runtime_policy.seed,
            require_exact_balance=policy.stage2_require_exact_balance,
        ),
        expected_slow_query="b",
        seed=runtime_policy.seed,
        timeout_ms=runtime_policy.timeout_ms,
        policy=policy,
    )
