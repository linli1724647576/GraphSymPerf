"""Host equality constraints on a short-circuit anchor, applied to its candidate roots.

The background graph is random, so a host filter such as `(v3:L {property_int: 2})` or
`filter(__.has("property_int", 2))` often rejects every candidate root and the pair
returns no rows. Step ④ can instead write those values onto the roots that already
carry the workload. The queries are unchanged, both sides read the same graph, and the
guards are still installed on every candidate, so equivalence is unaffected.
"""
from data_structures.ast import walk


def anchor_constraints(case):
    anchor = case.payload.get("anchor")
    if anchor is None or not case.payload.get("anchor_uids"):
        return {}
    from gremlin.ast_nodes import Traversal
    return _gremlin(case) if isinstance(case.query, Traversal) else _cypher(case.query, anchor)


def apply_anchor_constraints(graph, case):
    constraints = anchor_constraints(case)
    for root in case.payload.get("anchor_uids", ()) if constraints else ():
        graph.nodes[root]["properties"].update({str(key): value for key, value in constraints.items()})
    if constraints:
        case.payload["anchor_constraints"] = {str(key): value for key, value in constraints.items()}
    return constraints


def _cypher(query, anchor):
    from cypher.ast_nodes import NodePattern, Match, Binary, Property, Literal
    found = {}

    def keep(key, literal):
        # The first value per key wins; conflicting host filters stay unsatisfiable.
        # 每个属性键以第一个取值为准；相互矛盾的宿主过滤本来就无法满足。
        if literal.value is not None:
            found.setdefault(key, literal.value)

    def conjuncts(expression):
        if isinstance(expression, Binary) and expression.operator == "AND":
            yield from conjuncts(expression.left)
            yield from conjuncts(expression.right)
        else:
            yield expression

    for _, node in walk(query):
        if isinstance(node, NodePattern) and node.variable.name == anchor.name:
            for key, value in node.properties:
                if isinstance(value, Literal):
                    keep(key, value)
        elif isinstance(node, Match) and node.where is not None:
            declared = {item.variable.name for path in node.paths for item in path.nodes}
            if anchor.name not in declared:
                continue
            for term in conjuncts(node.where):
                if not (isinstance(term, Binary) and term.operator == "="):
                    continue
                for left, right in ((term.left, term.right), (term.right, term.left)):
                    if (isinstance(left, Property) and left.variable.name == anchor.name
                            and isinstance(right, Literal)):
                        keep(left.key, right)
    return found


STAY = {"hasLabel", "filter", "as", "identity"}


def _gremlin(case):
    from gremlin.ast_nodes import Traversal, Value
    steps = case.query.steps
    if case.payload.get("select") is None:
        index = case.target[1]
    else:
        index = next((i for i, step in enumerate(steps)
                      if step.name == "as" and step.args[0] == case.payload["select"]), None)
        if index is None:
            return {}
    # Collect has() filters in the run of steps that keep the anchor as the current object.
    # 收集锚点仍为当前对象的那一段连续步骤中的 has() 过滤。
    start = index
    while start > 0 and steps[start - 1].name in STAY:
        start -= 1
    end = index
    while end + 1 < len(steps) and steps[end + 1].name in STAY:
        end += 1
    found = {}
    for position in range(start, end + 1):
        step = steps[position]
        if position == case.target[1] or step.name != "filter":
            continue
        inner = step.args[0]
        if (isinstance(inner, Traversal) and len(inner.steps) == 1 and inner.steps[0].name == "has"
                and isinstance(inner.steps[0].args[1], Value)):
            key, value = inner.steps[0].args
            found.setdefault(key, value.value)
    return found


def plant_host_witness(graph, case):
    """Add one instance of the host's mandatory matching before the pattern is populated.

    Random background edges rarely complete a multi-hop host pattern, so most pairs
    return no rows and the workload sits on roots the host never reaches. This plants
    the host MATCH paths (Cypher) or the root traversal prefix (Gremlin) with their
    labels and literal property filters. It runs before populate(), so a planted node
    that carries the anchor's labels is a candidate root and receives the same guard
    witnesses and workload as every other candidate.
    """
    from gremlin.ast_nodes import Traversal
    planted = _plant_gremlin(graph, case) if isinstance(case.query, Traversal) else _plant_cypher(graph, case)
    if planted:
        case.payload["host_witness"] = planted
    return planted


def _host_label(case):
    labels = sorted(s for s in case.host_symbols if s.kind == "label")
    return labels[0] if labels else "Background"


