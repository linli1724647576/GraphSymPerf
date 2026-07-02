"""Symmetry-violation candidate detection."""

from __future__ import annotations

from typing import Any, Mapping

from graph_sym_perf.core import (
    ConfirmationConfig,
    MetricDelta,
    OracleRecord,
    PlanDiff,
    VariantProfileSummary,
    to_jsonable,
)


CORE_METRICS = ("db_hits", "rows", "memory_bytes")
SUPPORT_METRICS = ("page_cache_hits", "page_cache_misses")


def detect_symmetry_violation(
    case_id: str,
    fast: VariantProfileSummary,
    slow: VariantProfileSummary,
    plan_diff: PlanDiff,
    config: ConfirmationConfig,
) -> OracleRecord:
    latency_ratio = _ratio(fast.median_ms, slow.median_ms)
    latency_delta = _delta(fast.median_ms, slow.median_ms)
    latency_worse = (
        latency_ratio is not None
        and latency_delta is not None
        and latency_ratio >= config.min_latency_ratio
        and latency_delta >= config.min_latency_delta_ms
    )
    metric_deltas = tuple(
        _metric_delta(metric, fast.metrics.get(metric), slow.metrics.get(metric), _metric_threshold(metric, config))
        for metric in CORE_METRICS + SUPPORT_METRICS
    )
    core_metric_worse = any(delta.worsened for delta in metric_deltas if delta.metric in CORE_METRICS)
    plan_changed = plan_diff.structural_hash_changed or bool(plan_diff.changes)

    candidate_types = _candidate_types(fast, slow, plan_diff, latency_worse, core_metric_worse)

    # Tiered oracle (document/总体研究主线.md §6):
    #   plan_signal  → structural plan difference (weakest tier)
    #   profile_signal → plan_signal + core metric regression
    #   runtime_signal → profile_signal + stable latency regression (strongest)
    plan_signal = plan_changed and bool(candidate_types)
    profile_signal = plan_signal and core_metric_worse
    runtime_signal = profile_signal and latency_worse

    is_candidate = plan_signal  # plan-only is a legitimate weak candidate tier

    if runtime_signal:
        oracle_support_level = "profile_and_runtime_supported"
    elif profile_signal:
        oracle_support_level = "profile_supported"
    elif plan_signal:
        oracle_support_level = "plan_only"
    else:
        oracle_support_level = "none"

    return OracleRecord(
        case_id=case_id,
        is_candidate=is_candidate,
        candidate_types=tuple(candidate_types),
        reason="SYMMETRY_VIOLATION_CANDIDATE" if is_candidate else "NO_SYMMETRY_VIOLATION_SIGNAL",
        evidence={
            "fast_variant_id": fast.variant_id,
            "slow_variant_id": slow.variant_id,
            "fast_query": fast.query,
            "slow_query": slow.query,
            "latency": {
                "fast_median_ms": fast.median_ms,
                "slow_median_ms": slow.median_ms,
                "ratio": latency_ratio,
                "delta_ms": latency_delta,
                "worsened": latency_worse,
            },
            "oracle_layer_status": {
                "result_equivalence": "pass",
                "profile": "pass" if core_metric_worse else "fail",
                "runtime": "pass" if runtime_signal else "fail",
            },
            "support_level": oracle_support_level,
            "metric_deltas": metric_deltas,
            "plan_changed": plan_changed,
            "structural_hash": {
                "fast": fast.plan.structural_hash if fast.plan else None,
                "slow": slow.plan.structural_hash if slow.plan else None,
                "changed": plan_diff.structural_hash_changed,
            },
            "operator_tree_diff": to_jsonable(plan_diff),
            "access_point_delta": plan_diff.access_point_delta,
            "expand_chain_delta": plan_diff.expand_chain_delta,
            "barrier_delta": plan_diff.barrier_delta,
            "family_delta": plan_diff.family_delta,
            "reproduce": {
                "fast_query_artifact": f"{case_id}/fast_query.cypher",
                "slow_query_artifact": f"{case_id}/slow_query.cypher",
                "command": "python scripts/run_gdsmith_mining.py --config config/graph_sym_perf.yaml --systems neo4j --max-cases 500 --seed 20260522",
            },
        },
    )


def _candidate_types(
    fast: VariantProfileSummary,
    slow: VariantProfileSummary,
    diff: PlanDiff,
    latency_worse: bool,
    metric_worse: bool,
) -> list[str]:
    types: list[str] = []
    if diff.access_point_delta.get("regressed") or _access_points_changed(diff):
        types.append("AccessPointAsymmetry")
    if int(diff.expand_chain_delta.get("delta") or 0) > 0 or int(diff.family_delta.get("Expand", 0)) > 0:
        types.append("ExpansionAsymmetry")
    if int(diff.family_delta.get("Filter", 0)) != 0:
        types.append("PredicatePushdownAsymmetry")
    if int(diff.barrier_delta.get("delta") or 0) > 0 or int(diff.family_delta.get("Barrier", 0)) > 0:
        types.append("JoinOrderAsymmetry")
    if _has_var_length(fast) or _has_var_length(slow):
        types.append("VarLengthEndpointAsymmetry")
    if latency_worse or metric_worse:
        types.append("OutlierVariant")
    return _dedupe(types)


def _metric_threshold(metric: str, config: ConfirmationConfig) -> float:
    if metric == "rows":
        return config.min_rows_ratio
    return config.min_db_hits_ratio


def _metric_delta(metric: str, fast: float | None, slow: float | None, threshold: float) -> MetricDelta:
    if fast is None or slow is None:
        return MetricDelta(metric=metric, original=fast, transformed=slow, ratio=None, delta=None, worsened=False)
    ratio = slow / max(fast, 0.001)
    delta = slow - fast
    return MetricDelta(metric=metric, original=fast, transformed=slow, ratio=ratio, delta=delta, worsened=slow > fast and ratio >= threshold)


def _ratio(fast: float | None, slow: float | None) -> float | None:
    if fast is None or slow is None:
        return None
    return slow / max(fast, 0.001)


def _delta(fast: float | None, slow: float | None) -> float | None:
    if fast is None or slow is None:
        return None
    return slow - fast


def _access_points_changed(diff: PlanDiff) -> bool:
    access = diff.access_point_delta
    return bool(access.get("changed"))


def _has_var_length(profile: VariantProfileSummary) -> bool:
    if "[*" in profile.query:
        return True
    if not profile.plan or not profile.plan.normalized_plan:
        return False
    return any("var" in node.operator_type.lower() and "expand" in node.operator_type.lower() for node in profile.plan.normalized_plan.nodes)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
