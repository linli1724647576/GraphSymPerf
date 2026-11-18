from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import replace_at
from data_structures.case import Case, symbols
from data_structures.graph import shares


SCALAR_TYPES = {"int", "float", "bool", "string"}


@dataclass(frozen=True)
class WorkCandidate:
    mode: str
    keep: bool
    two_hops: bool = False

    def expression(self, payload, role, kind="bool"):
        if self.mode == "COUNT":
            return work(payload, role, kind, two_hops=self.two_hops)
        return measure_rows(payload, role, "EXISTS")


WORK_CANDIDATES = {
    "one_hop_count": WorkCandidate("COUNT", keep=True),
    "two_hop_count": WorkCandidate("COUNT", keep=True, two_hops=True),
    "no_match_exists": WorkCandidate("EXISTS", keep=False),
}


def choose_work(payload, kind, rng):
    names = [name for name, candidate in WORK_CANDIDATES.items() if kind == "bool" or candidate.mode == "COUNT"]
    name = rng.choice(names)
    payload["work_candidate"] = name
    candidate = WORK_CANDIDATES[name]
    if candidate.two_hops:
        prepare_two_hops(payload)
    return candidate


ROW_SOURCES = {"one_hop_rows": False, "two_hop_rows": True}


def choose_row_source(payload, rng, roles=("low", "high")):
    name = rng.choice(tuple(ROW_SOURCES))
    payload["work_candidate"] = name
    two_hops = ROW_SOURCES[name]
    if two_hops:
        prepare_two_hops(payload, roles)
    return two_hops


def prepare_two_hops(payload, roles=("low", "high")):
    for role in roles:
        payload[role + "_middle_label"] = Symbol("Pattern_p_" + role + "_middle", "label")
        payload[role + "_first_type"] = Symbol("Pattern_p_" + role + "_first_edge", "relationship")


def prepare(query, site, pattern, seed, rng, work_kind="count"):
    # Reuse a traceable outer node or introduce a private root inside the local subquery.
    external = [v for v in site.variables.values() if v.kind == "node" and v.labels and not v.nullable]
    correlated = bool(external) and rng.random() < .65
    anchor = rng.choice(external) if correlated else Variable("p_root", "node", (Symbol("Pattern_p_root", "label"),))
    # Host names use v...; pattern-local names use p_... to avoid variable capture.
    payload = {
        "anchor": anchor, "correlated": correlated, "work_kind": work_kind,
        "low_label": Symbol("Pattern_p_low", "label"), "high_label": Symbol("Pattern_p_high", "label"),
        "low_type": Symbol("Pattern_p_low_edge", "relationship"), "high_type": Symbol("Pattern_p_high_edge", "relationship"),
        "witness_label": Symbol("Pattern_p_witness", "label"), "witness_type": Symbol("Pattern_p_witness_edge", "relationship"),
    }
    return Case(pattern, seed, query, site.path, set(symbols(query)), payload)


def anchor_node(payload):
    anchor = payload["anchor"]
    return NodePattern(anchor, [] if payload["correlated"] else list(anchor.labels))


def rows(payload, role, outputs=None, predicate=True, *, two_hops=False):
    node = Variable("p_" + role + "_node", "node", (payload[role + "_label"],))
    path = PathPattern([anchor_node(payload), NodePattern(node, list(node.labels))],
                       [RelationshipPattern([payload[role + "_type"]])])
    if two_hops:
        middle = Variable("p_" + role + "_middle", "node", (payload[role + "_middle_label"],))
        path.nodes.insert(1, NodePattern(middle, list(middle.labels)))
        path.relationships.insert(0, RelationshipPattern([payload[role + "_first_type"]]))
    where = Binary(Property(node, "keep", "bool"), "=", Literal(True, "bool")) if predicate else None
    outputs = outputs or [Variable("p_value", "int")]
    # Constant projections preserve values and duplicates when a limit selects any n rows.
    items = [ProjectionItem(constant(variable.kind), variable) for variable in outputs]
    return Query([Match([path], where=where), Projection("RETURN", items)])


