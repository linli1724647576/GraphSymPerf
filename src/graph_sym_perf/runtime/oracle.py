"""Strict runtime decision gates; plan and profile evidence are intentionally absent."""

from __future__ import annotations

import math
from statistics import median

from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.runtime.screening import ScreeningPolicy
from graph_sym_perf.runtime.statistics import (
    bootstrap_delta_ci,
    bootstrap_ratio_ci,
    relative_mad,
)


def evaluate_runtime(
    evidence: RuntimeEvidence,
    *,
    noise_floor_ms: float,
    min_ratio: float = 3.0,
    min_ci_low: float = 2.0,
    timer_resolution_ms: float = 0.001,
) -> RuntimeEvidence:
    if evidence.verdict is not RuntimeVerdict.PASS:
        return evidence
    reasons = list(evidence.reason_codes)
    if evidence.median_ratio is None or evidence.median_ratio < min_ratio:
        reasons.append("ratio_below_threshold")
    if evidence.ci_ratio_low is None or evidence.ci_ratio_low < min_ci_low:
        reasons.append("ci_lower_below_threshold")
    if evidence.direction_consistency <= 0.5:
        reasons.append("direction_unstable")
    minimum_delta = max(noise_floor_ms, 10 * timer_resolution_ms)
    if evidence.median_delta_ms is None or evidence.median_delta_ms <= minimum_delta:
        reasons.append("delta_within_noise")
    if reasons:
        return RuntimeEvidence(**{**evidence.__dict__, "verdict": RuntimeVerdict.FAIL, "reason_codes": tuple(reasons)})
    return evidence


def validate_stage2_evidence(
    evidence: RuntimeEvidence,
    *,
    query_a_hash: str,
    query_b_hash: str,
    expected_orders: tuple[str, ...],
    expected_slow_query: str,
    seed: int,
    timeout_ms: float,
    policy: ScreeningPolicy,
) -> None:
    """Recompute a persisted Stage-2 summary from its raw paired samples."""

    if expected_slow_query not in {"a", "b"}:
        raise ValueError("expected_slow_query must be 'a' or 'b'")
    if not math.isfinite(timeout_ms) or timeout_ms <= 0:
        raise ValueError("timeout_ms must be finite and positive")
    if len(evidence.paired_samples) != len(expected_orders):
        raise ValueError("Stage-2 sample count differs from the fixed protocol")
    for sample, expected_order in zip(
        evidence.paired_samples,
        expected_orders,
        strict=True,
    ):
        if sample.order != expected_order:
            raise ValueError("Stage-2 sample order differs from the fixed schedule")
        if type(sample.a_timeout) is not bool or type(sample.b_timeout) is not bool:
            raise ValueError("Stage-2 timeout flags must be booleans")
        for value, timed_out in (
            (sample.a_ms, sample.a_timeout),
            (sample.b_ms, sample.b_timeout),
        ):
            if (value is None) is not timed_out:
                raise ValueError("Stage-2 timeout flag differs from its sample")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("Stage-2 elapsed sample is invalid")
    expected = _canonical_stage2_evidence(
        evidence.paired_samples,
        query_a_hash=query_a_hash,
        query_b_hash=query_b_hash,
        expected_slow_query=expected_slow_query,
        seed=seed,
        timeout_ms=timeout_ms,
        policy=policy,
    )
    if evidence != expected:
        raise ValueError("Stage-2 summary differs from its canonical paired samples")


def _canonical_stage2_evidence(
    samples,
    *,
    query_a_hash: str,
    query_b_hash: str,
    expected_slow_query: str,
    seed: int,
    timeout_ms: float,
    policy: ScreeningPolicy,
) -> RuntimeEvidence:
    complete = tuple(
        (sample.a_ms, sample.b_ms)
        for sample in samples
        if sample.a_ms is not None and sample.b_ms is not None
    )
    a_timeouts = sum(sample.a_timeout for sample in samples)
    b_timeouts = sum(sample.b_timeout for sample in samples)
    if a_timeouts and b_timeouts:
        return RuntimeEvidence(
            RuntimeVerdict.INCONCLUSIVE,
            query_a_hash,
            query_b_hash,
            samples,
            None,
            None,
            None,
            None,
            0.0,
            None,
            ("both_timeout",),
            slow_query=expected_slow_query,
        )
    if a_timeouts or b_timeouts:
        timeout_side = "a" if a_timeouts else "b"
        timeout_count = a_timeouts if timeout_side == "a" else b_timeouts
        completed = [
            sample.b_ms if timeout_side == "a" else sample.a_ms
            for sample in samples
            if (sample.b_ms if timeout_side == "a" else sample.a_ms) is not None
        ]
        reasons: list[str] = []
        lower_bound: float | None = None
        if (
            timeout_count >= 0.8 * len(samples)
            and len(completed) >= 0.8 * len(samples)
            and median(completed) > 0
        ):
            lower_bound = timeout_ms / float(median(completed))
            reasons.append("one_sided_timeout")
        else:
            reasons.append("timeout_inconclusive")
        if expected_slow_query != timeout_side:
            reasons.append("direction_mismatch")
        return RuntimeEvidence(
            RuntimeVerdict.INCONCLUSIVE,
            query_a_hash,
            query_b_hash,
            samples,
            None,
            None,
            None,
            None,
            0.0,
            None,
            tuple(reasons),
            lower_bound,
            expected_slow_query,
        )
    if not complete:
        return RuntimeEvidence(
            RuntimeVerdict.INCONCLUSIVE,
            query_a_hash,
            query_b_hash,
            samples,
            None,
            None,
            None,
            None,
            0.0,
            None,
            ("no_complete_samples",),
        )

    median_a_ms = float(median(a for a, _b in complete))
    median_b_ms = float(median(b for _a, b in complete))
    paired_delta_ms = float(median(b - a for a, b in complete))
    observed_slow_query = (
        "b"
        if paired_delta_ms > 0
        else "a"
        if paired_delta_ms < 0
        else None
    )
    reasons = (
        ("direction_mismatch",)
        if observed_slow_query != expected_slow_query
        else ()
    )
    oriented = tuple(
        (b, a) if expected_slow_query == "a" else (a, b)
        for a, b in complete
    )
    ratios = [slow / fast for fast, slow in oriented if fast > 0]
    if not ratios:
        return RuntimeEvidence(
            RuntimeVerdict.INVALID,
            query_a_hash,
            query_b_hash,
            samples,
            None,
            None,
            None,
            None,
            0.0,
            None,
            ("nonpositive_baseline",),
        )
    ratio_ci = bootstrap_ratio_ci(
        oriented,
        resamples=policy.stage2_bootstrap_resamples,
        seed=seed,
    )
    delta_ci = bootstrap_delta_ci(
        complete,
        slower_side=expected_slow_query,
        resamples=policy.stage2_bootstrap_resamples,
        seed=seed,
    )
    return RuntimeEvidence(
        RuntimeVerdict.PASS,
        query_a_hash,
        query_b_hash,
        samples,
        float(median(ratios)),
        float(median(slow - fast for fast, slow in oriented)),
        ratio_ci.low,
        ratio_ci.high,
        sum(value > 1.0 for value in ratios) / len(ratios),
        relative_mad(ratios),
        reasons,
        slow_query=expected_slow_query,
        median_a_ms=median_a_ms,
        median_b_ms=median_b_ms,
        slower_side=expected_slow_query,
        ci_delta_low_ms=delta_ci.low,
        ci_delta_high_ms=delta_ci.high,
    )


__all__ = ["evaluate_runtime", "validate_stage2_evidence"]
