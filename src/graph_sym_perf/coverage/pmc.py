"""PMC extraction from normalized plan diffs."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from graph_sym_perf.core import CoverageConfig, MetricDelta, PlanDiff
from graph_sym_perf.coverage.model import CoverageUnit, PMCEvidence

DEFAULT_RATIO_BUCKETS: tuple[float, ...] = (1.5, 2.0, 5.0, 10.0, 50.0, 100.0, 1000.0)
ROW_METRIC_ALIASES = {"rows", "actual_rows", "records", "records_produced", "row_work"}
DB_HIT_METRIC_ALIASES = {"db_hits", "actual_hits", "actual_db_hits", "dbhits"}
LATENCY_METRIC_ALIASES = {"latency_ms", "median_ms", "elapsed_ms", "runtime_ms"}


def extract_pmc_units(
    *,
    case_id: str,
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
    fast_variant_id: str | None,
    slow_variant_id: str | None,
    plan_diff: PlanDiff,
    metric_thresholds: Mapping[str, float] | None = None,
) -> tuple[CoverageUnit, ...]:
    del case_id, fast_variant_id, slow_variant_id
    return _extract_pmc_units(
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        plan_diff=plan_diff,
        ratio_buckets=DEFAULT_RATIO_BUCKETS,
        metric_thresholds=metric_thresholds,
    )


def make_pmc_evidence(
    *,
    case_id: str,
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
    fast_variant_id: str | None,
    slow_variant_id: str | None,
    plan_diff: PlanDiff,
    coverage_config: CoverageConfig | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PMCEvidence:
    config = coverage_config or CoverageConfig()
    units = _extract_pmc_units(
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        plan_diff=plan_diff,
        ratio_buckets=tuple(config.ratio_buckets),
        metric_thresholds=None,
    )
    if not config.include_transformation_context:
        units = tuple(unit for unit in units if unit.kind != "transformation_context")
    return PMCEvidence(
        case_id=case_id,
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        fast_variant_id=fast_variant_id,
        slow_variant_id=slow_variant_id,
        structural_hash_changed=plan_diff.structural_hash_changed,
        units=units,
        unit_count=len(units),
        metadata=dict(metadata or {}),
    )


def ratio_bucket_label(ratio: float | None, buckets: Iterable[float] = DEFAULT_RATIO_BUCKETS) -> str | None:
    if ratio is None:
        return None
    bucket_values = tuple(float(value) for value in buckets)
    if not bucket_values:
        return None
    if ratio < bucket_values[0]:
        return "below"
    selected = bucket_values[0]
    for bucket in bucket_values:
        if ratio >= bucket:
            selected = bucket
        else:
            break
    return f"{selected:g}x"


def _extract_pmc_units(
    *,
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
    plan_diff: PlanDiff,
    ratio_buckets: tuple[float, ...],
    metric_thresholds: Mapping[str, float] | None,
) -> tuple[CoverageUnit, ...]:
    units: list[CoverageUnit] = []
    _add_structural_units(units, plan_diff)
    _add_operator_units(units, plan_diff)
    _add_access_units(units, plan_diff)
    _add_expand_units(units, plan_diff)
    _add_predicate_units(units, plan_diff)
    _add_barrier_units(units, plan_diff)
    _add_cardinality_units(units, plan_diff, ratio_buckets, metric_thresholds)
    _add_work_units(units, plan_diff.metric_deltas, ratio_buckets, metric_thresholds)
    _add_context_units(units, transform_family, transform_rule, safety_tier)
    return _dedupe_units(units)


def _add_structural_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    if plan_diff.structural_hash_changed:
        units.append(_unit("structural", "structural_hash_changed", "Structural hash changed"))
        return
    if any(delta.ratio not in (None, 1.0) or delta.delta not in (None, 0.0) for delta in plan_diff.metric_deltas):
        units.append(
            _unit(
                "structural",
                "structural_hash_unchanged_but_metrics_changed",
                "Structural hash unchanged but metrics changed",
            )
        )


def _add_operator_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    for node in plan_diff.added_nodes:
        family = str(node.get("operator_family") or node.get("operator_type") or "Unknown")
        units.append(_unit("operator_delta", f"operator_added:{family}", f"Operator added: {family}"))
    for node in plan_diff.removed_nodes:
        family = str(node.get("operator_family") or node.get("operator_type") or "Unknown")
        units.append(_unit("operator_delta", f"operator_removed:{family}", f"Operator removed: {family}"))
    for item in plan_diff.replaced_nodes:
        original = item.get("original") if isinstance(item, Mapping) else {}
        transformed = item.get("transformed") if isinstance(item, Mapping) else {}
        left = str((original or {}).get("operator_family") or (original or {}).get("operator_type") or "Unknown")
        right = str(
            (transformed or {}).get("operator_family") or (transformed or {}).get("operator_type") or "Unknown"
        )
        units.append(_unit("operator_delta", f"operator_replaced:{left}->{right}", f"Operator replaced: {left} -> {right}"))
    for family, delta in sorted(plan_diff.family_delta.items()):
        if int(delta) == 0:
            continue
        units.append(
            _unit(
                "operator_delta",
                f"family_delta:{family}:{delta:+d}",
                f"Operator family delta {family}: {delta:+d}",
                metadata={"delta": int(delta)},
            )
        )


def _add_access_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    access = plan_diff.access_point_delta
    if access.get("changed"):
        units.append(_unit("access_delta", "access_changed", "Access path changed"))
    if access.get("regressed"):
        units.append(_unit("access_delta", "access_regressed", "Access path regressed"))
    if _is_access_improved(access):
        units.append(_unit("access_delta", "access_improved", "Access path improved"))
    original = tuple(access.get("original") or ())
    transformed = tuple(access.get("transformed") or ())
    if original and transformed:
        left = original[0] if isinstance(original[0], Mapping) else {}
        right = transformed[0] if isinstance(transformed[0], Mapping) else {}
        left_kind = left.get("kind")
        right_kind = right.get("kind")
        if left_kind != right_kind and left_kind and right_kind:
            units.append(
                _unit(
                    "access_delta",
                    f"access_kind:{left_kind}->{right_kind}",
                    f"Access kind changed: {left_kind} -> {right_kind}",
                )
            )
        if left.get("variable") != right.get("variable") and (left.get("variable") or right.get("variable")):
            units.append(_unit("access_delta", "access_variable_changed", "Access variable changed"))
        if left.get("label") != right.get("label") and (left.get("label") or right.get("label")):
            units.append(_unit("access_delta", "access_label_changed", "Access label changed"))


def _add_expand_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    expand = plan_diff.expand_chain_delta
    delta = int(expand.get("delta") or 0)
    if delta > 0:
        units.append(_unit("expand_delta", "expand_count_increased", "Expand count increased"))
    elif delta < 0:
        units.append(_unit("expand_delta", "expand_count_decreased", "Expand count decreased"))
    original = tuple(expand.get("original") or ())
    transformed = tuple(expand.get("transformed") or ())
    if original and transformed:
        left = original[0] if isinstance(original[0], Mapping) else {}
        right = transformed[0] if isinstance(transformed[0], Mapping) else {}
        if left.get("relationship_type") != right.get("relationship_type") and (
            left.get("relationship_type") or right.get("relationship_type")
        ):
            units.append(_unit("expand_delta", "relationship_type_changed", "Relationship type changed"))
        if left.get("direction") != right.get("direction") and (left.get("direction") or right.get("direction")):
            units.append(_unit("expand_delta", "expand_direction_changed", "Expand direction changed"))


def _add_predicate_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    filter_delta = int(plan_diff.family_delta.get("Filter", 0))
    if filter_delta > 0:
        units.append(_unit("predicate_delta", "filter_added", "Filter added"))
        units.append(_unit("predicate_delta", "predicate_residue_added", "Predicate residue added"))
    elif filter_delta < 0:
        units.append(_unit("predicate_delta", "filter_removed", "Filter removed"))
    if filter_delta != 0:
        units.append(_unit("predicate_delta", "predicate_attachment_changed", "Predicate attachment changed"))
    if filter_delta > 0 and (
        plan_diff.access_point_delta.get("regressed") or int(plan_diff.barrier_delta.get("delta") or 0) > 0
    ):
        units.append(
            _unit(
                "predicate_delta",
                "possible_predicate_pushdown_blocked",
                "Possible predicate pushdown blocked",
            )
        )


def _add_barrier_units(units: list[CoverageUnit], plan_diff: PlanDiff) -> None:
    barrier = plan_diff.barrier_delta
    delta = int(barrier.get("delta") or 0)
    if delta > 0:
        units.append(_unit("barrier_delta", "barrier_count_increased", "Barrier count increased"))
    elif delta < 0:
        units.append(_unit("barrier_delta", "barrier_count_decreased", "Barrier count decreased"))
    original = {
        str(item.get("kind")).lower()
        for item in tuple(barrier.get("original") or ())
        if isinstance(item, Mapping) and item.get("kind")
    }
    transformed = {
        str(item.get("kind")).lower()
        for item in tuple(barrier.get("transformed") or ())
        if isinstance(item, Mapping) and item.get("kind")
    }
    for kind in sorted(transformed - original):
        units.append(
            _unit(
                "barrier_delta",
                f"barrier_added:{kind}",
                f"Barrier added: {kind}",
            )
        )


def _add_cardinality_units(
    units: list[CoverageUnit],
    plan_diff: PlanDiff,
    ratio_buckets: tuple[float, ...],
    metric_thresholds: Mapping[str, float] | None,
) -> None:
    estimated = plan_diff.estimated_rows_delta
    estimated_ratio = _float_or_none(estimated.get("ratio")) if isinstance(estimated, Mapping) else None
    estimated_worsened = bool((estimated or {}).get("worsened")) if isinstance(estimated, Mapping) else False
    if estimated_worsened and _passes_threshold("estimated_rows", estimated_ratio, metric_thresholds):
        bucket = ratio_bucket_label(estimated_ratio, ratio_buckets)
        if bucket is not None:
            units.append(
                _unit(
                    "cardinality_delta",
                    f"estimated_rows_inflated:{bucket}",
                    f"Estimated rows inflated: {bucket}",
                    metadata={"ratio": estimated_ratio},
                )
            )
    rows_delta = _metric_by_name(plan_diff.metric_deltas, ROW_METRIC_ALIASES)
    if rows_delta and rows_delta.worsened and _passes_threshold("rows", rows_delta.ratio, metric_thresholds):
        bucket = ratio_bucket_label(rows_delta.ratio, ratio_buckets)
        if bucket is not None:
            units.append(
                _unit(
                    "cardinality_delta",
                    f"actual_rows_inflated:{bucket}",
                    f"Actual rows inflated: {bucket}",
                    metadata={"ratio": rows_delta.ratio},
                )
            )
            units.append(
                _unit(
                    "cardinality_delta",
                    f"rows_ratio_bucket:{bucket}",
                    f"Rows ratio bucket: {bucket}",
                    metadata={"ratio": rows_delta.ratio},
                )
            )


def _add_work_units(
    units: list[CoverageUnit],
    metric_deltas: tuple[MetricDelta, ...],
    ratio_buckets: tuple[float, ...],
    metric_thresholds: Mapping[str, float] | None,
) -> None:
    for metric in metric_deltas:
        canonical = _canonical_metric(metric.metric)
        if canonical == "rows" or not metric.worsened or not _passes_threshold(canonical, metric.ratio, metric_thresholds):
            continue
        bucket = ratio_bucket_label(metric.ratio, ratio_buckets)
        if bucket is None:
            continue
        if canonical == "db_hits":
            units.append(_unit("work_delta", f"db_hits_ratio_bucket:{bucket}", f"DB hits ratio bucket: {bucket}"))
        elif canonical == "memory_bytes":
            units.append(_unit("work_delta", f"memory_ratio_bucket:{bucket}", f"Memory ratio bucket: {bucket}"))
        elif canonical in {"page_cache_hits", "page_cache_misses"}:
            units.append(
                _unit(
                    "work_delta",
                    f"page_cache_ratio_bucket:{bucket}",
                    f"Page cache ratio bucket: {bucket}",
                    metadata={"metric": metric.metric},
                )
            )
        elif canonical == "latency_ms":
            units.append(
                _unit(
                    "latency_delta",
                    f"latency_ratio_bucket:{bucket}",
                    f"Latency ratio bucket: {bucket}",
                    metadata={"metric": metric.metric},
                )
            )


def _add_context_units(
    units: list[CoverageUnit],
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
) -> None:
    units.extend(
        (
            _unit("transformation_context", f"family:{transform_family}", f"Transform family: {transform_family}", weight=0.2),
            _unit("transformation_context", f"rule:{transform_rule}", f"Transform rule: {transform_rule}", weight=0.2),
            _unit("transformation_context", f"safety_tier:{safety_tier}", f"Safety tier: {safety_tier}", weight=0.2),
        )
    )


def _dedupe_units(units: list[CoverageUnit]) -> tuple[CoverageUnit, ...]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CoverageUnit] = []
    for unit in units:
        token = (unit.kind, unit.key)
        if token in seen:
            continue
        seen.add(token)
        deduped.append(unit)
    return tuple(deduped)


def _unit(
    kind: str,
    key: str,
    label: str,
    *,
    weight: float = 1.0,
    metadata: Mapping[str, Any] | None = None,
) -> CoverageUnit:
    return CoverageUnit(kind=kind, key=key, label=label, weight=weight, metadata=dict(metadata or {}))


def _is_access_improved(access: Mapping[str, Any]) -> bool:
    original_rank = _float_or_none(access.get("original_best_rank"))
    transformed_rank = _float_or_none(access.get("transformed_best_rank"))
    return original_rank is not None and transformed_rank is not None and transformed_rank < original_rank


def _metric_by_name(metric_deltas: tuple[MetricDelta, ...], metric: str | set[str]) -> MetricDelta | None:
    wanted = {metric} if isinstance(metric, str) else metric
    for delta in metric_deltas:
        if delta.metric in wanted or _canonical_metric(delta.metric) in wanted:
            return delta
    return None


def _canonical_metric(metric: str) -> str:
    normalized = metric.strip().lower()
    if normalized in ROW_METRIC_ALIASES:
        return "rows"
    if normalized in DB_HIT_METRIC_ALIASES:
        return "db_hits"
    if normalized in LATENCY_METRIC_ALIASES:
        return "latency_ms"
    return normalized


def _passes_threshold(
    metric: str,
    ratio: float | None,
    metric_thresholds: Mapping[str, float] | None,
) -> bool:
    if ratio is None:
        return False
    threshold = 1.0
    if metric_thresholds and metric in metric_thresholds:
        threshold = float(metric_thresholds[metric])
    return ratio >= threshold


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
