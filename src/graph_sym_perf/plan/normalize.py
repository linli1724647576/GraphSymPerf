"""Plan normalization, structural hashing, and tree differencing."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Mapping

from graph_sym_perf.core import (
    ExecutionResult,
    MetricDelta,
    NormalizedPlan,
    PlanDiff,
    PlanNode,
    PlanRecord,
    ProfileRecord,
    TargetSystem,
    stable_json_dumps,
    to_jsonable,
)


OPERATOR_PATTERNS = {
    "Filter": ("filter", "selection", "predicate"),
    "Expand": ("expand", "traverse", "relationship", "conditional traverse"),
    "Scan": ("scan", "seek", "index", "nodebylabel", "scanall", "all node"),
    "Join": ("join", "apply", "hash join"),
    "Barrier": ("eager", "materialize", "sort", "aggregate", "distinct", "cartesian"),
}

ACCESS_RANK = {
    "IndexSeek": 0,
    "IndexScan": 1,
    "DirectedRelationshipIndexSeek": 1,
    "NodeByLabelScan": 2,
    "RelationshipTypeScan": 3,
    "AllRelationshipsScan": 4,
    "AllNodesScan": 4,
    "UnknownAccess": 5,
}

PROFILE_METRICS = ("rows", "db_hits", "page_cache_hits", "page_cache_misses", "memory_bytes")


def make_plan_record(system: TargetSystem, query_kind: str, raw_plan: Any, available: bool = True) -> PlanRecord:
    raw_plan_json = to_jsonable(raw_plan)
    raw_text = _plan_text(raw_plan_json)
    normalized = normalize_plan(system, raw_plan_json)
    return PlanRecord(
        system=system,
        query_kind=query_kind,
        raw_plan=raw_plan_json,
        raw_text=raw_text,
        operator_families=tuple(node.operator_family for node in normalized.nodes),
        available=available,
        normalized_plan=normalized,
        structural_hash=normalized.structural_hash,
        operator_skeleton=normalized.operator_skeleton,
        access_points=normalized.access_points,
        predicate_attachments=normalized.predicate_attachments,
        expand_chains=normalized.expand_chains,
        barriers=normalized.barriers,
    )


def unavailable_plan(system: TargetSystem, query_kind: str, reason: str) -> PlanRecord:
    return make_plan_record(system, query_kind, {"unavailable": reason}, available=False)


def normalize_plan(system: TargetSystem, raw_plan: Any) -> NormalizedPlan:
    raw_plan = _coerce_plan_raw(raw_plan)
    root = _node_from_raw(system, raw_plan, "0")
    nodes = _flatten_nodes(root) if root is not None else ()
    structural_payload = _structural_payload(root)
    structural_hash = hashlib.sha256(stable_json_dumps(structural_payload).encode("utf-8")).hexdigest()
    return NormalizedPlan(
        root=root,
        nodes=nodes,
        structural_hash=structural_hash,
        operator_skeleton=tuple(node.operator_type for node in nodes),
        access_points=tuple(_access_point(node) for node in nodes if node.access_kind),
        predicate_attachments=tuple(_predicate_attachment(node) for node in nodes if node.predicate),
        expand_chains=tuple(_expand_entry(node) for node in nodes if node.operator_family == "Expand"),
        barriers=tuple(_barrier_entry(node) for node in nodes if node.barrier_kind),
        metrics=_aggregate_metrics(nodes),
    )


def diff_plan_records(original: PlanRecord, transformed: PlanRecord) -> PlanDiff:
    return diff_normalized_plans(original.normalized_plan, transformed.normalized_plan)


def diff_normalized_plans(original: NormalizedPlan | None, transformed: NormalizedPlan | None) -> PlanDiff:
    original_nodes = _nodes_by_path(original)
    transformed_nodes = _nodes_by_path(transformed)
    added: list[Mapping[str, Any]] = []
    removed: list[Mapping[str, Any]] = []
    replaced: list[Mapping[str, Any]] = []
    changes: list[Mapping[str, Any]] = []
    all_paths = sorted(set(original_nodes) | set(transformed_nodes), key=_path_sort_key)
    for path in all_paths:
        left = original_nodes.get(path)
        right = transformed_nodes.get(path)
        if left is None and right is not None:
            item = _node_summary(right, change="added")
            added.append(item)
            changes.append(item)
        elif right is None and left is not None:
            item = _node_summary(left, change="removed")
            removed.append(item)
            changes.append(item)
        elif left is not None and right is not None and _node_signature(left) != _node_signature(right):
            item = {
                "change": "replaced",
                "path": path,
                "original": _node_summary(left),
                "transformed": _node_summary(right),
            }
            replaced.append(item)
            changes.append(item)

    family_delta = _counter_delta(
        Counter(node.operator_family for node in _plan_nodes(original)),
        Counter(node.operator_family for node in _plan_nodes(transformed)),
    )
    access_delta = _access_delta(original, transformed)
    expand_delta = _sequence_delta(
        tuple(_expand_key(item) for item in (original.expand_chains if original else ())),
        tuple(_expand_key(item) for item in (transformed.expand_chains if transformed else ())),
        "expand_count",
    )
    barrier_delta = _sequence_delta(
        tuple(_barrier_key(item) for item in (original.barriers if original else ())),
        tuple(_barrier_key(item) for item in (transformed.barriers if transformed else ())),
        "barrier_count",
    )
    metric_deltas = tuple(
        _metric_delta(metric, _metric(original, metric), _metric(transformed, metric), ratio_threshold=1.0)
        for metric in PROFILE_METRICS
    )
    return PlanDiff(
        structural_hash_changed=(original.structural_hash if original else None)
        != (transformed.structural_hash if transformed else None),
        changes=tuple(changes),
        added_nodes=tuple(added),
        removed_nodes=tuple(removed),
        replaced_nodes=tuple(replaced),
        family_delta=family_delta,
        access_point_delta=access_delta,
        expand_chain_delta=expand_delta,
        barrier_delta=barrier_delta,
        estimated_rows_delta=_estimated_rows_delta(original, transformed),
        metric_deltas=metric_deltas,
    )


def profile_record_from_execution(result: ExecutionResult) -> ProfileRecord:
    profile_payload = result.summary.get("profile") if isinstance(result.summary, Mapping) else None
    plan = normalize_plan(result.system, profile_payload) if profile_payload else None
    metrics = plan.metrics if plan else {}
    return ProfileRecord(
        system=result.system,
        query_kind=result.query_kind,
        elapsed_ms=result.elapsed_ms,
        plan=plan,
        metrics=metrics,
        available=plan is not None,
    )


def _node_from_raw(system: TargetSystem, raw_plan: Any, path: str) -> PlanNode | None:
    if not isinstance(raw_plan, Mapping):
        return None
    operator_type = _base_operator_type(str(raw_plan.get("operator_type") or raw_plan.get("operatorType") or "Unknown"))
    arguments = raw_plan.get("arguments") or raw_plan.get("args") or {}
    if not isinstance(arguments, Mapping):
        arguments = {}
    details = str(_arg(arguments, "Details", "details", default="") or "")
    children_raw = raw_plan.get("children") or ()
    children = tuple(
        child
        for index, child_raw in enumerate(children_raw)
        if (child := _node_from_raw(system, child_raw, f"{path}.{index}")) is not None
    )
    access = _access_metadata(operator_type, details)
    expand = _expand_metadata(operator_type, details)
    return PlanNode(
        path=path,
        operator_type=operator_type,
        operator_family=_operator_family(operator_type),
        identifiers=tuple(str(item) for item in (raw_plan.get("identifiers") or ())),
        details=details,
        estimated_rows=_float_arg(arguments, "EstimatedRows", "Estimated Rows", "estimatedRows"),
        actual_rows=_float_arg(arguments, "Rows", "rows", "records"),
        db_hits=_float_arg(arguments, "DbHits", "DB Hits", "dbHits", "db_hits"),
        page_cache_hits=_float_arg(arguments, "PageCacheHits", "Page Cache Hits", "pageCacheHits", "page_cache_hits"),
        page_cache_misses=_float_arg(
            arguments,
            "PageCacheMisses",
            "Page Cache Misses",
            "pageCacheMisses",
            "page_cache_misses",
        ),
        memory_bytes=_float_arg(arguments, "Memory", "memory", "memory_bytes"),
        access_kind=access.get("kind"),
        access_variable=access.get("variable"),
        access_label=access.get("label"),
        relationship_type=expand.get("relationship_type") or access.get("relationship_type"),
        expand_direction=expand.get("direction"),
        predicate=details if _operator_family(operator_type) == "Filter" else None,
        barrier_kind=_barrier_kind(operator_type),
        children=children,
    )


def _plan_text(raw_plan: Any) -> str:
    if isinstance(raw_plan, str):
        return raw_plan
    if isinstance(raw_plan, list | tuple):
        lines = _text_lines_from_sequence(raw_plan)
        if lines:
            return "\n".join(lines)
    return stable_json_dumps(raw_plan)


def _coerce_plan_raw(raw_plan: Any) -> Any:
    if isinstance(raw_plan, Mapping):
        return raw_plan
    raw_text = _plan_text(raw_plan)
    return _parse_text_plan(raw_text) if raw_text.strip() else raw_plan


def _parse_text_plan(raw_text: str) -> Mapping[str, Any] | None:
    nodes: list[Mapping[str, Any]] = []
    for line in raw_text.splitlines():
        operator = _operator_from_text_line(line)
        if operator is None:
            continue
        rows = _actual_rows_from_text_line(line)
        if rows is None:
            rows = _first_metric_from_text_line(
                line,
                ("Rows", "Actual Rows", "Records", "Records produced", "actual number of rows"),
            )
        db_hits = _first_metric_from_text_line(
            line,
            ("DB Hits", "DbHits", "db_hits", "Actual Hits", "Actual DB Hits", "Hits"),
        )
        memory = _first_metric_from_text_line(line, ("Memory", "Memory Bytes", "Memory (Bytes)", "actual memory"))
        cache_hits = _first_metric_from_text_line(line, ("Page Cache Hits", "page_cache_hits", "shared hit", "hit"))
        cache_misses = _first_metric_from_text_line(
            line,
            ("Page Cache Misses", "page_cache_misses", "shared read", "read"),
        )
        arguments: dict[str, Any] = {"Details": _clean_plan_line(line)}
        if rows is not None:
            arguments["Rows"] = rows
        if db_hits is not None:
            arguments["DbHits"] = db_hits
        if memory is not None:
            arguments["Memory"] = memory
        if cache_hits is not None:
            arguments["PageCacheHits"] = cache_hits
        if cache_misses is not None:
            arguments["PageCacheMisses"] = cache_misses
        nodes.append({"operator_type": operator, "arguments": arguments, "children": []})
    if not nodes:
        return None
    root: dict[str, Any] | None = None
    for node in reversed(nodes):
        current = dict(node)
        current["children"] = (root,) if root is not None else ()
        root = current
    return root


def _operator_from_text_line(line: str) -> str | None:
    cleaned = _clean_plan_line(line)
    if not cleaned:
        return None
    if cleaned.startswith(("Plan:", "QUERY PLAN")):
        return None
    candidates = (
        "NodeIndexSeek",
        "NodeIndexScan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Index Scan",
        "Bitmap Heap Scan",
        "DirectedRelationshipIndexSeek",
        "NodeByLabelScan",
        "Node By Label Scan",
        "IntersectionNodeByLabelsScan",
        "RelationshipTypeScan",
        "AllRelationshipsScan",
        "AllNodesScan",
        "ScanAll",
        "All Node Scan",
        "Seq Scan",
        "Function Scan",
        "Subquery Scan",
        "CTE Scan",
        "Filter",
        "Selection",
        "Expand",
        "Conditional Traverse",
        "Traverse",
        "Nested Loop",
        "Merge Join",
        "Hash Join",
        "Apply",
        "Join",
        "Sort",
        "Aggregate",
        "Aggregation",
        "Distinct",
        "ProduceResults",
        "Produce",
        "Project",
        "Projection",
        "Results",
        "Buffers",
    )
    lowered = cleaned.lower()
    for candidate in candidates:
        if candidate.lower() in lowered:
            return candidate.replace(" ", "")
    return None


def _clean_plan_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^[|+\\\-\s>`*]+", "", cleaned)
    cleaned = re.sub(r"[|+\\\-\s]+$", "", cleaned)
    return cleaned.strip()


def _first_metric_from_text_line(line: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        value = _metric_from_text_line(line, label)
        if value is not None:
            return value
    return None


def _metric_from_text_line(line: str, label: str) -> float | None:
    match = re.search(
        rf"{re.escape(label)}\s*[:=]\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _actual_rows_from_text_line(line: str) -> float | None:
    match = re.search(
        r"\(actual\s+[^)]*rows=([0-9][0-9,]*(?:\.[0-9]+)?)",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _text_lines_from_sequence(raw_plan: list[Any] | tuple[Any, ...]) -> list[str]:
    lines: list[str] = []
    for item in raw_plan:
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, Mapping):
            structured_line = _structured_text_line_from_mapping(item)
            if structured_line:
                lines.append(structured_line)
                continue
            for value in item.values():
                if isinstance(value, str):
                    lines.append(value)
                elif isinstance(value, list | tuple) and all(isinstance(entry, str) for entry in value):
                    lines.extend(value)
        elif isinstance(item, list | tuple):
            for value in item:
                if isinstance(value, str):
                    lines.append(value)
    return lines


def _structured_text_line_from_mapping(item: Mapping[str, Any]) -> str | None:
    lowered = {str(key).lower().replace(" ", "_"): key for key in item}
    operator_key = lowered.get("operator") or lowered.get("operator_type") or lowered.get("operatortype")
    if operator_key is None:
        return None
    operator = item.get(operator_key)
    if not isinstance(operator, str) or not operator.strip():
        return None
    parts = [operator]
    for output_label, keys in (
        ("Rows", ("rows", "actual_rows", "records", "records_produced")),
        ("Actual Hits", ("actual_hits", "db_hits", "dbhits")),
        ("Memory", ("memory", "memory_bytes", "actual_memory")),
    ):
        for key in keys:
            original_key = lowered.get(key)
            if original_key is None:
                continue
            value = item.get(original_key)
            if value not in (None, ""):
                parts.append(f"{output_label}: {value}")
            break
    return " | ".join(parts)


def _operator_family(operator_type: str) -> str:
    lowered = operator_type.lower()
    if "filter" in lowered or "selection" in lowered:
        return "Filter"
    if "expand" in lowered or "traverse" in lowered:
        return "Expand"
    if "scan" in lowered or "seek" in lowered or "index" in lowered:
        return "Scan"
    if any(token in lowered for token in ("eager", "materialize", "sort", "aggregate", "distinct", "apply", "join")):
        return "Barrier"
    if any(token in lowered for token in ("projection", "project", "produce", "results", "unwind", "limit")):
        return "Projection"
    return "Other"


def _operator_families(raw_text: str) -> tuple[str, ...]:
    lowered = raw_text.lower()
    families: list[str] = []
    for family, patterns in OPERATOR_PATTERNS.items():
        count = 0
        for pattern in patterns:
            count += lowered.count(pattern)
        families.extend(family for _ in range(count))
    return tuple(families)


def _base_operator_type(operator_type: str) -> str:
    return operator_type.split("@", 1)[0].strip() or "Unknown"


def _flatten_nodes(root: PlanNode) -> tuple[PlanNode, ...]:
    nodes = [root]
    for child in root.children:
        nodes.extend(_flatten_nodes(child))
    return tuple(nodes)


def _structural_payload(node: PlanNode | None) -> Any:
    if node is None:
        return None
    return {
        "operator_family": node.operator_family,
        "operator_type": node.operator_type,
        "access_kind": node.access_kind,
        "relationship_type": node.relationship_type,
        "expand_direction": node.expand_direction,
        "barrier_kind": node.barrier_kind,
        "children": [_structural_payload(child) for child in node.children],
    }


def _node_signature(node: PlanNode) -> Mapping[str, Any]:
    return {
        "operator_family": node.operator_family,
        "operator_type": node.operator_type,
        "access_kind": node.access_kind,
        "relationship_type": node.relationship_type,
        "expand_direction": node.expand_direction,
        "barrier_kind": node.barrier_kind,
    }


def _node_summary(node: PlanNode, *, change: str | None = None) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "path": node.path,
        "operator_type": node.operator_type,
        "operator_family": node.operator_family,
        "details": node.details,
    }
    if change:
        payload["change"] = change
    return payload


def _access_metadata(operator_type: str, details: str) -> dict[str, str | None]:
    if not _is_access_operator(operator_type):
        return {"kind": None, "variable": None, "label": None, "relationship_type": None}
    kind = "UnknownAccess"
    lowered = operator_type.lower()
    if "nodeindexseek" in lowered or "indexseek" in lowered:
        kind = "IndexSeek"
    elif "indexscan" in lowered:
        kind = "IndexScan"
    elif "relationshipindexseek" in lowered:
        kind = "DirectedRelationshipIndexSeek"
    elif "nodebylabelscan" in lowered or "nodebylabelsscan" in lowered:
        kind = "NodeByLabelScan"
    elif "relationshiptypescan" in lowered:
        kind = "RelationshipTypeScan"
    elif "allrelationshipsscan" in lowered:
        kind = "AllRelationshipsScan"
    elif (
        "allnodesscan" in lowered
        or "scanall" in lowered
        or "allnodescan" in lowered
        or "allnodes" in lowered
        or "seqscan" in lowered
        or "functionscan" in lowered
    ):
        kind = "AllNodesScan"
    variable, label = _parse_variable_label(details)
    relationship_type = _parse_relationship_type(details)
    return {"kind": kind, "variable": variable, "label": label, "relationship_type": relationship_type}


def _expand_metadata(operator_type: str, details: str) -> dict[str, str | None]:
    if "expand" not in operator_type.lower():
        return {"direction": None, "relationship_type": None}
    direction = None
    if "->" in details:
        direction = "out"
    elif "<-" in details:
        direction = "in"
    relationship_type = _parse_relationship_type(details)
    return {"direction": direction, "relationship_type": relationship_type}


def _is_access_operator(operator_type: str) -> bool:
    lowered = operator_type.lower()
    return any(token in lowered for token in ("scan", "seek", "index"))


def _parse_variable_label(details: str) -> tuple[str | None, str | None]:
    node_match = re.search(r"\(?([A-Za-z]\w*)\s*:\s*([A-Za-z]\w*)", details)
    if node_match:
        return node_match.group(1), node_match.group(2)
    variable_match = re.search(r"\(?([A-Za-z]\w*)\)?", details)
    return (variable_match.group(1), None) if variable_match else (None, None)


def _parse_relationship_type(details: str) -> str | None:
    match = re.search(r"\[[A-Za-z]\w*\s*:\s*([A-Za-z]\w*)", details)
    return match.group(1) if match else None


def _barrier_kind(operator_type: str) -> str | None:
    lowered = operator_type.lower()
    for token in ("eager", "apply", "join", "sort", "aggregation", "aggregate", "distinct", "materialize"):
        if token in lowered:
            return token
    return None


def _access_point(node: PlanNode) -> Mapping[str, Any]:
    return {
        "path": node.path,
        "operator_type": node.operator_type,
        "kind": node.access_kind,
        "rank": ACCESS_RANK.get(node.access_kind or "UnknownAccess", ACCESS_RANK["UnknownAccess"]),
        "variable": node.access_variable,
        "label": node.access_label,
        "relationship_type": node.relationship_type,
        "details": node.details,
    }


def _predicate_attachment(node: PlanNode) -> Mapping[str, Any]:
    return {"path": node.path, "operator_type": node.operator_type, "predicate": node.predicate}


def _expand_entry(node: PlanNode) -> Mapping[str, Any]:
    return {
        "path": node.path,
        "operator_type": node.operator_type,
        "direction": node.expand_direction,
        "relationship_type": node.relationship_type,
        "details": node.details,
    }


def _barrier_entry(node: PlanNode) -> Mapping[str, Any]:
    return {"path": node.path, "operator_type": node.operator_type, "kind": node.barrier_kind, "details": node.details}


def _aggregate_metrics(nodes: tuple[PlanNode, ...]) -> Mapping[str, float]:
    totals: dict[str, float] = {}
    for metric, attr in (
        ("estimated_rows", "estimated_rows"),
        ("rows", "actual_rows"),
        ("db_hits", "db_hits"),
        ("page_cache_hits", "page_cache_hits"),
        ("page_cache_misses", "page_cache_misses"),
        ("memory_bytes", "memory_bytes"),
    ):
        values = [getattr(node, attr) for node in nodes if getattr(node, attr) is not None]
        if values:
            totals[metric] = float(sum(values))
    return totals


def _nodes_by_path(plan: NormalizedPlan | None) -> Mapping[str, PlanNode]:
    return {node.path: node for node in (plan.nodes if plan else ())}


def _plan_nodes(plan: NormalizedPlan | None) -> tuple[PlanNode, ...]:
    return plan.nodes if plan else ()


def _counter_delta(original: Counter[str], transformed: Counter[str]) -> Mapping[str, int]:
    keys = sorted(set(original) | set(transformed))
    return {key: transformed.get(key, 0) - original.get(key, 0) for key in keys}


def _access_delta(original: NormalizedPlan | None, transformed: NormalizedPlan | None) -> Mapping[str, Any]:
    original_points = tuple(_access_key(item) for item in (original.access_points if original else ()))
    transformed_points = tuple(_access_key(item) for item in (transformed.access_points if transformed else ()))
    original_best = min((_access_rank(item) for item in (original.access_points if original else ())), default=None)
    transformed_best = min((_access_rank(item) for item in (transformed.access_points if transformed else ())), default=None)
    return {
        "original": original_points,
        "transformed": transformed_points,
        "changed": original_points != transformed_points,
        "original_best_rank": original_best,
        "transformed_best_rank": transformed_best,
        "regressed": original_best is not None and transformed_best is not None and transformed_best > original_best,
    }


def _sequence_delta(original: tuple[Any, ...], transformed: tuple[Any, ...], label: str) -> Mapping[str, Any]:
    return {
        "original": original,
        "transformed": transformed,
        "changed": original != transformed,
        "original_count": len(original),
        "transformed_count": len(transformed),
        "delta": len(transformed) - len(original),
        label: {"original": len(original), "transformed": len(transformed)},
    }


def _estimated_rows_delta(original: NormalizedPlan | None, transformed: NormalizedPlan | None) -> Mapping[str, Any]:
    original_value = _metric(original, "estimated_rows")
    transformed_value = _metric(transformed, "estimated_rows")
    delta = _metric_delta("estimated_rows", original_value, transformed_value, ratio_threshold=1.0)
    return to_jsonable(delta)


def _metric_delta(metric: str, original: float | None, transformed: float | None, *, ratio_threshold: float) -> MetricDelta:
    if original is None or transformed is None:
        return MetricDelta(metric, original, transformed, None, None, False)
    ratio = transformed / max(original, 0.001)
    delta = transformed - original
    return MetricDelta(metric, original, transformed, ratio, delta, transformed > original and ratio >= ratio_threshold)


def _metric(plan: NormalizedPlan | None, metric: str) -> float | None:
    if not plan:
        return None
    value = plan.metrics.get(metric)
    return float(value) if value is not None else None


def _access_key(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "kind": item.get("kind"),
        "label": item.get("label"),
        "relationship_type": item.get("relationship_type"),
        "operator_type": item.get("operator_type"),
    }


def _access_rank(item: Mapping[str, Any]) -> int:
    rank = item.get("rank")
    return int(rank) if rank is not None else ACCESS_RANK["UnknownAccess"]


def _expand_key(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "operator_type": item.get("operator_type"),
        "direction": item.get("direction"),
        "relationship_type": item.get("relationship_type"),
    }


def _barrier_key(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"operator_type": item.get("operator_type"), "kind": item.get("kind")}


def _path_sort_key(path: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in path.split("."))
    except ValueError:
        return (10**9,)


def _arg(arguments: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in arguments:
            return arguments[name]
    lowered = {str(key).lower().replace(" ", "").replace("_", ""): value for key, value in arguments.items()}
    for name in names:
        key = name.lower().replace(" ", "").replace("_", "")
        if key in lowered:
            return lowered[key]
    return default


def _float_arg(arguments: Mapping[str, Any], *names: str) -> float | None:
    value = _arg(arguments, *names)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
