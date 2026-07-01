"""Candidate root-cause classification."""

from __future__ import annotations

from typing import Any, Mapping

from graph_sym_perf.core import ConfirmationRecord, OracleRecord, PlanDiff
from graph_sym_perf.coverage import PMCEvidence


def classify_candidate(
    *,
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
    oracle: OracleRecord | None,
    confirmation: ConfirmationRecord | None,
    plan_diff: PlanDiff | None,
    pmc_evidence: PMCEvidence | None,
) -> tuple[str, ...]:
    labels: list[str] = []
    lower_rule = transform_rule.lower()
    if transform_family == "bool_redundancy":
        if "null" in lower_rule or "contradiction" in lower_rule:
            labels.append("NullContradictionSimplificationMissing")
        elif any(token in lower_rule for token in ("case", "double_negated", "not_false", "or_false", "and_true")):
            labels.append("ConstantFoldingMissing")
        else:
            labels.append("BooleanSimplificationMissing")
    elif transform_family == "symmetry":
        if "predicate_order" in lower_rule:
            labels.append("PredicateOrderSensitivity")
        elif "pattern_order" in lower_rule:
            labels.append("PatternOrderSensitivity")
        elif "label_order" in lower_rule:
            labels.append("LabelOrderSensitivity")
        elif "property_map_order" in lower_rule:
            labels.append("PropertyOrderSensitivity")
        elif "alpha_renaming" in lower_rule:
            labels.append("AlphaRenamingSensitivity")

    if plan_diff is not None:
        filter_delta = int(plan_diff.family_delta.get("Filter", 0))
        barrier_delta = int(plan_diff.barrier_delta.get("delta") or 0)
        access_regressed = bool(plan_diff.access_point_delta.get("regressed"))
        access_changed = bool(plan_diff.access_point_delta.get("changed"))
        expand_delta = int(plan_diff.expand_chain_delta.get("delta") or 0)
        estimated_only = _estimated_only_anomaly(plan_diff, confirmation)
        if transform_family == "bool_redundancy":
            if filter_delta > 0:
                labels.append("PredicateResidueIntroduced")
                if access_regressed or barrier_delta > 0:
                    labels.append("PredicatePushdownBlocked")
            if access_changed or access_regressed:
                labels.append("AccessPathRegression")
            if barrier_delta > 0:
                labels.append("JoinOrderChangedByRedundantPredicate")
            if _estimated_rows_worsened(plan_diff):
                labels.append("CardinalityEstimatePollution")
        elif transform_family == "symmetry":
            if access_changed or access_regressed:
                labels.append("AccessPointInstability")
            if expand_delta != 0:
                labels.append("ExpansionOrderInstability")
            if barrier_delta > 0:
                labels.append("BarrierInsertion")
                labels.append("JoinOrderInstability")
        if _work_inflation(plan_diff):
            labels.append("WorkInflation")
        if estimated_only:
            labels.append("EstimatedOnlyAnomaly")

    support_level = str(confirmation.support_level) if confirmation is not None else "none"
    if support_level == "profile_supported":
        labels.append("ProfileSupported")
    elif support_level == "latency_supported":
        labels.append("LatencySupported")
    elif support_level == "plan_only":
        labels.append("PlanOnly")

    if safety_tier == "exploratory":
        labels.append("ExploratoryOnly")
    if _looks_upstream(oracle, confirmation):
        labels.append("UpstreamOrBackendLimitationCandidate")
    if confirmation is not None and support_level != "plan_only" and not _work_inflation(plan_diff):
        labels.append("RuntimeOnlyOutlier")
    if pmc_evidence is not None and not pmc_evidence.units:
        labels.append("UnknownPlanAnomaly")
    if not labels:
        labels.append("UnknownPlanAnomaly")
    return _dedupe(labels)


def _estimated_only_anomaly(plan_diff: PlanDiff, confirmation: ConfirmationRecord | None) -> bool:
    if not _estimated_rows_worsened(plan_diff):
        return False
    if _work_inflation(plan_diff):
        return False
    support_level = str(confirmation.support_level) if confirmation is not None else "none"
    return support_level == "plan_only"


def _estimated_rows_worsened(plan_diff: PlanDiff) -> bool:
    payload = plan_diff.estimated_rows_delta
    if not isinstance(payload, Mapping):
        return False
    return bool(payload.get("worsened") or ((payload.get("ratio") or 0) and float(payload.get("ratio") or 0) > 1.0))


def _work_inflation(plan_diff: PlanDiff | None) -> bool:
    if plan_diff is None:
        return False
    return any(delta.worsened for delta in plan_diff.metric_deltas if delta.metric in {"db_hits", "rows", "memory_bytes"})


def _looks_upstream(oracle: OracleRecord | None, confirmation: ConfirmationRecord | None) -> bool:
    tokens = " ".join(
        value
        for value in (
            str(oracle.reason) if oracle is not None else "",
            str(confirmation.reason) if confirmation is not None else "",
            str(confirmation.developer_status) if confirmation is not None else "",
        )
        if value
    ).lower()
    return any(token in tokens for token in ("unsupported", "upstream", "backend limitation"))


def _dedupe(labels: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        result.append(label)
    return tuple(result)
