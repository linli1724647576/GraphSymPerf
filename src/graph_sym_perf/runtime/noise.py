"""A/A negative-control calibration for a single DBMS shard."""

from __future__ import annotations

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
    order_effects: list[float] = []
    for index, query in enumerate(probe_queries):
        evidence = harness.measure_pair(
            executor,
            query,
            query,
            policy=RuntimePolicy(warmups=0, pairs=pairs_per_query, seed=index, require_exact_balance=pairs_per_query % 2 == 0),
        )
        if evidence.verdict.value != "pass":
            return NoiseCalibration(False, len(deltas), None, ("aa_probe_inconclusive",))
        for sample in evidence.paired_samples:
            if sample.a_ms is None or sample.b_ms is None:
                continue
            deltas.append(abs(sample.b_ms - sample.a_ms))
            first_ms, second_ms = (sample.a_ms, sample.b_ms) if sample.order == "AB" else (sample.b_ms, sample.a_ms)
            order_effects.append(second_ms - first_ms)
    if len(deltas) < 200:
        return NoiseCalibration(False, len(deltas), None, ("insufficient_aa_samples",))
    if len(probe_queries) != 20 or pairs_per_query != 10:
        return NoiseCalibration(False, len(deltas), None, ("invalid_aa_protocol",))
    dominant_order_effect = max(
        sum(effect > 0 for effect in order_effects),
        sum(effect < 0 for effect in order_effects),
    )
    if dominant_order_effect >= 0.8 * len(order_effects):
        return NoiseCalibration(False, len(deltas), None, ("aa_order_bias",))
    return NoiseCalibration(True, len(deltas), percentile(deltas, 0.99), ())
