"""Runtime-first data contracts with no plan or profile fields."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


@dataclass(frozen=True)
class RuntimePolicy:
    warmups: int
    pairs: int
    seed: int = 0
    require_exact_balance: bool = False
    timeout_ms: float | None = None
    bootstrap_resamples: int = 10_000


@dataclass(frozen=True)
class PairedSample:
    order: str
    a_ms: float | None
    b_ms: float | None
    a_timeout: bool = False
    b_timeout: bool = False


@dataclass(frozen=True)
class RuntimeEvidence:
    verdict: RuntimeVerdict
    query_a_hash: str
    query_b_hash: str
    paired_samples: tuple[PairedSample, ...]
    median_ratio: float | None
    median_delta_ms: float | None
    ci_ratio_low: float | None
    ci_ratio_high: float | None
    direction_consistency: float
    relative_mad: float | None
    reason_codes: tuple[str, ...]
    timeout_lower_bound_ratio: float | None = None
    slow_query: str | None = None


@dataclass(frozen=True)
class NoiseCalibration:
    valid: bool
    sample_count: int
    p99_delta_ms: float | None
    reason_codes: tuple[str, ...]