def _first_node(graph, labels):
    """Reuse the first node with these labels, which recipes also pick as their root."""
    required = set(map(str, labels))
    for index, node in enumerate(graph.nodes):
        if required <= set(node["labels"]):
            return index
    return graph.node(list(labels), keep=True, value=1)


def _plant_cypher(graph, case):
    from cypher.ast_nodes import Match, Literal
    nodes = {}

    def node(pattern):
        name = pattern.variable.name
        if name.startswith("p_"):
            return None
        if name not in nodes:
            labels = list(pattern.labels or pattern.variable.labels) or [_host_label(case)]
            nodes[name] = _first_node(graph, labels)
        uid = nodes[name]
        for key, value in pattern.properties:
            # Pattern-owned keys such as p_anchor stay under the recipe's control.
            # p_anchor 等 Pattern 自有属性仍由构图配方控制。
            if isinstance(value, Literal) and value.value is not None and not str(key).startswith("p_"):
                graph.nodes[uid]["properties"][str(key)] = value.value
        return uid

    for _, match in walk(case.query):
        if not isinstance(match, Match) or match.optional:
            continue
        for path in match.paths:
            ends = [node(item) for item in path.nodes]
            if None in ends:
                continue
            for (start, end), relation in zip(zip(ends, ends[1:]), path.relationships):
                if not relation.types or str(relation.types[0]).startswith("Pattern_"):
                    continue
                kind = relation.types[0]
                # A variable-length relationship gets its shortest allowed chain.
                # 变长关系按允许的最短长度铺设。
                hops = [start] + [graph.node([_host_label(case)], keep=True, value=1)
                                  for _ in range(max(relation.minimum, 1) - 1)] + [end]
                for left, right in zip(hops, hops[1:]):
                    graph.edge(*((right, left) if relation.direction == "in" else (left, right)), kind, keep=True)
        for name, (key, value) in _cypher_equalities(match):
            if name in nodes and not str(key).startswith("p_"):
                graph.nodes[nodes[name]]["properties"][str(key)] = value
    return sorted(set(nodes.values()))


def _cypher_equalities(match):
    from cypher.ast_nodes import Binary, Property, Literal
    if match.where is None:
        return
    pending = [match.where]
    while pending:
        term = pending.pop()
        if isinstance(term, Binary) and term.operator == "AND":
            pending += [term.left, term.right]
        elif isinstance(term, Binary) and term.operator == "=":
            for left, right in ((term.left, term.right), (term.right, term.left)):
                if isinstance(left, Property) and isinstance(right, Literal) and right.value is not None:
                    yield left.variable.name, (left.key, right.value)


def _plant_gremlin(graph, case):
    from gremlin.ast_nodes import Traversal, Value
    steps = case.query.steps
    pattern_step = case.target[1] if case.target and case.target[0] == "steps" else None
    if len(steps) < 2 or steps[1].name != "hasLabel":
        return []
    current = _first_node(graph, steps[1].args)
    planted, bindings, index = [current], {}, 2

    def set_property(key, value):
        if not str(key).startswith("p_"):
            graph.nodes[current]["properties"][str(key)] = value.value

    while index < len(steps):
        step = steps[index]
        if step.name == "match":
            # The incoming object binds the patterns' start labels; their own edges are the recipe's.
            # 输入对象绑定各模式的起始标签；模式内的边由配方构造。
            for pattern in step.args:
                bindings.setdefault(pattern.steps[0].args[0], current)
            index += 1
        elif index == pattern_step:
            # The pattern's own step is built by its recipe; continue with the host after it.
            # Pattern 自己的步骤由配方构造，越过它继续铺设宿主遍历。
            index += 1
        elif (step.name in {"out", "in", "both"} and not str(step.args[0]).startswith("Pattern_")
                and index + 1 < len(steps) and steps[index + 1].name == "hasLabel"):
            following = graph.node(list(steps[index + 1].args), keep=True, value=1)
            graph.edge(*((following, current) if step.name == "in" else (current, following)), step.args[0], keep=True)
            current, index = following, index + 2
            planted.append(current)
        elif step.name == "filter":
            inner = step.args[0]
            if (isinstance(inner, Traversal) and len(inner.steps) == 1 and inner.steps[0].name == "has"
                    and isinstance(inner.steps[0].args[1], Value)):
                set_property(*inner.steps[0].args)
            index += 1
        elif step.name == "has" and isinstance(step.args[1], Value):
            set_property(*step.args)
            index += 1
        elif step.name == "as":
            bindings[step.args[0]] = current
            index += 1
        elif step.name == "select" and step.args[0] in bindings:
            current, index = bindings[step.args[0]], index + 1
        elif step.name == "identity":
            index += 1
        else:
            break
    return sorted(set(planted))
