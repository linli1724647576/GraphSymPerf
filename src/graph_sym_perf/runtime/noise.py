"""A/A negative-control calibration for a single DBMS shard."""

from __future__ import annotations

from statistics import median

from graph_sym_perf.runtime.measurement import ExecuteOnly, RuntimeHarness
from graph_sym_perf.runtime.models import NoiseCalibration, RuntimePolicy
from graph_sym_perf.runtime.statistics import percentile


def calibrate_aa_noise(
    harness: RuntimeHarness,
    executor: ExecuteOnly,
    probe_queries: tuple[str, ...],
    *,
    pairs_per_query: int = 10,
) -> NoiseCalibration:
    deltas: list[float] = []
    signed_ab: list[float] = []
    signed_ba: list[float] = []
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
                sample_count=len(deltas),
                p95_delta_ms=None,
                p99_delta_ms=None,
                median_order_bias_ms=None,
                long_tail_ratio=None,
                reason_codes=("aa_probe_inconclusive",),
            )
        for sample in evidence.paired_samples:
            if sample.a_ms is None or sample.b_ms is None:
                continue
            deltas.append(abs(sample.b_ms - sample.a_ms))
            signed_delta = sample.b_ms - sample.a_ms
            (signed_ab if sample.order == "AB" else signed_ba).append(signed_delta)
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
    if len(probe_queries) != 20 or pairs_per_query != 10:
        return NoiseCalibration(False, len(deltas), p95, p99, order_bias, long_tail, ("invalid_aa_protocol",))
    reasons: list[str] = []
    if order_bias is None or p95 is None or order_bias > p95:
        reasons.append("aa_order_bias")
    if long_tail is None or long_tail > 5.0:
        reasons.append("aa_long_tail")
    return NoiseCalibration(not reasons, len(deltas), p95, p99, order_bias, long_tail, tuple(reasons))
