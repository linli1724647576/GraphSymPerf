from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import median
import json
from neo4j.exceptions import Neo4jError
from neo4j.graph import Node, Relationship, Path as GraphPath


def run_pair(db, source, mutant, options, *, check_only=False):
    queries = [source, mutant]
    result = {"status": "checking", "discovery": [], "confirmation": []}
    try:
        reference = None
        # Compare complete typed row multisets before screening any timing difference.
        # 先比较完整的带类型结果多重集，再筛选耗时差异。
        for query in queries:
            columns, rows, _ = db.execute(query, options["timeout_seconds"])
            current = (columns, Counter(tuple(freeze(value) for value in row) for row in rows))
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
                    columns, rows, elapsed = db.execute(queries[index], options["timeout_seconds"])
                    current = (columns, Counter(tuple(freeze(value) for value in row) for row in rows))
                    if current != reference:
                        return mismatch(result, reference, current)
                    times[index] = elapsed
                if iteration >= warmups:
                    result[phase].append({"order": "AB" if order[0] == 0 else "BA", "a_ms": times[0], "b_ms": times[1]})
            summary = summarize(result[phase])
            result[phase + "_summary"] = summary
            if not qualifies(summary, options):
                result["status"] = "no_gap" if phase == "discovery" else "unconfirmed"
                return result
            if phase == "confirmation" and summary["slower"] != result["discovery_summary"]["slower"]:
                result["status"] = "unconfirmed"
                return result
        result["status"] = "candidate"
        # PROFILE runs separately so instrumentation does not affect timing samples.
        # PROFILE 单独执行，避免分析开销进入普通计时样本。
        result["plans"] = {"a": db.profile(source, options["timeout_seconds"]), "b": db.profile(mutant, options["timeout_seconds"])}
        return result
    except Neo4jError as error:
        if "TimedOut" not in (error.code or "") and "TransactionTimeout" not in (error.code or ""):
            raise
        # A timeout is an outcome, not a measured runtime or a precise slowdown ratio.
        # 超时是独立结果，不能把超时上限当作实际耗时或精确倍率。
        result["status"] = "timeout"
        result["error"] = {"code": error.code, "message": error.message}
        return result


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


def freeze(value):
    if value is None:
        return ("null",)
    # Python bool is an int subclass; tag it first to avoid equating true with 1.
    # Python 的 bool 是 int 子类，必须先区分，避免把 true 与 1 判为相同。
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, Node):
        return ("node", value.element_id)
    if isinstance(value, Relationship):
        return ("relationship", value.element_id)
    if isinstance(value, GraphPath):
        # Preserve path and list order; only the order of result rows is ignored.
        # 保留路径和列表的内部顺序，只忽略结果行之间的顺序。
        return ("path", tuple(node.element_id for node in value.nodes),
                tuple(edge.element_id for edge in value.relationships))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(freeze(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((key, freeze(item)) for key, item in value.items())))
    raise TypeError(f"Unsupported result type: {type(value).__name__}")


def save_case(directory, case, graph, source, mutant, result, config, version):
    # Archive the actual graph and query text; replay does not depend on regenerating a seed.
    # 保存实际图和查询文本，重放不依赖按旧种子重新生成。
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "query_a.cypher").write_text(source + "\n")
    (directory / "query_b.cypher").write_text(mutant + "\n")
    (directory / "graph.json").write_text(json.dumps(asdict(graph), ensure_ascii=False))
    record = {"pattern": case.pattern, "seed": case.seed, "bindings": case.bindings,
              "nodes": len(graph.nodes), "relationships": len(graph.relationships),
              "config": config, "database_version": version,
              **{key: value for key, value in result.items() if key != "plans"}}
    (directory / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    if "plans" in result:
        (directory / "plans.json").write_text(json.dumps(result["plans"], ensure_ascii=False, indent=2))
