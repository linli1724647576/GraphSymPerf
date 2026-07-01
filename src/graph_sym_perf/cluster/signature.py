"""Stable candidate signatures for clustering and deduplication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from graph_sym_perf.core import (
    CaseRecord,
    ClaimLevel,
    ConfirmationRecord,
    MetricDelta,
    OracleRecord,
    PlanDiff,
)
from graph_sym_perf.coverage import PMCEvidence, ratio_bucket_label
from graph_sym_perf.cluster.classify import classify_candidate

RATIO_BUCKETS: tuple[float, ...] = (1.5, 2.0, 5.0, 10.0, 50.0, 100.0, 1000.0)


@dataclass(frozen=True)
class CandidateSignature:
    case_id: str
    system: str
    transform_family: str
    transform_rule: str
    safety_tier: str
    candidate_types: tuple[str, ...]
    root_cause_labels: tuple[str, ...]
    support_level: str
    claim_level: str
    plan_signature: str
    metric_signature: str
    pmc_signature: str
    issue_family_key: str
    unique_issue_key: str
    duplicate_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


def build_candidate_signature(
    *,
    record: CaseRecord,
    transform_family: str,
    transform_rule: str,
    safety_tier: str,
    oracle: OracleRecord | None,
    confirmation: ConfirmationRecord | None,
    plan_diff: PlanDiff | None,
    pmc_evidence: PMCEvidence | None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> CandidateSignature:
    candidate_types = tuple((oracle.candidate_types if oracle is not None else ()) or ())
    root_cause_labels = classify_candidate(
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        oracle=oracle,
        confirmation=confirmation,
        plan_diff=plan_diff,
        pmc_evidence=pmc_evidence,
    )
    support_level = str(confirmation.support_level) if confirmation is not None else "none"
    claim_level = _claim_level(record, confirmation)
    plan_signature, main_plan_delta = _plan_signature(plan_diff)
    metric_signature = _metric_signature(confirmation)
    pmc_signature = _pmc_signature(pmc_evidence)
    primary_root_cause = root_cause_labels[0] if root_cause_labels else "UnknownPlanAnomaly"
    primary_candidate_type = candidate_types[0] if candidate_types else "unknown"
    issue_family_key = "|".join(
        (
            record.system.value,
            transform_family,
            primary_root_cause,
            primary_candidate_type,
            main_plan_delta,
        )
    )
    unique_issue_key = "|".join(
        (
            record.system.value,
            transform_family,
            transform_rule,
            ",".join(root_cause_labels),
            plan_signature,
            metric_signature,
        )
    )
    duplicate_key = "|".join((record.system.value, transform_rule, plan_signature, pmc_signature))
    metadata = {
        "primary_root_cause": primary_root_cause,
        "primary_candidate_type": primary_candidate_type,
        "main_plan_delta": main_plan_delta,
        "latency_ratio": confirmation.latency_ratio if confirmation is not None else None,
        "latency_delta_ms": confirmation.latency_delta_ms if confirmation is not None else None,
        "pmc_new_units": pmc_evidence.new_unit_count if pmc_evidence is not None else 0,
        "max_work_ratio": _max_work_ratio(confirmation),
        "developer_status": str(confirmation.developer_status) if confirmation is not None else str(record.developer_status),
        "db_hits_ratio": _metric_ratio(confirmation, "db_hits"),
        "rows_ratio": _metric_ratio(confirmation, "rows"),
        "memory_ratio": _metric_ratio(confirmation, "memory_bytes"),
    }
    if extra_metadata:
        metadata.update(dict(extra_metadata))
    return CandidateSignature(
        case_id=record.case_id,
        system=record.system.value,
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        candidate_types=candidate_types,
        root_cause_labels=root_cause_labels,
        support_level=support_level,
        claim_level=claim_level,
        plan_signature=plan_signature,
        metric_signature=metric_signature,
        pmc_signature=pmc_signature,
        issue_family_key=issue_family_key,
        unique_issue_key=unique_issue_key,
        duplicate_key=duplicate_key,
        metadata=metadata,
    )


def _claim_level(record: CaseRecord, confirmation: ConfirmationRecord | None) -> str:
    if confirmation is not None and confirmation.claim_level:
        return str(confirmation.claim_level)
    return str(record.claim_level or ClaimLevel.CANDIDATE)


def _plan_signature(plan_diff: PlanDiff | None) -> tuple[str, str]:
    if plan_diff is None:
        return ("plan:none", "none")
    family_delta = ",".join(
        f"{key}:{value:+d}" for key, value in sorted(plan_diff.family_delta.items()) if int(value) != 0
    ) or "family:none"
    access = plan_diff.access_point_delta
    access_part = "access:none"
    if access:
        access_part = "access:" + ":".join(
            str(part)
            for part in (
                "changed" if access.get("changed") else "same",
                "regressed" if access.get("regressed") else "stable",
                _access_kind(access.get("original")),
                _access_kind(access.get("transformed")),
            )
        )
    expand = plan_diff.expand_chain_delta
    expand_part = f"expand:{int(expand.get('delta') or 0):+d}"
    barrier = plan_diff.barrier_delta
    barrier_part = f"barrier:{int(barrier.get('delta') or 0):+d}"
    operators = ",".join(sorted(_operator_summary(plan_diff))) or "ops:none"
    signature = "|".join(
        (
            "structural:changed" if plan_diff.structural_hash_changed else "structural:same",
            family_delta,
            access_part,
            expand_part,
            barrier_part,
            operators,
        )
    )
    return signature, _main_plan_delta(plan_diff)


def _metric_signature(confirmation: ConfirmationRecord | None) -> str:
    if confirmation is None:
        return "metrics:none"
    deltas = {delta.metric: delta for delta in confirmation.metric_deltas}
    parts = [
        f"db_hits:{_metric_bucket(deltas.get('db_hits'))}",
        f"rows:{_metric_bucket(deltas.get('rows'))}",
        f"memory:{_metric_bucket(deltas.get('memory_bytes'))}",
        f"latency:{_ratio_bucket(confirmation.latency_ratio)}",
    ]
    return "|".join(parts)


def _pmc_signature(pmc_evidence: PMCEvidence | None) -> str:
    if pmc_evidence is None or not pmc_evidence.units:
        return "pmc:none"
    keys = sorted(unit.key for unit in pmc_evidence.units if unit.kind != "transformation_context")[:8]
    payload = "|".join(keys)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"pmc:{digest}"


def _max_work_ratio(confirmation: ConfirmationRecord | None) -> float | None:
    if confirmation is None:
        return None
    ratios = [float(delta.ratio) for delta in confirmation.metric_deltas if delta.ratio is not None]
    if confirmation.latency_ratio is not None:
        ratios.append(float(confirmation.latency_ratio))
    return max(ratios) if ratios else None


def _metric_ratio(confirmation: ConfirmationRecord | None, metric: str) -> float | None:
    if confirmation is None:
        return None
    for delta in confirmation.metric_deltas:
        if delta.metric == metric:
            return delta.ratio
    return None


def _metric_bucket(delta: MetricDelta | None) -> str:
    if delta is None or delta.ratio is None:
        return "none"
    return _ratio_bucket(delta.ratio)


def _ratio_bucket(ratio: float | None) -> str:
    if ratio is None:
        return "none"
    if ratio < 1.5:
        return "same"
    return ratio_bucket_label(ratio, RATIO_BUCKETS) or "same"


def _access_kind(items: Any) -> str:
    entries = tuple(items or ())
    if not entries:
        return "none"
    first = entries[0] if isinstance(entries[0], Mapping) else {}
    return str(first.get("kind") or "none")


def _operator_summary(plan_diff: PlanDiff) -> tuple[str, ...]:
    items: list[str] = []
    for node in plan_diff.added_nodes:
        items.append(f"add:{node.get('operator_family') or node.get('operator_type')}")
    for node in plan_diff.removed_nodes:
        items.append(f"remove:{node.get('operator_family') or node.get('operator_type')}")
    for node in plan_diff.replaced_nodes:
        original = node.get("original") if isinstance(node, Mapping) else {}
        transformed = node.get("transformed") if isinstance(node, Mapping) else {}
        items.append(
            f"replace:{(original or {}).get('operator_family') or (original or {}).get('operator_type')}->{(transformed or {}).get('operator_family') or (transformed or {}).get('operator_type')}"
        )
    return tuple(items)


def _main_plan_delta(plan_diff: PlanDiff) -> str:
    if plan_diff.access_point_delta.get("changed"):
        return "access_delta"
    if int(plan_diff.family_delta.get("Filter", 0)) != 0:
        return "predicate_delta"
    if int(plan_diff.barrier_delta.get("delta") or 0) != 0:
        return "barrier_delta"
    if int(plan_diff.expand_chain_delta.get("delta") or 0) != 0:
        return "expand_delta"
    if any(int(value) != 0 for value in plan_diff.family_delta.values()):
        return "operator_delta"
    if any(delta.worsened for delta in plan_diff.metric_deltas):
        return "work_delta"
    if plan_diff.structural_hash_changed:
        return "structural"
    return "none"
