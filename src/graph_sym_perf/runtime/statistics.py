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
        paired_ratios = [slow / fast for fast, slow in selected if fast > 0]
        if paired_ratios:
            ratios.append(float(median(paired_ratios)))
    if not ratios:
        raise ValueError("bootstrap samples have no positive baseline median")
    ratios.sort()
    return BootstrapInterval(low=_quantile(ratios, 0.025), high=_quantile(ratios, 0.975))


def bootstrap_delta_ci(
    samples: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    *,
    slower_side: str | None = None,
    resamples: int = 10_000,
    seed: int = 0,
) -> BootstrapInterval:
    """Bootstrap the paired median delta in one frozen runtime direction."""

    if not samples:
        raise ValueError("bootstrap requires at least one paired sample")
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    if slower_side not in {None, "a", "b"}:
        raise ValueError("slower_side must be 'a', 'b', or None")
    if slower_side is None:
        median_a = float(median(a for a, _b in samples))
        median_b = float(median(b for _a, b in samples))
        slower_side = "a" if median_a > median_b else "b"
    random_source = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        selected = [samples[random_source.randrange(len(samples))] for _ in range(len(samples))]
        oriented = [a - b if slower_side == "a" else b - a for a, b in selected]
        deltas.append(float(median(oriented)))
    deltas.sort()
    return BootstrapInterval(low=_quantile(deltas, 0.025), high=_quantile(deltas, 0.975))


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
