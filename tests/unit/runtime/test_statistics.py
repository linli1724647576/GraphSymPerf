from __future__ import annotations

from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, relative_mad


def test_bootstrap_ratio_ci_is_seeded_and_uses_paired_observations() -> None:
    samples = ((10.0, 40.0), (11.0, 44.0), (9.0, 36.0), (10.0, 40.0))

    first = bootstrap_ratio_ci(samples, resamples=500, seed=3)
    second = bootstrap_ratio_ci(samples, resamples=500, seed=3)

    assert first == second
    assert first.low >= 3.0
    assert relative_mad((10.0, 11.0, 9.0, 10.0)) > 0


def test_bootstrap_uses_median_of_paired_ratios_not_ratio_of_medians() -> None:
    samples = (
        (7.0, 79.0),
        (7.0, 72.0),
        (15.0, 91.0),
        (8.0, 51.0),
        (1.0, 70.0),
        (12.0, 53.0),
    )

    interval = bootstrap_ratio_ci(samples, resamples=500, seed=3)

    assert interval.low == 5.241666666666667
    assert interval.high == 40.642857142857146
