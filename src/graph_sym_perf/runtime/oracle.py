"""Strict runtime decision gates; plan and profile evidence are intentionally absent."""

from __future__ import annotations

from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimeVerdict


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
    minimum_delta = max(noise_floor_ms, 10 * timer_resolution_ms)
    if evidence.median_delta_ms is None or evidence.median_delta_ms <= minimum_delta:
        reasons.append("delta_within_noise")
    if reasons:
        return RuntimeEvidence(**{**evidence.__dict__, "verdict": RuntimeVerdict.FAIL, "reason_codes": tuple(reasons)})
    return evidence
