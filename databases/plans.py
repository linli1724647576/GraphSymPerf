"""Backend-neutral summaries of archived PROFILE output.

Each summary lists normalized operator names in plan order, the total work the plan
reports, and its hottest operator. Work is the backend's own per-operator counter:
Neo4j DB hits plus rows, Memgraph actual hits, FalkorDB records produced, Ladybug
output tuples, and TinkerPop or ArcadeDB Gremlin element counts. ArcadeDB Cypher
plans report only time, so their work is None.
"""
import re


def normalize(name):
    # Keep the operator kind; drop arguments, identifiers and backend suffixes.
    # 只保留算子类别，去掉参数、标识符和后端后缀。
    name = re.sub(r"[({\[].*$", "", str(name)).split("@")[0]
    return re.sub(r"[\s*|\\]+", " ", name).strip()


def summarize(plan):
    if plan.get("status") != "ok":
        return None
    operators = list(_operators(plan))
    counted = [(index, name, count) for index, (name, count) in enumerate(operators) if count is not None]
    # Ties go to the later operator, which is the nested step that does the work.
    # 计数相同时取靠后的算子，即真正做工作的内层步骤。
    hot = max(counted, key=lambda item: (item[2], item[0])) if counted else None
    work = sum(count for _, _, count in counted) if counted else None
    if plan["format"] == "ladybug_profile":
        text = "\n".join(str(cell) for row in plan["rows"] for cell in row)
        work = sum(int(value) for value in re.findall(r"NumOutputTuples: (\d+)", text))
    return {"operators": [name for name, _ in operators], "work": work,
            "hot": {"operator": hot[1], "count": hot[2]} if hot else None}


def _operators(plan):
    kind = plan["format"]
    if kind == "neo4j_profile":
        def walk(node):
            yield normalize(node.get("operatorType", "")), node.get("dbHits", 0) + node.get("rows", 0)
            for child in node.get("children", []):
                yield from walk(child)
        yield from walk(plan["plan"])
    elif kind == "memgraph_profile":
        for row in plan["rows"]:
            if isinstance(row[1], int):
                yield normalize(row[0]), row[1]
    elif kind == "falkordb_profile":
        for line in plan["lines"]:
            match = re.search(r"Records produced: (\d+)", line)
            yield normalize(line.split("|")[0]), int(match.group(1)) if match else None
    elif kind == "ladybug_profile":
        text = "\n".join(str(cell) for row in plan["rows"] for cell in row)
        # Side-by-side boxes make per-operator pairing unreliable; summarize() reports the total only.
        # 并排的方框无法可靠地把计数对应到算子，summarize() 只报告总量。
        for name in re.findall(r"([A-Z][A-Z_]+)\[\d+\]", text):
            yield normalize(name), None
    elif kind == "arcadedb_profile":
        def walk(step):
            yield normalize(step.get("name", step.get("type", ""))), step.get("elements")
            for child in step.get("subSteps", []):
                yield from walk(child)
        for step in plan["plan"].get("steps", []):
            yield from walk(step)
    elif kind == "tinkerpop_profile":
        def walk(metric):
            yield normalize(metric.get("name", "")), metric.get("counts", {}).get("elementCount")
            for child in metric.get("metrics", []):
                yield from walk(child)
        for metric in plan["metrics"].get("metrics", []):
            yield from walk(metric)


def totals(plan):
    """Work per normalized operator name, or None when the plan reports no counts."""
    if plan.get("status") != "ok":
        return None
    result = {}
    for name, count in _operators(plan):
        if count is not None:
            result[name] = result.get(name, 0) + count
    return result or None


def signature(system, pattern, plans, slower):
    """Cluster key for a confirmed gap: the operator whose work grows most on the slower side."""
    faster = "a" if slower == "b" else "b"
    slow, fast = totals(plans.get(slower, {})), totals(plans.get(faster, {}))
    if slow is None or fast is None:
        names = [summarize(plans.get(side, {})) for side in (slower, faster)]
        operators = tuple(sorted(set(names[0]["operators"]) ^ set(names[1]["operators"]))) if all(names) else ()
        return {"system": system, "pattern": pattern, "operator": None, "extra_work": None, "differing": operators}
    growth = {name: slow.get(name, 0) - fast.get(name, 0) for name in set(slow) | set(fast)}
    name = min(growth, key=lambda key: (-growth[key], key))
    return {"system": system, "pattern": pattern, "operator": name, "extra_work": growth[name],
            "differing": tuple(sorted(set(slow) ^ set(fast)))}
