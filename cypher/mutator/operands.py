from cypher.ast_nodes import *
from data_structures.ast import replace_at
from data_structures.case import Case, symbols
from data_structures.graph import shares


SCALAR_TYPES = {"int", "float", "bool", "string"}


def prepare(query, site, pattern, seed, rng, work_kind="count"):
    # Reuse a traceable outer node or introduce a private root inside the local subquery.
    # 复用来源可追踪的外层节点，或在局部子查询中引入独立根节点。
    external = [v for v in site.variables.values() if v.kind == "node" and v.labels and not v.nullable]
    correlated = bool(external) and rng.random() < .65
    anchor = rng.choice(external) if correlated else Variable("p_root", "node", (Symbol("p_root", "label"),))
    # Host names use v...; pattern-local names use p_... to avoid variable capture.
    # 宿主变量使用 v...，Pattern 局部变量使用 p_...，避免意外引用同名变量。
    payload = {
        "anchor": anchor, "correlated": correlated, "work_kind": work_kind,
        "low_label": Symbol("p_low", "label"), "high_label": Symbol("p_high", "label"),
        "low_type": Symbol("p_low_edge", "relationship"), "high_type": Symbol("p_high_edge", "relationship"),
        "witness_label": Symbol("p_witness", "label"), "witness_type": Symbol("p_witness_edge", "relationship"),
    }
    return Case(pattern, seed, query, site.path, set(symbols(query)), payload)


def anchor_node(payload):
    anchor = payload["anchor"]
    return NodePattern(anchor, [] if payload["correlated"] else list(anchor.labels))


def rows(payload, role, outputs=None, predicate=True):
    node = Variable("p_" + role + "_node", "node", (payload[role + "_label"],))
    path = PathPattern([anchor_node(payload), NodePattern(node, list(node.labels))],
                       [RelationshipPattern([payload[role + "_type"]])])
    where = Binary(Property(node, "keep", "bool"), "=", Literal(True, "bool")) if predicate else None
    outputs = outputs or [Variable("p_value", "int")]
    # Constant projections preserve values and duplicates when a limit selects any n rows.
    # 各分支投影同一常量，使 LIMIT 任取 n 行时仍保持值和重复次数一致。
    items = [ProjectionItem(constant(variable.kind), variable) for variable in outputs]
    return Query([Match([path], where=where), Projection("RETURN", items)])


def constant(kind):
    return Literal({"int": 1, "float": 1.5, "bool": True, "string": "kept"}[kind], kind)


def work(payload, role, kind):
    query = rows(payload, role)
    match = query.clauses[0]
    if payload["work_kind"] == "pattern_list":
        # Keep the traversal inside the operand so short-circuit evaluation can avoid it.
        # 遍历保留在操作数内部，使短路求值有机会跳过这部分工作。
        values = PatternComprehension(match.paths[0], match.where, Literal(1))
        return Binary(Function("size", [values], "int"), ">", Literal(0))
    body = MatchBody(match.paths, match.where) if payload["work_kind"] == "match_count" else query
    count = SubqueryExpression("COUNT", body)
    if kind == "int":
        return count
    if kind == "bool":
        return Binary(count, ">", Literal(0))
    if payload["work_kind"] == "match_count":
        # Ladybug uses native CAST for scalar COUNT results; keep the declared AST type.
        # Ladybug 使用原生 CAST 转换 COUNT 标量结果，保持 AST 声明的类型。
        target_type = "DOUBLE" if kind == "float" else "STRING"
        return Function("CAST", [count, Literal(target_type, "string")], kind)
    return Function("toFloat" if kind == "float" else "toString", [count], kind)


def guard(payload, truth, rng):
    # Each candidate supplies a known truth value under the matching graph recipe.
    # 每个候选在配套构图条件下具有确定真值，不能把任意表达式当作决定项。
    choices = ["literal", "comparison", "exists"] if payload["work_kind"] == "pattern_list" else ["literal", "comparison", "count", "exists"]
    if payload["correlated"]:
        choices.append("property")
    name = rng.choice(choices)
    payload["guard_name"] = name
    payload["guard_value"] = truth
    if name == "literal":
        return Literal(truth, "bool")
    if name == "comparison":
        return Binary(Literal(2), ">" if truth else "<", Literal(1))
    if name == "count":
        return Binary(work(payload, "low", "int"), ">=" if truth else "<", Literal(0))
    if name == "exists":
        if payload["work_kind"] == "pattern_list":
            # The decisive witness must stay separate from the deliberately failing work region.
            # 决定项见证与故意不满足谓词的工作区域分离。
            expression = work(payload, "witness", "bool")
        else:
            query = rows(payload, "low")
            match = query.clauses[0]
            body = MatchBody(match.paths, match.where) if payload["work_kind"] == "match_count" else query
            expression = SubqueryExpression("EXISTS", body)
        return expression if truth else Unary("NOT", expression)
    value = Property(payload["anchor"], "p_guard", "bool")
    return Function("coalesce", [value, Literal(truth, "bool")], "bool")


def nonnull(payload, kind, rng):
    # A zero count is still non-null; the prefix need not be true or numerically positive.
    # 计数为零仍然是非空值；非空前缀不要求为真或大于零。
    choices = ["literal"] if payload["work_kind"] == "pattern_list" else ["literal", "count"]
    if payload["correlated"]:
        choices.append("property")
    name = rng.choice(choices)
    payload["nonnull_name"] = name
    payload["nonnull_kind"] = kind
    if name == "literal":
        if kind == "bool" and payload["work_kind"] == "pattern_list":
            return Literal(rng.choice([False, True]), "bool")
        return constant(kind)
    if name == "count":
        return work(payload, "low", kind)
    return Function("coalesce", [Property(payload["anchor"], "p_value", kind), constant(kind)], kind)


def install(case, expression):
    case.query = replace_at(case.query, case.target, expression)
    return case


def populate_regions(graph, payload, total, *, witness=0, high_keep=True):
    roots = graph.candidates(payload["anchor"], create=True)
    allocations = shares(total, len(roots))
    payload["anchor_count"] = len(roots)
    payload["anchor_uids"] = roots
    payload["high_allocations"] = allocations
    pattern_list = payload.get("work_kind") == "pattern_list"
    if pattern_list:
        witness = int(payload.get("guard_name") == "exists")
    # Guards and witnesses apply per anchor; only the high-cost total is shared across anchors.
    # 守卫与见证条件逐锚点满足，只有高成本总预算在锚点之间分配。
    for root, count in zip(roots, allocations):
        if "guard_value" in payload:
            graph.nodes[root]["properties"]["p_guard"] = payload["guard_value"]
        if payload.get("nonnull_name") == "property":
            graph.nodes[root]["properties"]["p_value"] = constant(payload["nonnull_kind"]).value
        for role, quantity in (("low", 1), ("witness", witness), ("high", count)):
            for _ in range(quantity):
                node = graph.node([payload[role + "_label"]], keep=(role == "witness") if pattern_list else (high_keep if role == "high" else True), value=1)
                graph.edge(root, node, payload[role + "_type"])
