"""Validated query and setup-graph reduction for triaged candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.confirm.runtime import evaluate_confirmation
from graph_sym_perf.core import ConfirmationConfig, ConfirmationRecord, ProfileRecord, to_jsonable
from graph_sym_perf.equivalence import compare_results
from graph_sym_perf.plan import profile_record_from_execution

GraphReload = Callable[[tuple[str, ...]], None]


def reduce_candidate(
    *,
    adapter,
    case_id: str,
    case_dir: str | Path,
    fast_query: str,
    slow_query: str,
    parameters: Mapping[str, Any],
    confirmation_config: ConfirmationConfig,
    max_result_rows: int,
    timeout_seconds: float,
    runtime_repeats: int,
    preserve_metric_ratio: float = 1.5,
    max_query_attempts: int = 32,
    setup_statements: tuple[str, ...] = (),
    reload_graph: GraphReload | None = None,
    reduce_graph: bool = False,
) -> Mapping[str, Any]:
    """Try to shrink a candidate while preserving the observable symptom."""

    output_dir = Path(case_dir) / "reduced"
    output_dir.mkdir(parents=True, exist_ok=True)
    config = replace(confirmation_config, repeats=max(1, int(runtime_repeats)), timeout_seconds=timeout_seconds)
    baseline = _evaluate_pair(
        adapter,
        case_id=f"{case_id}:baseline",
        fast_query=fast_query,
        slow_query=slow_query,
        parameters=parameters,
        config=config,
        max_result_rows=max_result_rows,
    )
    target_ratio = max(float(preserve_metric_ratio), min(_symptom_ratio(baseline["confirmation"]) or 0.0, float(preserve_metric_ratio)))
    fast_current = _normalize_query_text(fast_query)
    slow_current = _normalize_query_text(slow_query)
    query_steps: list[Mapping[str, Any]] = []
    if _preserves_symptom(baseline, target_ratio):
        fast_current, slow_current = _try_query_reductions(
            adapter=adapter,
            case_id=case_id,
            fast_query=fast_current,
            slow_query=slow_current,
            parameters=parameters,
            config=config,
            max_result_rows=max_result_rows,
            target_ratio=target_ratio,
            max_attempts=max_query_attempts,
            steps=query_steps,
        )
    else:
        query_steps.append(
            {
                "name": "baseline",
                "accepted": False,
                "reason": "baseline_did_not_preserve_equivalence_or_symptom",
                "equivalence_passed": baseline["equivalence_passed"],
                "symptom_ratio": _symptom_ratio(baseline["confirmation"]),
            }
        )

    final_eval = _evaluate_pair(
        adapter,
        case_id=f"{case_id}:reduced_query",
        fast_query=fast_current,
        slow_query=slow_current,
        parameters=parameters,
        config=config,
        max_result_rows=max_result_rows,
    )
    graph_summary = _reduce_graph_statements(
        adapter=adapter,
        case_id=case_id,
        current_fast=fast_current,
        current_slow=slow_current,
        parameters=parameters,
        config=config,
        max_result_rows=max_result_rows,
        target_ratio=target_ratio,
        setup_statements=setup_statements,
        reload_graph=reload_graph,
        reduce_graph=reduce_graph,
        output_dir=output_dir,
    )
    fast_path = output_dir / "fast_query.cypher"
    slow_path = output_dir / "slow_query.cypher"
    fast_path.write_text(fast_current.rstrip() + "\n", encoding="utf-8")
    slow_path.write_text(slow_current.rstrip() + "\n", encoding="utf-8")
    summary = {
        "mode": "validated_reducer",
        "case_id": case_id,
        "target_symptom_ratio": target_ratio,
        "baseline": _evaluation_payload(baseline),
        "final": _evaluation_payload(final_eval),
        "query_reduction": {
            "attempts": len(query_steps),
            "accepted": sum(1 for item in query_steps if item.get("accepted")),
            "steps": query_steps,
            "original_fast_length": len(fast_query),
            "original_slow_length": len(slow_query),
            "reduced_fast_length": len(fast_current),
            "reduced_slow_length": len(slow_current),
            "fast_query_path": str(fast_path),
            "slow_query_path": str(slow_path),
        },
        "graph_reduction": graph_summary,
        "artifact_dir": str(output_dir),
    }
    (output_dir / "reduction_summary.json").write_text(
        _json_dumps(summary),
        encoding="utf-8",
    )
    return summary


def _try_query_reductions(
    *,
    adapter,
    case_id: str,
    fast_query: str,
    slow_query: str,
    parameters: Mapping[str, Any],
    config: ConfirmationConfig,
    max_result_rows: int,
    target_ratio: float,
    max_attempts: int,
    steps: list[Mapping[str, Any]],
) -> tuple[str, str]:
    current_fast = fast_query
    current_slow = slow_query
    attempts = 0
    while attempts < max_attempts:
        accepted_this_round = False
        for proposal in _query_proposals(current_fast, current_slow):
            if attempts >= max_attempts:
                break
            attempts += 1
            fast_candidate = proposal["fast"]
            slow_candidate = proposal["slow"]
            if len(fast_candidate) + len(slow_candidate) >= len(current_fast) + len(current_slow):
                continue
            evaluation = _evaluate_pair(
                adapter,
                case_id=f"{case_id}:query_reduce:{attempts}",
                fast_query=fast_candidate,
                slow_query=slow_candidate,
                parameters=parameters,
                config=config,
                max_result_rows=max_result_rows,
            )
            accepted = _preserves_symptom(evaluation, target_ratio)
            steps.append(
                {
                    "name": proposal["name"],
                    "accepted": accepted,
                    "equivalence_passed": evaluation["equivalence_passed"],
                    "symptom_ratio": _symptom_ratio(evaluation["confirmation"]),
                    "fast_length": len(fast_candidate),
                    "slow_length": len(slow_candidate),
                }
            )
            if accepted:
                current_fast = fast_candidate
                current_slow = slow_candidate
                accepted_this_round = True
                break
        if not accepted_this_round:
            break
    return current_fast, current_slow


def _query_proposals(fast_query: str, slow_query: str) -> tuple[Mapping[str, Any], ...]:
    proposals: list[Mapping[str, Any]] = []
    normalized_fast = _normalize_query_text(fast_query)
    normalized_slow = _normalize_query_text(slow_query)
    if (normalized_fast, normalized_slow) != (fast_query, slow_query):
        proposals.append({"name": "normalize_whitespace", "fast": normalized_fast, "slow": normalized_slow})
    return_fast = _replace_final_return_with_count(normalized_fast)
    return_slow = _replace_final_return_with_count(normalized_slow)
    if return_fast and return_slow:
        proposals.append({"name": "replace_final_return_with_count", "fast": return_fast, "slow": return_slow})
    no_order_fast = _remove_order_by(normalized_fast)
    no_order_slow = _remove_order_by(normalized_slow)
    if no_order_fast != normalized_fast or no_order_slow != normalized_slow:
        proposals.append({"name": "remove_order_by", "fast": no_order_fast, "slow": no_order_slow})
    proposals.extend(_drop_clause_proposals(normalized_fast, normalized_slow))
    proposals.extend(_drop_where_conjunct_proposals(normalized_fast, normalized_slow))
    return tuple(proposals)


def _evaluate_pair(
    adapter,
    *,
    case_id: str,
    fast_query: str,
    slow_query: str,
    parameters: Mapping[str, Any],
    config: ConfirmationConfig,
    max_result_rows: int,
) -> Mapping[str, Any]:
    try:
        fast_rows = adapter.execute(
            fast_query,
            parameters,
            f"{case_id}:fast_execute",
            max_rows=max_result_rows,
            timeout_seconds=config.timeout_seconds,
        ).rows
        slow_rows = adapter.execute(
            slow_query,
            parameters,
            f"{case_id}:slow_execute",
            max_rows=max_result_rows,
            timeout_seconds=config.timeout_seconds,
        ).rows
        equivalence = compare_results(case_id, fast_rows, slow_rows, max_result_rows)
        if not equivalence.passed:
            return {"equivalence_passed": False, "equivalence": equivalence, "confirmation": None, "error": equivalence.reason}
        fast_samples: list[float] = []
        slow_samples: list[float] = []
        fast_profiles: list[ProfileRecord] = []
        slow_profiles: list[ProfileRecord] = []
        for repeat in range(max(1, config.repeats)):
            fast_result = adapter.profile(
                fast_query,
                parameters,
                f"{case_id}:fast_profile:{repeat}",
                max_rows=max_result_rows,
                timeout_seconds=config.timeout_seconds,
            )
            slow_result = adapter.profile(
                slow_query,
                parameters,
                f"{case_id}:slow_profile:{repeat}",
                max_rows=max_result_rows,
                timeout_seconds=config.timeout_seconds,
            )
            fast_samples.append(float(fast_result.elapsed_ms))
            slow_samples.append(float(slow_result.elapsed_ms))
            fast_profiles.append(profile_record_from_execution(fast_result))
            slow_profiles.append(profile_record_from_execution(slow_result))
        confirmation = evaluate_confirmation(
            case_id,
            tuple(fast_samples),
            tuple(slow_samples),
            config,
            tuple(fast_profiles),
            tuple(slow_profiles),
        )
        return {"equivalence_passed": True, "equivalence": equivalence, "confirmation": confirmation, "error": None}
    except Exception as exc:
        return {"equivalence_passed": False, "equivalence": None, "confirmation": None, "error": str(exc)}


def _preserves_symptom(evaluation: Mapping[str, Any], target_ratio: float) -> bool:
    if not evaluation.get("equivalence_passed"):
        return False
    confirmation = evaluation.get("confirmation")
    return (_symptom_ratio(confirmation) or 0.0) >= target_ratio


def _symptom_ratio(confirmation: ConfirmationRecord | None) -> float | None:
    if confirmation is None:
        return None
    ratios = [float(delta.ratio) for delta in confirmation.metric_deltas if delta.ratio is not None and delta.ratio >= 1.0]
    if confirmation.latency_ratio is not None and confirmation.latency_ratio >= 1.0:
        ratios.append(float(confirmation.latency_ratio))
    return max(ratios) if ratios else None


def _evaluation_payload(evaluation: Mapping[str, Any]) -> Mapping[str, Any]:
    confirmation = evaluation.get("confirmation")
    equivalence = evaluation.get("equivalence")
    return {
        "equivalence_passed": bool(evaluation.get("equivalence_passed")),
        "equivalence": to_jsonable(equivalence),
        "confirmation": to_jsonable(confirmation),
        "symptom_ratio": _symptom_ratio(confirmation),
        "error": evaluation.get("error"),
    }


def _reduce_graph_statements(
    *,
    adapter,
    case_id: str,
    current_fast: str,
    current_slow: str,
    parameters: Mapping[str, Any],
    config: ConfirmationConfig,
    max_result_rows: int,
    target_ratio: float,
    setup_statements: tuple[str, ...],
    reload_graph: GraphReload | None,
    reduce_graph: bool,
    output_dir: Path,
) -> Mapping[str, Any]:
    if not reduce_graph:
        return {"status": "not_requested"}
    if not setup_statements:
        return {"status": "unavailable_no_setup_artifact"}
    if reload_graph is None:
        return {"status": "unavailable_no_graph_reload_callback"}
    current = tuple(statement for statement in setup_statements if statement.strip())
    steps: list[Mapping[str, Any]] = []
    index = 0
    while index < len(current):
        if len(current) <= 1:
            break
        trial = current[:index] + current[index + 1 :]
        try:
            reload_graph(trial)
            evaluation = _evaluate_pair(
                adapter,
                case_id=f"{case_id}:graph_reduce:{index}",
                fast_query=current_fast,
                slow_query=current_slow,
                parameters=parameters,
                config=config,
                max_result_rows=max_result_rows,
            )
        except Exception as exc:
            steps.append({"removed_index": index, "accepted": False, "error": str(exc)})
            continue
        accepted = _preserves_symptom(evaluation, target_ratio)
        steps.append(
            {
                "removed_index": index,
                "accepted": accepted,
                "equivalence_passed": evaluation.get("equivalence_passed"),
                "symptom_ratio": _symptom_ratio(evaluation.get("confirmation")),
            }
        )
        if accepted:
            current = trial
            continue
        index += 1
    reload_graph(current)
    graph_path = output_dir / "reduced_graph.cypher"
    graph_path.write_text(";\n".join(statement.rstrip(";") for statement in current) + ";\n", encoding="utf-8")
    return {
        "status": "reduced" if len(current) < len(setup_statements) else "preserved_original_graph",
        "original_statement_count": len(setup_statements),
        "reduced_statement_count": len(current),
        "accepted_steps": sum(1 for item in steps if item.get("accepted")),
        "steps": steps,
        "graph_path": str(graph_path),
    }


def _normalize_query_text(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().rstrip(";")).strip()


def _replace_final_return_with_count(query: str) -> str | None:
    match = list(re.finditer(r"\bRETURN\b", query, flags=re.IGNORECASE))
    if not match:
        return None
    start = match[-1].start()
    return query[:start].rstrip() + " RETURN count(*) AS graphsymperf_reduced_count"


def _remove_order_by(query: str) -> str:
    return re.sub(
        r"\s+ORDER\s+BY\s+.*?(?=\s+(WHERE|WITH|MATCH|OPTIONAL\s+MATCH|RETURN|LIMIT|SKIP)\b|$)",
        " ",
        query,
        flags=re.IGNORECASE,
    ).strip()


def _drop_clause_proposals(fast_query: str, slow_query: str) -> tuple[Mapping[str, Any], ...]:
    fast_clauses = _clauses(fast_query)
    slow_clauses = _clauses(slow_query)
    proposals: list[Mapping[str, Any]] = []
    for index, (fast_clause, slow_clause) in enumerate(zip(fast_clauses, slow_clauses)):
        if fast_clause["keyword"] != slow_clause["keyword"] or fast_clause["keyword"] == "RETURN":
            continue
        proposals.append(
            {
                "name": f"drop_clause_{index}_{fast_clause['keyword'].lower()}",
                "fast": _remove_span(fast_query, fast_clause["start"], fast_clause["end"]),
                "slow": _remove_span(slow_query, slow_clause["start"], slow_clause["end"]),
            }
        )
    return tuple(proposals)


def _drop_where_conjunct_proposals(fast_query: str, slow_query: str) -> tuple[Mapping[str, Any], ...]:
    fast_regions = _where_regions(fast_query)
    slow_regions = _where_regions(slow_query)
    proposals: list[Mapping[str, Any]] = []
    for region_index, (fast_region, slow_region) in enumerate(zip(fast_regions, slow_regions)):
        fast_parts = _split_top_level_and(fast_query[fast_region["start"] : fast_region["end"]])
        slow_parts = _split_top_level_and(slow_query[slow_region["start"] : slow_region["end"]])
        if len(fast_parts) != len(slow_parts) or len(fast_parts) <= 1:
            continue
        for part_index in range(len(fast_parts)):
            fast_candidate = _replace_span(
                fast_query,
                fast_region["start"],
                fast_region["end"],
                " " + " AND ".join(part for idx, part in enumerate(fast_parts) if idx != part_index) + " ",
            )
            slow_candidate = _replace_span(
                slow_query,
                slow_region["start"],
                slow_region["end"],
                " " + " AND ".join(part for idx, part in enumerate(slow_parts) if idx != part_index) + " ",
            )
            proposals.append(
                {
                    "name": f"drop_where_{region_index}_conjunct_{part_index}",
                    "fast": _normalize_query_text(fast_candidate),
                    "slow": _normalize_query_text(slow_candidate),
                }
            )
    return tuple(proposals)


def _clauses(query: str) -> tuple[Mapping[str, Any], ...]:
    pattern = re.compile(r"\b(OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN|WHERE)\b", re.IGNORECASE)
    matches = list(pattern.finditer(query))
    clauses = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(query)
        clauses.append({"keyword": " ".join(match.group(1).upper().split()), "start": match.start(), "end": end})
    return tuple(clauses)


def _where_regions(query: str) -> tuple[Mapping[str, int], ...]:
    regions: list[Mapping[str, int]] = []
    for match in re.finditer(r"\bWHERE\b", query, flags=re.IGNORECASE):
        start = match.end()
        end_match = re.search(r"\b(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|RETURN|ORDER\s+BY|LIMIT|SKIP)\b", query[start:], flags=re.IGNORECASE)
        end = start + end_match.start() if end_match else len(query)
        regions.append({"start": start, "end": end})
    return tuple(regions)


def _split_top_level_and(expression: str) -> tuple[str, ...]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and expression[index : index + 5].upper() == " AND ":
            parts.append(expression[start:index].strip())
            start = index + 5
            index += 4
        index += 1
    parts.append(expression[start:].strip())
    return tuple(part for part in parts if part)


def _remove_span(query: str, start: int, end: int) -> str:
    return _normalize_query_text(query[:start] + " " + query[end:])


def _replace_span(query: str, start: int, end: int, replacement: str) -> str:
    return query[:start] + replacement + query[end:]


def _json_dumps(payload: Mapping[str, Any]) -> str:
    import json

    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
