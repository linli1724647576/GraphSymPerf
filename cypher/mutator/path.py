import copy
from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import at, replace_at, walk
from data_structures.case import Case, symbols, Inapplicable


def node_properties(node):
    return {key: value.value for key, value in node.properties}


def observed_path_is_safe(query, variable):
    # Accept length and supported order-insensitive quantifiers; reject direct path observation.
    # 接受长度和已支持的顺序无关量词，拒绝直接观察完整路径的情况。
    for path, node in walk(query):
        if not isinstance(node, Reference) or node.variable.name != variable.name:
            continue
        parent = at(query, path[:-2]) if len(path) >= 2 else None
        if isinstance(parent, Function) and parent.name == "length":
            continue
        if isinstance(parent, Function) and parent.name == "nodes":
            owner = at(query, path[:-3]) if len(path) >= 3 else None
            if isinstance(owner, Quantifier) and path[-3] == "source":
                continue
        return False
    return True


@dataclass
class PathPatternRule:
    id: str
    bounded: bool = False

    def accepts(self, site):
        node = site.node
        return (isinstance(node, PathPattern) and len(node.nodes) == 2
                and len(node.relationships) == 1
                and node.nodes[0].variable.name != node.nodes[1].variable.name
                and all(n.variable.labels and not n.variable.nullable for n in node.nodes)
                and (not self.bounded or node.relationships[0].variable is None))

    def embed(self, query, site, seed, rng):
        original = site.node
        if original.variable and not observed_path_is_safe(query, original.variable):
            raise Inapplicable("The enclosing query observes path order")
        case = Case(self.id, seed, query, site.path, set(symbols(query)))
        left, right = copy.deepcopy(original.nodes)
        # Keep endpoint variables and constraints; add one selective anchor within the pattern.
        # 保留端点变量及约束，在 Pattern 内加入一个有选择性的锚点。
        left.properties.append(("p_anchor", Literal(1)))
        relation = Symbol("Pattern_p_path_edge", "relationship")
        maximum = rng.randint(2, 4) if self.bounded else 1
        direction = "both" if self.bounded else rng.choice(["out", "in", "both"])
        path_variable = original.variable
        if self.bounded and path_variable is None:
            path_variable = Variable("p_path", "path")
        edge = RelationshipPattern([relation], direction, 1, maximum, original.relationships[0].variable if not self.bounded else None)
        replacement = PathPattern([left, right], [edge], path_variable)
        case.query = replace_at(query, site.path, replacement)
        case.payload = {"left": left, "right": right, "relation": relation, "direction": direction, "maximum": maximum}
        candidates = ("blocked_hub", "blocked_terminals") if self.bounded else ("selected_fanout", "distractor_fanout")
        case.payload["graph_candidate"] = rng.choice(candidates)
        if self.bounded:
            match_path = site.path[:-2]
            match = at(query, match_path)
            item = Variable("p_path_item", "node")
            predicate = Quantifier("all", item, Function("nodes", [Reference(path_variable)], "list_node"),
                                   Binary(Property(item, "keep", "bool"), "=", Literal(True, "bool")))
            match.where = Binary(match.where, "AND", predicate) if match.where else predicate
        return case

    def populate(self, graph, payload, total):
        left, right = payload["left"], payload["right"]
        root = graph.candidates(left.variable, create=True)[0]
        graph.nodes[root]["properties"].update(node_properties(left) | {"keep": True})
        graph.index(left.variable.labels[0], "p_anchor")
        if not self.bounded:
            def add_target(source):
                target = graph.node(right.variable.labels, **(node_properties(right) | {"keep": True}))
                start, end = (target, source) if payload["direction"] == "in" else (source, target)
                graph.edge(start, end, payload["relation"])
            center = root
            if payload["graph_candidate"] == "distractor_fanout":
                add_target(root)
                center = graph.node(left.variable.labels, **(node_properties(left) | {"p_anchor": 0, "keep": True}))
            for _ in range(total):
                add_target(center)
            return
        middle = graph.node(["PatternPathMiddle"], keep=True)
        target = graph.node(right.variable.labels, **(node_properties(right) | {"keep": True}))
        graph.edge(root, middle, payload["relation"])
        graph.edge(middle, target, payload["relation"])
        block_at_hub = payload["graph_candidate"] == "blocked_hub"
        hub = graph.node(["PatternPathHub"], keep=not block_at_hub)
        graph.edge(root, hub, payload["relation"])
        # Both layouts keep the valid path; the N additional paths fail at the hub or their ends.
        for _ in range(total):
            previous = hub
            for step in range(payload["maximum"] - 1):
                last = step == payload["maximum"] - 2
                labels = right.variable.labels if last else ["PatternPathMiddle"]
                props = node_properties(right) if last else {}
                node = graph.node(labels, **(props | {"keep": not last or block_at_hub}))
                graph.edge(previous, node, payload["relation"])
                previous = node

    def mutate(self, case):
        # Reflect only the path syntax; do not reverse or normalize returned values.
        # 只反转路径的书写形式，不反转返回值，也不对返回值强行归一化。
        at(case.query, case.target).reverse()
        return case.query
