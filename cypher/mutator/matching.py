import copy
from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import at
from data_structures.case import Case, symbols
from data_structures.graph import shares
from .path import node_properties


GRAPH_CANDIDATES = {
    "independent": ("selective_left", "selective_right"),
    "shared": ("left_fanout", "right_fanout"),
    "chain": ("detached_edges", "detached_fanout"),
    "optional": ("left_fanout", "right_fanout"),
}


def nodes_in_match(match):
    result = {}
    for path in match.paths:
        for node in path.nodes:
            result.setdefault(node.variable.name, node)
    return list(result.values())


@dataclass
class MatchingPattern:
    id: str
    mode: str

    def accepts(self, site):
        # This recipe replaces plain matches; it does not relocate attached predicates.
        # 此配方替换普通匹配，不通过移动附属谓词来完成交换。
        match = site.node
        if not isinstance(match, Match) or match.optional or match.where:
            return False
        if any(path.variable or any(edge.variable for edge in path.relationships) for path in match.paths):
            return False
        nodes = nodes_in_match(match)
        maximum = 2 if self.mode == "independent" else 3
        return len(nodes) <= maximum and all(n.variable.labels and not n.variable.nullable for n in nodes)

    def embed(self, query, site, seed, rng):
        case = Case(self.id, seed, query, site.path, set(symbols(query)))
        nodes = copy.deepcopy(nodes_in_match(site.node))
        # Reuse exported node variables and add private variables only for missing roles.
        # 复用对外提供的节点变量，只为缺少的局部角色增加私有变量。
        count = 2 if self.mode == "independent" else 3
        while len(nodes) < count:
            i = len(nodes)
            label = Symbol(f"p_match_label_{i}", "label")
            variable = Variable(f"p_match_{i}", "node", (label,))
            nodes.append(NodePattern(variable, [label]))
        root, a = nodes[:2]
        candidate = rng.choice(GRAPH_CANDIDATES[self.mode])
        selective = a if candidate == "selective_right" else root
        selective.properties.append(("p_anchor", Literal(1)))
        first_type, second_type = Symbol("p_left_edge", "relationship"), Symbol("p_right_edge", "relationship")

        def edge(left, right, rel, optional=False):
            return Match([PathPattern([left, right], [RelationshipPattern([rel])])], optional)

        if self.mode == "independent":
            pieces = [Match([PathPattern([root], [])]), Match([PathPattern([a], [])])]
        elif self.mode == "chain":
            pieces = [edge(root, a, first_type), edge(NodePattern(a.variable), nodes[2], second_type)]
        else:
            pieces = [edge(root, a, first_type), edge(NodePattern(root.variable), nodes[2], second_type)]
            if self.mode == "optional":
                # Keep the mandatory root outside the pair of independent optional branches.
                # 必选根节点位于两个独立可选分支之外，不参与分支交换。
                pieces = [Match([PathPattern([root], [])]),
                          edge(NodePattern(root.variable), a, first_type, True),
                          edge(NodePattern(root.variable), nodes[2], second_type, True)]
        owner_path, index = site.path[:-2], site.path[-1]
        owner = at(query, owner_path)
        owner.clauses[index:index + 1] = pieces
        case.target = owner_path
        case.payload = {"nodes": nodes, "first_type": first_type, "second_type": second_type,
                        "swap_index": index + (self.mode == "optional"), "graph_candidate": candidate}
        if self.mode == "independent":
            case.payload["small_empty"] = rng.random() < .25
            # Independent components may also be comma-separated parts of a single MATCH.
            # 独立分量也可以写成同一 MATCH 中逗号分隔的两个部分。
            case.payload["form"] = rng.choice(("clauses", "parts"))
            if case.payload["form"] == "parts":
                owner.clauses[index:index + 2] = [Match([piece.paths[0] for piece in pieces])]
                case.target = site.path
        return case

    def populate(self, graph, payload, total):
        nodes = payload["nodes"]
        root_spec, a_spec = nodes[:2]
        candidate = payload["graph_candidate"]
        if candidate == "selective_right":
            root_spec, a_spec = a_spec, root_spec
        root = graph.candidates(root_spec.variable, create=True)[0]
        graph.nodes[root]["properties"].update(node_properties(root_spec) | {"keep": True})
        graph.index(root_spec.variable.labels[0], "p_anchor")

        def new(spec):
            return graph.node(spec.variable.labels, **(node_properties(spec) | {"keep": True}))

        if self.mode == "independent":
            if payload["small_empty"]:
                # Remove the selective match, not the root node, to create an empty component.
                # 让选择性条件不再命中以构造空分量，不需要删除根节点。
                graph.nodes[root]["properties"]["p_anchor"] = 0
            for _ in range(total):
                new(a_spec)
            return
        b_spec = nodes[2]
        first, second = payload["first_type"], payload["second_type"]
        if self.mode == "chain":
            middle, target = new(a_spec), new(b_spec)
            graph.edge(root, middle, first)
            graph.edge(middle, target, second)
            # Extra right-hand chains have no left-hand connection to the selected root.
            # 新增右侧链没有连接到指定根节点的左侧关系，不能形成完整匹配。
            hub = new(a_spec) if candidate == "detached_fanout" else None
            for _ in range(total):
                graph.edge(hub if hub is not None else new(a_spec), new(b_spec), second)
            return
        if candidate == "left_fanout":
            a_spec, b_spec, first, second = b_spec, a_spec, second, first
        if self.mode == "shared":
            graph.edge(root, new(a_spec), first)
            for _ in range(total):
                graph.edge(root, new(b_spec), second)
            return
        # Four fixed roots retain all null-extension cases even at the smallest budget.
        both, narrow_only, wide_only, _ = [root] + [new(root_spec) for _ in range(3)]
        graph.edge(both, new(a_spec), first)
        graph.edge(narrow_only, new(a_spec), first)
        for anchor, quantity in zip((both, wide_only), shares(total, 2)):
            for _ in range(1 + quantity):
                graph.edge(anchor, new(b_spec), second)

    def mutate(self, case):
        # Swap two recorded clauses in place; retain their boundaries and the outer query.
        # 原位交换已记录的两条子句，保留子句边界及外层查询。
        owner = at(case.query, case.target)
        if case.payload.get("form") == "parts":
            # Swap the two comma-separated parts; the MATCH clause itself stays in place.
            # 交换逗号分隔的两个部分，MATCH 子句本身位置不变。
            owner.paths[0], owner.paths[1] = owner.paths[1], owner.paths[0]
            return case.query
        index = case.payload["swap_index"]
        owner.clauses[index], owner.clauses[index + 1] = owner.clauses[index + 1], owner.clauses[index]
        return case.query
