from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import median
import json
from data_structures.ast import at
from data_structures.query import QueryRejected


def run_pair(db, source, mutant, options, *, check_only=False):
    queries = [source, mutant]
    result = {"status": "checking", "discovery": [], "confirmation": []}
    phase, active_query = "checking", "a"
    try:
        reference = None
        # Compare complete typed row multisets before screening any timing difference.
        # 先比较完整的带类型结果多重集，再筛选耗时差异。
        for active_query, query in zip(("a", "b"), queries):
            columns, rows, _ = db.execute(query, options["timeout_seconds"])
            if getattr(db, "cancellation", None):
                result.setdefault("cancellations", []).append({"phase": phase, "query": active_query, "response": db.cancellation})
            current = (columns, Counter(rows))
            if reference is None:
                reference = current
            elif current != reference:
                return mismatch(result, reference, current)
        result["rows"] = sum(reference[1].values())
        if check_only:
            result["status"] = "equivalent"
            return result
        for phase, warmups, pairs, offset in (
            ("discovery", options["warmups"], options["pairs"], 0),
            ("confirmation", options["confirmation_warmups"], options["confirmation_pairs"], 1),
        ):
            for iteration in range(warmups + pairs):
                # Alternate AB/BA, excluding warmups from samples but still checking their results.
                # 交替执行 AB/BA；预热不计入耗时样本，但仍检查其结果。
                order = [0, 1] if (iteration + offset) % 2 == 0 else [1, 0]
                times = [0.0, 0.0]
                for index in order:
                    active_query = "a" if index == 0 else "b"
                    columns, rows, elapsed = db.execute(queries[index], options["timeout_seconds"])
                    if getattr(db, "cancellation", None):
                        result.setdefault("cancellations", []).append({"phase": phase, "query": active_query, "response": db.cancellation})
                    current = (columns, Counter(rows))
                    if current != reference:
                        return mismatch(result, reference, current)
                    times[index] = elapsed
                if iteration >= warmups:
                    result[phase].append({"order": "AB" if order[0] == 0 else "BA", "a_ms": times[0], "b_ms": times[1]})
            summary = summarize(result[phase])
            result[phase + "_summary"] = summary
            if not qualifies(summary, options):
                result["status"] = "no_gap" if phase == "discovery" else "unconfirmed"
                break
            if phase == "confirmation" and summary["slower"] != result["discovery_summary"]["slower"]:
                result["status"] = "unconfirmed"
                break
        else:
            result["status"] = "candidate"
    except QueryRejected as error:
        # A rejected side is evidence to archive, not a successful equivalence check.
        # 被拒绝的一侧需要归档，不能记作已经通过等价检查。
        checked = [] if reference is None else (["a"] if phase == "checking" else ["a", "b"])
        result.update(status="backend_rejected", reason=error.reason, checked_queries=checked,
                      error={"phase": phase, "query": active_query, **vars(error)})
        return result
    except TimeoutError as error:
        # A failed timing pair contributes neither a runtime nor a slowdown ratio.
        # 未完成的计时对不产生运行时间或加速比结论。
        result["status"] = "timeout"
        result["error"] = {"phase": phase, "query": active_query,
                           "limit_ms": options["timeout_seconds"] * 1000, "message": str(error)}
        result["error"].update(vars(error))
        if getattr(error, "stop_run", False):
            result["stop_run"] = True
        return result

    # Profile completed measurements, including no_gap; profiling never changes timing status.
    # 完成计时后采集计划，包括 no_gap；PROFILE 不改变已经得到的计时状态。
    result["plans"] = {}
    for name, query in zip(("a", "b"), queries):
        try:
            result["plans"][name] = db.profile(query, options["timeout_seconds"])
        except QueryRejected as error:
            result["plans"][name] = {"status": "backend_rejected", **vars(error)}
        except TimeoutError as error:
            result["plans"][name] = {"status": "timeout", "message": str(error)}
            result["plans"][name].update(vars(error))
            if getattr(error, "stop_run", False):
                # Preserve confirmed timing evidence, but submit no further query after an uncertain stop.
                # 保留已确认的计时证据，终止未确认时不再提交后续查询。
                result["stop_run"] = True
                break
    statuses = [plan["status"] for plan in result["plans"].values()]
    if all(status == "ok" for status in statuses):
        result["profile_status"] = "ok"
    else:
        result["profile_status"] = ("partial" if "ok" in statuses else
                                    "timeout" if "timeout" in statuses else "backend_rejected")
    return result


