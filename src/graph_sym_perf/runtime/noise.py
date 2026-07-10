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
    for index, query in enumerate(probe_queries):
        evidence = harness.measure_pair(
            executor,
            query,
            query,
            policy=RuntimePolicy(warmups=0, pairs=pairs_per_query, seed=index, require_exact_balance=pairs_per_query % 2 == 0),
        )
        if evidence.verdict.value != "pass":
            return NoiseCalibration(False, len(deltas), None, ("aa_probe_inconclusive",))
        deltas.extend(abs(sample.b_ms - sample.a_ms) for sample in evidence.paired_samples if sample.a_ms is not None and sample.b_ms is not None)
    if len(deltas) < 200:
        return NoiseCalibration(False, len(deltas), None, ("insufficient_aa_samples",))
    return NoiseCalibration(True, len(deltas), percentile(deltas, 0.99), ())
