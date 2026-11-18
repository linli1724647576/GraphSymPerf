from data_structures.ast import Symbol, at, replace_at, walk
from data_structures.case import Case, symbols
from data_structures.graph import shares
from gremlin.ast_nodes import Traversal, Step, Value


VALUES = {"int": [0, 1, -1, 2], "float": [0.0, 1.5, 2.5],
          "bool": [False, True], "string": ["", "kept", "x"]}


def anchors(scope):
    values = [(None, scope.current)] + list(scope.bindings.items())
    return [(name, value) for name, value in values if value.kind == "node" and value.labels and not value.nullable]


def prepare(query, site, pattern, seed, rng):
    name, anchor = rng.choice(anchors(site.scope))
    payload = {"anchor": anchor, "select": name, "work_kind": "gremlin_traversal",
               **{role + "_label": Symbol("Pattern_p_" + role, "label") for role in ("low", "high", "witness")},
               **{role + "_type": Symbol("Pattern_p_" + role + "_edge", "relationship") for role in ("low", "high", "witness")}}
    return Case(pattern, seed, query, site.path, set(symbols(query)), payload)


def prefix(payload):
    return [Step("select", [payload["select"]])] if payload["select"] is not None else []


def work(payload, role, kind="predicate"):
    expansion = Step("out", [payload[role + "_type"]])
    steps = [expansion]
    if kind == "int":
        steps.append(Step("count"))
    else:
        steps.append(Step("has", ["keep", Value(True, "bool")]))
        if kind != "predicate":
            steps.append(Step("constant", [payload["value"]]))
    return Traversal(steps), expansion


def guard(payload, truth, rng):
    name = rng.choice(["identity", "property", "witness"])
    payload.update(guard_name=name, guard_value=truth)
    if name == "identity":
        steps = [Step("identity")] if truth else [Step("not", [Traversal([Step("identity")])])]
    elif name == "property":
        steps = [Step("has", ["p_gate_true" if truth else "p_gate_false", Value(True, "bool")])]
    else:
        witness = Traversal([Step("out", [payload["witness_type"]])])
        steps = witness.steps if truth else [Step("not", [witness])]
    return Traversal(steps)


def install(case, replacement, target):
    case.query = replace_at(case.query, case.target, replacement)
    case.target = next(path for path, node in walk(case.query) if node is target)
    return case


def mutate(case):
    at(case.query, case.target).args[0] = case.payload["high_type"]
    return case.query


def populate(graph, payload, total):
    roots = graph.candidates(payload["anchor"], create=True)
    allocation = shares(total, len(roots))
    payload.update(anchor_uids=roots, anchor_count=len(roots), high_allocations=allocation)
    for root, count in zip(roots, allocation):
        props = graph.nodes[root]["properties"]
        props.update(p_gate_true=True, p_gate_false=False)
        if payload.get("prefix_name") == "property":
            props["p_prefix"] = payload["value"].value
        # Count distinct graph anchors once, even if the host emits an anchor repeatedly.
        if payload.get("variant") == "native_limit":
            for _ in range(payload["limit"]):
                node = graph.node([payload["low_label"]], keep=False)
                graph.edge(root, node, payload["low_type"])
                graph.edge(root, node, payload["high_type"])
            quantities = [("high", count)]
        else:
            quantities = [("low", 1), ("high", count)]
        for role, quantity in quantities:
            for _ in range(quantity):
                node = graph.node([payload[role + "_label"]], keep=False)
                graph.edge(root, node, payload[role + "_type"])
        if payload.get("guard_name") == "witness":
            node = graph.node([payload["witness_label"]], keep=True)
            graph.edge(root, node, payload["witness_type"])
