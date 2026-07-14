"""A/A negative-control calibration for a single DBMS shard."""

from __future__ import annotations

from statistics import median

from graph_sym_perf.runtime.measurement import ExecuteOnly, RuntimeHarness, paired_schedule
from graph_sym_perf.runtime.models import NoiseCalibration, RuntimePolicy
from graph_sym_perf.runtime.statistics import percentile


def calibrate_aa_noise(
    harness: RuntimeHarness,
    executor: ExecuteOnly,
    probe_queries: tuple[str, ...],
    *,
    pairs_per_query: int = 10,
) -> NoiseCalibration:
    calibration, _samples = collect_aa_noise_samples(
        harness,
        executor,
        probe_queries,
        pairs_per_query=pairs_per_query,
    )
    return calibration


def collect_aa_noise_samples(
    harness: RuntimeHarness,
    executor: ExecuteOnly,
    probe_queries: tuple[str, ...],
    *,
    pairs_per_query: int = 10,
) -> tuple[NoiseCalibration, tuple[tuple[tuple[str, int, int], ...], ...]]:
    probe_samples: list[tuple[tuple[str, int, int], ...]] = []
    for index, query in enumerate(probe_queries):
        evidence = harness.measure_pair(
            executor,
            query,
            query,
            policy=RuntimePolicy(warmups=0, pairs=pairs_per_query, seed=index, require_exact_balance=pairs_per_query % 2 == 0),
        )
        if evidence.verdict.value != "pass":
            return NoiseCalibration(
                valid=False,
                sample_count=sum(len(samples) for samples in probe_samples),
                p95_delta_ms=None,
                p99_delta_ms=None,
                median_order_bias_ms=None,
                long_tail_ratio=None,
                reason_codes=("aa_probe_inconclusive",),
            ), tuple(probe_samples)
        integer_samples: list[tuple[str, int, int]] = []
        for sample in evidence.paired_samples:
            if sample.a_ms is None or sample.b_ms is None:
                return NoiseCalibration(
                    False,
                    sum(len(samples) for samples in probe_samples),
                    None,
                    None,
                    None,
                    None,
                    ("aa_probe_inconclusive",),
                ), tuple(probe_samples)
            integer_samples.append(
                (
                    sample.order,
                    int(round(sample.a_ms * 1000.0)),
                    int(round(sample.b_ms * 1000.0)),
                )
            )
        probe_samples.append(tuple(integer_samples))
    frozen = tuple(probe_samples)
    return summarize_aa_noise_samples(frozen), frozen


def summarize_aa_noise_samples(
    probe_samples_us: tuple[tuple[tuple[str, int, int], ...], ...]
    | list[list[list[object]]],
) -> NoiseCalibration:
    """Recompute fixed-protocol A/A statistics from integer microseconds."""

    deltas: list[float] = []
    signed_ab: list[float] = []
    signed_ba: list[float] = []
    for index, probe_samples in enumerate(probe_samples_us):
        if not isinstance(probe_samples, tuple | list) or not probe_samples:
            raise ValueError("A/A probe samples must be a non-empty sequence")
        if any(
            not isinstance(sample, tuple | list)
            or len(sample) != 3
            or sample[0] not in {"AB", "BA"}
            or type(sample[1]) is not int
            or type(sample[2]) is not int
            or sample[1] < 0
            or sample[2] < 0
            for sample in probe_samples
        ):
            raise ValueError("A/A samples must be ordered non-negative integer microseconds")
        expected_orders = paired_schedule(
            len(probe_samples),
            seed=index,
            require_exact_balance=len(probe_samples) % 2 == 0,
        )
        if tuple(sample[0] for sample in probe_samples) != expected_orders:
            raise ValueError("A/A sample order differs from the fixed schedule")
        for sample in probe_samples:
            signed_delta = (sample[2] - sample[1]) / 1000.0
            deltas.append(abs(signed_delta))
            (signed_ab if sample[0] == "AB" else signed_ba).append(signed_delta)
    p95 = percentile(deltas, 0.95) if deltas else None
    p99 = percentile(deltas, 0.99) if deltas else None
    order_bias = (
        abs(float(median(signed_ab)) - float(median(signed_ba)))
        if signed_ab and signed_ba
        else None
    )
    long_tail = p99 / max(p95, 0.001) if p95 is not None and p99 is not None else None
    if len(deltas) < 200:
        return NoiseCalibration(False, len(deltas), p95, p99, order_bias, long_tail, ("insufficient_aa_samples",))
    if len(probe_samples_us) != 20 or any(len(samples) != 10 for samples in probe_samples_us):
        return NoiseCalibration(False, len(deltas), p95, p99, order_bias, long_tail, ("invalid_aa_protocol",))
    reasons: list[str] = []
    if order_bias is None or p95 is None or order_bias > p95:
        reasons.append("aa_order_bias")
    if long_tail is None or long_tail > 5.0:
        reasons.append("aa_long_tail")
    return NoiseCalibration(not reasons, len(deltas), p95, p99, order_bias, long_tail, tuple(reasons))


__all__ = [
    "calibrate_aa_noise",
    "collect_aa_noise_samples",
    "summarize_aa_noise_samples",
]
