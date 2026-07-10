"""Randomized paired ordinary-execute measurement."""

from __future__ import annotations

import hashlib
import random
from statistics import median
from typing import Any, Mapping, Protocol

from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, relative_mad


class ExecuteOnly(Protocol):
    def execute(self, query: str, parameters: Mapping[str, Any], **kwargs: Any) -> Any: ...


def paired_schedule(pair_count: int, *, seed: int, require_exact_balance: bool) -> tuple[str, ...]:
    if pair_count <= 0:
        raise ValueError("pair_count must be positive")
    if require_exact_balance and pair_count % 2:
        raise ValueError("exactly balanced paired schedule requires an even pair count")
    ab_count = pair_count // 2 if require_exact_balance else (pair_count + 1) // 2
    values = ["AB"] * ab_count + ["BA"] * (pair_count - ab_count)
    random.Random(seed).shuffle(values)
    return tuple(values)


class RuntimeHarness:
    """Measures only ``execute``; adapters exposing profile are deliberately ignored."""

    def measure_pair(
        self,
        executor: ExecuteOnly,
        query_a: str,
        query_b: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        policy: RuntimePolicy,
    ) -> RuntimeEvidence:
        params = parameters or {}
        for _ in range(policy.warmups):
            self._elapsed(executor, query_a, params, policy)
            self._elapsed(executor, query_b, params, policy)
        samples: list[PairedSample] = []
        for order in paired_schedule(policy.pairs, seed=policy.seed, require_exact_balance=policy.require_exact_balance):
            if order == "AB":
                a_ms, a_timeout = self._elapsed(executor, query_a, params, policy)
                b_ms, b_timeout = self._elapsed(executor, query_b, params, policy)
            else:
                b_ms, b_timeout = self._elapsed(executor, query_b, params, policy)
                a_ms, a_timeout = self._elapsed(executor, query_a, params, policy)
            samples.append(PairedSample(order=order, a_ms=a_ms, b_ms=b_ms, a_timeout=a_timeout, b_timeout=b_timeout))
        return _evidence(query_a, query_b, tuple(samples), policy)

    @staticmethod
    def _elapsed(executor: ExecuteOnly, query: str, parameters: Mapping[str, Any], policy: RuntimePolicy) -> tuple[float | None, bool]:
        try:
            result = executor.execute(query, parameters, query_kind="runtime", timeout_seconds=(policy.timeout_ms / 1000 if policy.timeout_ms else None))
        except TimeoutError:
            return None, True
        elapsed = getattr(result, "elapsed_ms", result)
        if isinstance(elapsed, bool) or not isinstance(elapsed, int | float) or elapsed < 0:
            raise TypeError("execute must return a non-negative elapsed milliseconds value or an object with elapsed_ms")
        return float(elapsed), False


def _evidence(query_a: str, query_b: str, samples: tuple[PairedSample, ...], policy: RuntimePolicy) -> RuntimeEvidence:
    complete = tuple((sample.a_ms, sample.b_ms) for sample in samples if sample.a_ms is not None and sample.b_ms is not None)
    a_timeouts = sum(sample.a_timeout for sample in samples)
    b_timeouts = sum(sample.b_timeout for sample in samples)
    reason_codes: list[str] = []
    timeout_lower_bound_ratio: float | None = None
    if a_timeouts and b_timeouts:
        return RuntimeEvidence(RuntimeVerdict.INCONCLUSIVE, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, ("both_timeout",))
    if a_timeouts or b_timeouts:
        timeout_side = "a" if a_timeouts else "b"
        completed = [sample.b_ms if timeout_side == "a" else sample.a_ms for sample in samples if (sample.b_ms if timeout_side == "a" else sample.a_ms) is not None]
        if len(completed) >= 0.8 * len(samples) and policy.timeout_ms and median(completed) > 0:
            timeout_lower_bound_ratio = policy.timeout_ms / float(median(completed))
            reason_codes.append("one_sided_timeout")
        else:
            reason_codes.append("timeout_inconclusive")
        return RuntimeEvidence(RuntimeVerdict.INCONCLUSIVE, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, tuple(reason_codes), timeout_lower_bound_ratio)
    if not complete:
        return RuntimeEvidence(RuntimeVerdict.INCONCLUSIVE, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, ("no_complete_samples",))
    a_values = tuple(float(item[0]) for item in complete)
    b_values = tuple(float(item[1]) for item in complete)
    ratios = [b / a for a, b in complete if a > 0]
    if not ratios:
        return RuntimeEvidence(RuntimeVerdict.INVALID, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, ("nonpositive_baseline",))
    ci = bootstrap_ratio_ci(complete, resamples=policy.bootstrap_resamples, seed=policy.seed)
    return RuntimeEvidence(
        RuntimeVerdict.PASS,
        _hash(query_a),
        _hash(query_b),
        samples,
        float(median(ratios)),
        float(median(b - a for a, b in complete)),
        ci.low,
        ci.high,
        sum(value >= 1.0 for value in ratios) / len(ratios),
        relative_mad(a_values + b_values),
        tuple(reason_codes),
    )


def _hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()