def constant(kind):
    return Literal({"int": 1, "float": 1.5, "bool": True, "string": "kept"}[kind], kind)


def measure_rows(payload, role, mode, *, two_hops=False):
    query = rows(payload, role, two_hops=two_hops)
    match = query.clauses[0]
    if payload["work_kind"] == "pattern_list":
        if mode == "EXISTS":
            # FalkorDB expresses existential quantification over a pattern comprehension.
            flags = PatternComprehension(match.paths[0], Literal(True, "bool"), match.where)
            flag = Variable("p_keep", "bool")
            return Quantifier("any", flag, flags, Reference(flag))
        values = PatternComprehension(match.paths[0], match.where, Literal(1))
        return Function("size", [values], "int")
    body = MatchBody(match.paths, match.where) if payload["work_kind"] == "match_count" else query
    return SubqueryExpression(mode, body)


def work(payload, role, kind, *, two_hops=False):
    count = measure_rows(payload, role, "COUNT", two_hops=two_hops)
    if kind == "bool" or payload["work_kind"] == "pattern_list":
        return Binary(count, ">", Literal(0))
    if kind == "int":
        return count
    if payload["work_kind"] == "match_count":
        # Ladybug uses native CAST for scalar COUNT results; keep the declared AST type.
        target_type = "DOUBLE" if kind == "float" else "STRING"
        return Function("CAST", [count, Literal(target_type, "string")], kind)
    return Function("toFloat" if kind == "float" else "toString", [count], kind)


def guard(payload, truth, rng):
    # Each candidate supplies a known truth value under the matching graph recipe.
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
        return Binary(work(payload, "witness", "int"), ">=" if truth else "<", Literal(0))
    if name == "exists":
        if payload["work_kind"] == "pattern_list":
            # The decisive witness must stay separate from the deliberately failing work region.
            expression = work(payload, "witness", "bool")
        else:
            expression = measure_rows(payload, "witness", "EXISTS")
        return expression if truth else Unary("NOT", expression)
    value = Property(payload["anchor"], "p_guard", "bool")
    return Function("coalesce", [value, Literal(truth, "bool")], "bool")


def nonnull(payload, kind, rng):
    # A zero count is still non-null; the prefix need not be true or numerically positive.
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
        return work(payload, "witness", kind)
    return Function("coalesce", [Property(payload["anchor"], "p_value", kind), constant(kind)], kind)


def install(case, expression):
    case.query = replace_at(case.query, case.target, expression)
    return case


def populate_work(graph, payload, total):
    candidate = WORK_CANDIDATES[payload["work_candidate"]]
    witness = payload.get("guard_name") in {"count", "exists"} or payload.get("nonnull_name") == "count"
    populate_regions(graph, payload, total, witness=int(witness), low_keep=candidate.keep,
                     high_keep=candidate.keep, two_hops=candidate.two_hops)


def populate_regions(graph, payload, total, *, witness=0, low_keep=True, high_keep=True, two_hops=False):
    roots = graph.candidates(payload["anchor"], create=True)
    allocations = shares(total, len(roots))
    payload["anchor_count"] = len(roots)
    payload["anchor_uids"] = roots
    payload["high_allocations"] = allocations
    # Guards and witnesses apply per anchor; only the high-cost total is shared across anchors.
    for root, count in zip(roots, allocations):
        if "guard_value" in payload:
            graph.nodes[root]["properties"]["p_guard"] = payload["guard_value"]
        if payload.get("nonnull_name") == "property":
            graph.nodes[root]["properties"]["p_value"] = constant(payload["nonnull_kind"]).value
        for role, quantity, keep in (("low", 1, low_keep), ("witness", witness, True), ("high", count, high_keep)):
            start = root
            if two_hops and role != "witness":
                start = graph.node([payload[role + "_middle_label"]])
                graph.edge(root, start, payload[role + "_first_type"])
            for _ in range(quantity):
                node = graph.node([payload[role + "_label"]], keep=keep, value=1)
                graph.edge(start, node, payload[role + "_type"])
