from __future__ import annotations

from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, relative_mad


def test_bootstrap_ratio_ci_is_seeded_and_uses_paired_observations() -> None:
    samples = ((10.0, 40.0), (11.0, 44.0), (9.0, 36.0), (10.0, 40.0))

    first = bootstrap_ratio_ci(samples, resamples=500, seed=3)
    second = bootstrap_ratio_ci(samples, resamples=500, seed=3)

    assert first == second
    assert first.low >= 3.0
    assert relative_mad((10.0, 11.0, 9.0, 10.0)) > 0
