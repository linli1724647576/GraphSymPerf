"""Randomized paired ordinary-execute measurement."""

from __future__ import annotations

import hashlib
import math
import random
from statistics import median
from typing import Any, Mapping, Protocol

from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.runtime.oracle import evaluate_runtime
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, relative_mad


class ExecuteOnly(Protocol):
    def execute(self, query: str, parameters: Mapping[str, Any], **kwargs: Any) -> Any: ...


class _InvalidMeasurement(ValueError):
    pass


class _ExecutionFailure(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


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

    def measure_discovery(
        self,
        executor: ExecuteOnly,
        query_a: str,
        query_b: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        policy: RuntimePolicy,
    ) -> RuntimeEvidence:
        """Run the fixed five-pair discovery protocol without an issue-grade verdict."""

        if policy.pairs != 5 or policy.require_exact_balance or policy.bootstrap_resamples != 10_000:
            raise ValueError("discovery requires exactly 5 pairs, approximate balance, and 10000 bootstrap resamples")
        return self.measure_pair(executor, query_a, query_b, parameters=parameters, policy=policy)

    def measure_validation(
        self,
        executor: ExecuteOnly,
        query_a: str,
        query_b: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        policy: RuntimePolicy,
        expected_slow_query: str,
        noise_floor_ms: float,
        timer_resolution_ms: float,
    ) -> RuntimeEvidence:
        """Run the fixed validation protocol and return its issue-grade verdict."""

        if policy.pairs != 20 or not policy.require_exact_balance or policy.bootstrap_resamples != 10_000:
            raise ValueError("validation requires exactly 20 pairs, exact balance, and 10000 bootstrap resamples")
        raw_evidence = self.measure_pair(
            executor,
            query_a,
            query_b,
            parameters=parameters,
            policy=policy,
            expected_slow_query=expected_slow_query,
        )
        return evaluate_runtime(
            raw_evidence,
            noise_floor_ms=noise_floor_ms,
            timer_resolution_ms=timer_resolution_ms,
        )

    def measure_pair(
        self,
        executor: ExecuteOnly,
        query_a: str,
        query_b: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        policy: RuntimePolicy,
        expected_slow_query: str | None = None,
    ) -> RuntimeEvidence:
        """Collect low-level samples; PASS here means measurement validity only."""

        if expected_slow_query not in {None, "a", "b"}:
            raise ValueError("expected_slow_query must be 'a', 'b', or None")
        params = parameters or {}
        samples: list[PairedSample] = []
        try:
            for _ in range(policy.warmups):
                self._elapsed(executor, query_a, params, policy)
                self._elapsed(executor, query_b, params, policy)
            for order in paired_schedule(policy.pairs, seed=policy.seed, require_exact_balance=policy.require_exact_balance):
                if order == "AB":
                    a_ms, a_timeout = self._elapsed(executor, query_a, params, policy)
                    b_ms, b_timeout = self._elapsed(executor, query_b, params, policy)
                else:
                    b_ms, b_timeout = self._elapsed(executor, query_b, params, policy)
                    a_ms, a_timeout = self._elapsed(executor, query_a, params, policy)
                samples.append(PairedSample(order=order, a_ms=a_ms, b_ms=b_ms, a_timeout=a_timeout, b_timeout=b_timeout))
        except _InvalidMeasurement as exc:
            return _invalid_evidence(query_a, query_b, tuple(samples), str(exc), expected_slow_query)
        except _ExecutionFailure as exc:
            return _invalid_evidence(
                query_a,
                query_b,
                tuple(samples),
                f"execution_error:{exc.error_type}",
                expected_slow_query,
            )
        return _evidence(query_a, query_b, tuple(samples), policy, expected_slow_query=expected_slow_query)

    @staticmethod
    def _elapsed(executor: ExecuteOnly, query: str, parameters: Mapping[str, Any], policy: RuntimePolicy) -> tuple[float | None, bool]:
        try:
            result = executor.execute(query, parameters, query_kind="runtime", timeout_seconds=(policy.timeout_ms / 1000 if policy.timeout_ms else None))
        except TimeoutError:
            return None, True
        except (AssertionError, AttributeError, TypeError):
            raise
        except Exception as exc:
            raise _ExecutionFailure(type(exc).__name__) from exc
        elapsed = getattr(result, "elapsed_ms", result)
        if isinstance(elapsed, bool) or not isinstance(elapsed, int | float):
            raise _InvalidMeasurement("invalid_elapsed")
        if not math.isfinite(elapsed):
            raise _InvalidMeasurement("non_finite_elapsed")
        if elapsed < 0:
            raise _InvalidMeasurement("invalid_elapsed")
        return float(elapsed), False


def _evidence(
    query_a: str,
    query_b: str,
    samples: tuple[PairedSample, ...],
    policy: RuntimePolicy,
    *,
    expected_slow_query: str | None,
) -> RuntimeEvidence:
    complete = tuple((sample.a_ms, sample.b_ms) for sample in samples if sample.a_ms is not None and sample.b_ms is not None)
    a_timeouts = sum(sample.a_timeout for sample in samples)
    b_timeouts = sum(sample.b_timeout for sample in samples)
    reason_codes: list[str] = []
    timeout_lower_bound_ratio: float | None = None
    if a_timeouts and b_timeouts:
        return RuntimeEvidence(
            RuntimeVerdict.INCONCLUSIVE,
            _hash(query_a),
            _hash(query_b),
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
        slow_query = expected_slow_query or timeout_side
        completed = [sample.b_ms if timeout_side == "a" else sample.a_ms for sample in samples if (sample.b_ms if timeout_side == "a" else sample.a_ms) is not None]
        if timeout_count >= 0.8 * len(samples) and len(completed) >= 0.8 * len(samples) and policy.timeout_ms and median(completed) > 0:
            timeout_lower_bound_ratio = policy.timeout_ms / float(median(completed))
            reason_codes.append("one_sided_timeout")
        else:
            reason_codes.append("timeout_inconclusive")
        if expected_slow_query is not None and expected_slow_query != timeout_side:
            reason_codes.append("direction_mismatch")
        return RuntimeEvidence(
            RuntimeVerdict.INCONCLUSIVE,
            _hash(query_a),
            _hash(query_b),
            samples,
            None,
            None,
            None,
            None,
            0.0,
            None,
            tuple(reason_codes),
            timeout_lower_bound_ratio,
            slow_query,
        )
    if not complete:
        return RuntimeEvidence(RuntimeVerdict.INCONCLUSIVE, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, ("no_complete_samples",))
    direction_delta = float(median(b - a for a, b in complete))
    observed_slow_query = "b" if direction_delta > 0 else "a" if direction_delta < 0 else None
    slow_query = expected_slow_query or observed_slow_query
    if expected_slow_query is not None and observed_slow_query != expected_slow_query:
        reason_codes.append("direction_mismatch")
    oriented = tuple((a, b) if slow_query != "a" else (b, a) for a, b in complete)
    ratios = [slow / fast for fast, slow in oriented if fast > 0]
    if not ratios:
        return RuntimeEvidence(RuntimeVerdict.INVALID, _hash(query_a), _hash(query_b), samples, None, None, None, None, 0.0, None, ("nonpositive_baseline",))
    ci = bootstrap_ratio_ci(oriented, resamples=policy.bootstrap_resamples, seed=policy.seed)
    return RuntimeEvidence(
        RuntimeVerdict.PASS,
        _hash(query_a),
        _hash(query_b),
        samples,
        float(median(ratios)),
        float(median(slow - fast for fast, slow in oriented)),
        ci.low,
        ci.high,
        sum(value > 1.0 for value in ratios) / len(ratios),
        relative_mad(ratios),
        tuple(reason_codes),
        slow_query=slow_query,
    )


def _hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _invalid_evidence(
    query_a: str,
    query_b: str,
    samples: tuple[PairedSample, ...],
    reason: str,
    slow_query: str | None,
) -> RuntimeEvidence:
    return RuntimeEvidence(
        RuntimeVerdict.INVALID,
        _hash(query_a),
        _hash(query_b),
        samples,
        None,
        None,
        None,
        None,
        0.0,
        None,
        (reason,),
        slow_query=slow_query,
    )
