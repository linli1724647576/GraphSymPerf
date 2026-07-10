"""Deterministic robust statistics for paired runtime samples."""

from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class BootstrapInterval:
    low: float
    high: float


def relative_mad(samples: tuple[float, ...] | list[float]) -> float | None:
    if not samples:
        return None
    center = float(median(samples))
    if center <= 0:
        return None
    return float(median([abs(value - center) for value in samples]) / center)


def bootstrap_ratio_ci(
    samples: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    *,
    resamples: int = 10_000,
    seed: int = 0,
) -> BootstrapInterval:
    if not samples:
        raise ValueError("bootstrap requires at least one paired sample")
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    random_source = random.Random(seed)
    ratios: list[float] = []
    for _ in range(resamples):
        selected = [samples[random_source.randrange(len(samples))] for _ in range(len(samples))]
        a_median = float(median(value[0] for value in selected))
        b_median = float(median(value[1] for value in selected))
        if a_median > 0:
            ratios.append(b_median / a_median)
    if not ratios:
        raise ValueError("bootstrap samples have no positive baseline median")
    ratios.sort()
    return BootstrapInterval(low=_quantile(ratios, 0.025), high=_quantile(ratios, 0.975))


def percentile(values: list[float] | tuple[float, ...], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    return _quantile(sorted(values), quantile)


def _quantile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = (len(sorted_values) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = index - lower
    return float(sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction)
