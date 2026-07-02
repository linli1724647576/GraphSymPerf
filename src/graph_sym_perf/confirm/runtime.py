"""Runtime confirmation helpers."""

from __future__ import annotations

from statistics import median
from typing import Mapping, Protocol

from graph_sym_perf.core import (
    ConfirmationConfig,
    ConfirmationRecord,
    ExecutionResult,
    MetricDelta,
    ProfileRecord,
    QueryPair,
)
from graph_sym_perf.plan import profile_record_from_execution


class RuntimeExecutor(Protocol):
    def profile(
        self,
        query: str,
        parameters: dict[str, object],
        query_kind: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        ...


def evaluate_confirmation(
    case_id: str,
    original_samples_ms: tuple[float, ...],
    transformed_samples_ms: tuple[float, ...],
    config: ConfirmationConfig,
    original_profiles: tuple[ProfileRecord, ...] = (),
    transformed_profiles: tuple[ProfileRecord, ...] = (),
) -> ConfirmationRecord:
    if not original_samples_ms or not transformed_samples_ms:
        return ConfirmationRecord(
            case_id=case_id,
            confirmed=False,
            original_median_ms=None,
            transformed_median_ms=None,
            latency_ratio=None,
            latency_delta_ms=None,
            original_samples_ms=original_samples_ms,
            transformed_samples_ms=transformed_samples_ms,
            reason="NO_RUNTIME_SAMPLES",
            level="none",
            support_level="plan_only",
        )
    original_median = float(median(original_samples_ms))
    transformed_median = float(median(transformed_samples_ms))
    ratio = transformed_median / max(original_median, 0.001)
    delta = transformed_median - original_median
    latency_worsened = ratio >= config.min_latency_ratio and delta >= config.min_latency_delta_ms
    metric_deltas = _metric_deltas(original_profiles, transformed_profiles, config)
    workload_worsened = any(item.worsened for item in metric_deltas)
    level = _confirmation_level(latency_worsened, workload_worsened, original_profiles, transformed_profiles)
    support_level = _support_level(latency_worsened, workload_worsened)
    confirmed = workload_worsened
    return ConfirmationRecord(
        case_id=case_id,
        confirmed=confirmed,
        original_median_ms=original_median,
        transformed_median_ms=transformed_median,
        latency_ratio=ratio,
        latency_delta_ms=delta,
        original_samples_ms=original_samples_ms,
        transformed_samples_ms=transformed_samples_ms,
        reason=_confirmation_reason(level, latency_worsened, workload_worsened),
        level=level,
        support_level=support_level,
        metric_deltas=metric_deltas,
        profile_samples=_profile_samples(original_profiles, transformed_profiles),
    )


def confirm_runtime(executor: RuntimeExecutor, pair: QueryPair, config: ConfirmationConfig) -> ConfirmationRecord:
    parameters = dict(pair.parameters)
    for _ in range(max(0, config.warmup)):
        executor.profile(pair.original_query, parameters, "original_warmup", timeout_seconds=config.timeout_seconds)
        executor.profile(pair.transformed_query, parameters, "transformed_warmup", timeout_seconds=config.timeout_seconds)
    original_samples: list[float] = []
    transformed_samples: list[float] = []
    original_profiles: list[ProfileRecord] = []
    transformed_profiles: list[ProfileRecord] = []
    for _ in range(max(1, config.repeats)):
        original_result = executor.profile(
            pair.original_query,
            parameters,
            "original_profile",
            timeout_seconds=config.timeout_seconds,
        )
        transformed_result = executor.profile(
            pair.transformed_query,
            parameters,
            "transformed_profile",
            timeout_seconds=config.timeout_seconds,
        )
        original_samples.append(original_result.elapsed_ms)
        transformed_samples.append(transformed_result.elapsed_ms)
        original_profiles.append(profile_record_from_execution(original_result))
        transformed_profiles.append(profile_record_from_execution(transformed_result))
    return evaluate_confirmation(
        pair.case_id,
        tuple(original_samples),
        tuple(transformed_samples),
        config,
        tuple(original_profiles),
        tuple(transformed_profiles),
    )


def _metric_deltas(
    original_profiles: tuple[ProfileRecord, ...],
    transformed_profiles: tuple[ProfileRecord, ...],
    config: ConfirmationConfig,
) -> tuple[MetricDelta, ...]:
    return tuple(
        _metric_delta(
            metric,
            _median_metric(original_profiles, metric),
            _median_metric(transformed_profiles, metric),
            _metric_threshold(metric, config),
        )
        for metric in ("db_hits", "rows", "page_cache_hits", "page_cache_misses", "memory_bytes")
    )


def _median_metric(records: tuple[ProfileRecord, ...], metric: str) -> float | None:
    values = [float(record.metrics[metric]) for record in records if metric in record.metrics]
    return float(median(values)) if values else None


def _metric_threshold(metric: str, config: ConfirmationConfig) -> float:
    if metric == "rows":
        return config.min_rows_ratio
    return config.min_db_hits_ratio


def _metric_delta(metric: str, original: float | None, transformed: float | None, threshold: float) -> MetricDelta:
    if original is None or transformed is None:
        return MetricDelta(metric, original, transformed, None, None, False)
    ratio = transformed / max(original, 0.001)
    delta = transformed - original
    return MetricDelta(metric, original, transformed, ratio, delta, transformed > original and ratio >= threshold)


def _confirmation_level(
    latency_worsened: bool,
    workload_worsened: bool,
    original_profiles: tuple[ProfileRecord, ...],
    transformed_profiles: tuple[ProfileRecord, ...],
) -> str:
    if workload_worsened and latency_worsened:
        return "strong"
    if workload_worsened:
        return "moderate"
    if original_profiles or transformed_profiles:
        return "weak"
    return "none"


def _support_level(latency_worsened: bool, workload_worsened: bool) -> str:
    del latency_worsened
    if workload_worsened:
        return "profile_supported"
    return "plan_only"


def _confirmation_reason(level: str, latency_worsened: bool, workload_worsened: bool) -> str:
    if workload_worsened:
        if latency_worsened:
            return "PROFILE_METRIC_REGRESSION_WITH_LATENCY"
        return "PROFILE_METRIC_REGRESSION"
    if latency_worsened:
        return "RUNTIME_WITHOUT_PROFILE_SUPPORT"
    if level == "weak":
        return "PLAN_ONLY_WEAK"
    return "RUNTIME_THRESHOLD_NOT_MET"


def _profile_samples(
    original_profiles: tuple[ProfileRecord, ...],
    transformed_profiles: tuple[ProfileRecord, ...],
) -> Mapping[str, object]:
    return {
        "original": tuple(_profile_sample(record) for record in original_profiles),
        "transformed": tuple(_profile_sample(record) for record in transformed_profiles),
    }


def _profile_sample(record: ProfileRecord) -> Mapping[str, object]:
    return {
        "elapsed_ms": record.elapsed_ms,
        "available": record.available,
        "metrics": dict(record.metrics),
        "structural_hash": record.plan.structural_hash if record.plan else None,
    }
