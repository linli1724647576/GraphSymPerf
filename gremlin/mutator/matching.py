from dataclasses import dataclass
from data_structures.ast import Symbol, at, walk
from data_structures.case import Case, symbols
from gremlin.ast_nodes import Traversal, Step, Value


GRAPH_CANDIDATES = {
    "independent": ("selective_left", "selective_right"),
    "shared": ("left_fanout", "right_fanout"),
    "chain": ("detached_edges", "detached_fanout"),
}
ROOT = "p_match_u"


def anchor_filter():
    return Step("has", ["p_anchor", Value(1, "int")])


@dataclass
class MatchingPattern:
    """SY5-SY7 for Gremlin: exchange two patterns of one declarative match() step."""
    id: str
    mode: str

    def accepts(self, site):
        # Insert after the root V().hasLabel(X): every X node reaches match(), including the workload root.
        # 插在根部 V().hasLabel(X) 之后，所有 X 节点（含工作根）都会进入 match()。
        current = site.output.current
        return (site.path == ("steps", 1) and site.node.name == "hasLabel"
                and current.kind == "node" and bool(current.labels) and not current.nullable)

    def embed(self, query, site, seed, rng):
        case = Case(self.id, seed, query, site.path, set(symbols(query)))
        anchor = site.output.current
        labels = [Symbol(f"p_match_label_{i}", "label") for i in range(2)]
        first, second = Symbol("p_left_edge", "relationship"), Symbol("p_right_edge", "relationship")
        candidate = rng.choice(GRAPH_CANDIDATES[self.mode])

        def pattern(start, steps, end):
            return Traversal([Step("as", [start])] + steps + [Step("as", [end])])

        if self.mode == "independent":
            # Two mid-traversal lookups share only the computed start label.
            # 两个中途 V() 查找只共享计算出的起始标签，彼此独立。
            selective = 0 if candidate == "selective_left" else 1
            patterns = [pattern(ROOT, [Step("V"), Step("hasLabel", [label])]
                                + ([anchor_filter()] if i == selective else []), f"p_match_{i}")
                        for i, label in enumerate(labels)]
        elif self.mode == "shared":
            patterns = [pattern(ROOT, [Step("out", [first]), Step("hasLabel", [labels[0]])], "p_match_0"),
                        pattern(ROOT, [Step("out", [second]), Step("hasLabel", [labels[1]])], "p_match_1")]
        else:
            patterns = [pattern(ROOT, [Step("out", [first]), Step("hasLabel", [labels[0]])], "p_match_0"),
                        pattern("p_match_0", [Step("out", [second]), Step("hasLabel", [labels[1]])], "p_match_1")]
        match = Step("match", patterns)
        index = site.path[1] + 1
        # The selected anchor keeps one workload root; select() restores the host's current node.
        # 锚点过滤只保留一个工作根，select() 恢复宿主遍历的当前节点。
        query.steps[index:index] = [anchor_filter(), match, Step("select", [ROOT])]
        case.target = next(path for path, node in walk(query) if node is match)
        case.payload = {"anchor": anchor, "labels": labels, "first_type": first, "second_type": second,
                        "graph_candidate": candidate}
        if self.mode == "independent":
            case.payload["small_empty"] = rng.random() < .25
        return case

    def populate(self, graph, payload, total):
        candidate, (left, right) = payload["graph_candidate"], payload["labels"]
        root = graph.candidates(payload["anchor"], create=True)[0]
        graph.nodes[root]["properties"].update(p_anchor=1, keep=True)
        # HugeGraph folds V().hasLabel(X).has(p_anchor) into the start lookup and requires this index.
        # HugeGraph 把 V().hasLabel(X).has(p_anchor) 折叠进起始查找，需要该二级索引。
        graph.index(graph.nodes[root]["labels"][0], "p_anchor")
        payload.update(anchor_uids=[root], anchor_count=1, high_allocations=[total])
        first, second = payload["first_type"], payload["second_type"]
        if self.mode == "independent":
            selective, wide = (left, right) if candidate == "selective_left" else (right, left)
            graph.index(selective, "p_anchor")
            # The selective side has one node, or none when the whole match is empty.
            # 选择性一侧只有一个节点；small_empty 时为零个，整个 match 为空。
            graph.node([selective], keep=True, p_anchor=0 if payload["small_empty"] else 1)
            for _ in range(total):
                graph.node([wide], keep=True)
            return
        if self.mode == "shared":
            narrow, wide = ((right, second), (left, first)) if candidate == "left_fanout" else ((left, first), (right, second))
            graph.edge(root, graph.node([narrow[0]], keep=True), narrow[1])
            for _ in range(total):
                graph.edge(root, graph.node([wide[0]], keep=True), wide[1])
            return
        middle = graph.node([left], keep=True)
        graph.edge(root, middle, first)
        graph.edge(middle, graph.node([right], keep=True), second)
        # Detached right-stage edges cannot join the selected root's left stage.
        # 脱离的右段边无法接上选定根的左段，只增加从右段出发时的工作量。
        hub = graph.node([left], keep=True) if candidate == "detached_fanout" else None
        for _ in range(total):
            start = hub if hub is not None else graph.node([left], keep=True)
            graph.edge(start, graph.node([right], keep=True), second)

    def mutate(self, case):
        at(case.query, case.target).args.reverse()
        return case.query