def workload_record(case, graph, high_units):
    # Construction describes potential work; execution stays unknown until the plan is reviewed.
    # 构图信息描述潜在工作；执行情况在核对原始计划之前一律保留为 unknown。
    payload = asdict(case)["payload"]
    roles = {key: value for key, value in payload.items()
             if key.endswith(("_label", "_type")) or key == "relation"}
    conditions = {key: payload[key] for key in (
        "anchor", "correlated", "select", "guard_name", "guard_value", "nonnull_name", "nonnull_kind",
        "prefix_name", "value", "left", "right", "nodes", "direction", "maximum", "swap_index", "small_empty"
    ) if key in payload}
    if case.pattern in {"S5", "S6"}:
        # S5 always renders LIMIT 0; its unused recipe draw is not the query's limit.
        # S5 始终生成 LIMIT 0，配方中未使用的随机值不是查询的实际限制。
        conditions["limit"] = 0 if case.pattern == "S5" else payload["limit"]
    return {"target_path": list(case.target), "target_kind": type(at(case.query, case.target)).__name__,
            "variant": payload.get("variant"), "work_kind": payload.get("work_kind"),
            "roles": roles, "conditions": conditions, "configured_high_units": high_units,
            "anchor_uids": payload.get("anchor_uids"), "high_allocations": payload.get("high_allocations"),
            "graph_counts": {
                "labels": dict(Counter(label for node in graph.nodes for label in node["labels"])),
                "relationship_types": dict(Counter(edge["type"] for edge in graph.relationships))},
            "opportunity": "unknown", "execution": {"a": "unknown", "b": "unknown"}}


def summarize(samples):
    a = median(sample["a_ms"] for sample in samples)
    b = median(sample["b_ms"] for sample in samples)
    slower = "a" if a >= b else "b"
    # Choose the slower side by medians, then measure paired differences in that direction.
    # 用各自耗时中位数确定慢侧，再沿该方向计算每对样本的耗时差。
    signed = [sample["a_ms"] - sample["b_ms"] for sample in samples]
    if slower == "b":
        signed = [-value for value in signed]
    return {"a_median_ms": a, "b_median_ms": b, "slower": slower,
            "ratio": max(a, b) / min(a, b), "delta_ms": median(signed),
            "direction_consistency": sum(value > 0 for value in signed) / len(signed)}


def qualifies(summary, options):
    return (summary["ratio"] >= options["ratio"]
            and summary["delta_ms"] >= options["minimum_delta_ms"]
            and summary["direction_consistency"] >= options["direction_consistency"])


def mismatch(result, reference, current):
    result["status"] = "result_mismatch"
    result["expected_columns"], result["actual_columns"] = reference[0], current[0]
    result["expected_rows"], result["actual_rows"] = sum(reference[1].values()), sum(current[1].values())
    result["missing_examples"] = list((reference[1] - current[1]).items())[:10]
    result["extra_examples"] = list((current[1] - reference[1]).items())[:10]
    return result


def save_case(directory, case, graph, source, mutant, result, config, version):
    # Archive the actual graph and query text; replay does not depend on regenerating a seed.
    # 保存实际图和查询文本，重放不依赖按旧种子重新生成。
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    language = config["language"]
    (directory / f"query_a.{language}").write_text(source.text + "\n")
    (directory / f"query_b.{language}").write_text(mutant.text + "\n")
    (directory / "graph.json").write_text(json.dumps(asdict(graph), ensure_ascii=False))
    workload = workload_record(case, graph, config["graph"]["high_units"])
    construction = {key: case.payload[key] for key in
                    ("anchor_uids", "high_allocations") if key in case.payload}
    if "limit" in workload["conditions"]:
        construction["limit"] = workload["conditions"]["limit"]
    record = {"pattern": case.pattern, "seed": case.seed, "bindings": case.bindings,
              "workload": workload,
              "variant": case.payload.get("variant"), "work_kind": case.payload.get("work_kind"),
              "construction": construction,
              "nodes": len(graph.nodes), "relationships": len(graph.relationships),
              "config": config, "database_version": version,
              "parameters": {"a": source.parameters, "b": mutant.parameters},
              "columns": {"a": source.columns, "b": mutant.columns},
              **{key: value for key, value in result.items() if key != "plans"}}
    (directory / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    if "plans" in result:
        (directory / "plans.json").write_text(json.dumps(result["plans"], ensure_ascii=False, indent=2))
