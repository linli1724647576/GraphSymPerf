from dataclasses import dataclass
from data_structures.ast import Symbol, at, walk
from data_structures.graph import shares
from data_structures.case import Case, symbols
from gremlin.ast_nodes import Traversal, Step, Value


GRAPH_CANDIDATES = {
    "independent": ("selective_left", "selective_right"),
    "shared": ("left_fanout", "right_fanout"),
    "chain": ("detached_edges", "detached_fanout"),
    "optional": ("left_fanout", "right_fanout"),
    "edge": ("root_fanout", "target_fanout"),
    "walk": ("root_hub", "target_hub"),
}
ROOT = "p_match_u"
# Stands in for the null extension of an optional branch; never a stored property value.
MISSING = "p_match_missing"
REVERSE = {"out": "in", "in": "out", "both": "both"}


def anchor_filter():
    return Step("has", ["p_anchor", Value(1, "int")])


def edge_filter(start, end, payload):
    """SY3/SY4 filter pattern from start to end; where() keeps the start label independent of order."""
    # An end-label form makes TinkerPop compute the start label from pattern order, so a reversed
    # pattern listed first would silently bind the wrong label; where() patterns export no end label.
    relation = payload["second_type"]
    if payload["bound"]:
        inner = [Step("repeat", [Traversal([Step("both", [relation])])]), Step("emit"),
                 Step("times", [Value(payload["bound"], "int")])]
    else:
        direction = payload["direction"] if start == ROOT else REVERSE[payload["direction"]]
        inner = [Step(direction, [relation])]
    return Traversal([Step("as", [start]), Step("where", [Traversal(inner + [Step("as", [end])])])])


@dataclass
class MatchingPattern:
    """SY3-SY8 for Gremlin: two patterns of one declarative match() step.

    SY5-SY8 exchange the two patterns; SY3/SY4 rewrite the filter pattern from its other endpoint.
    """
    id: str
    mode: str

    def accepts(self, site):
        # Insert after the root V().hasLabel(X): every X node reaches match(), including the workload root.
        current = site.output.current
        return (site.path == ("steps", 1) and site.node.name == "hasLabel"
                and current.kind == "node" and bool(current.labels) and not current.nullable)

    def embed(self, query, site, seed, rng):
        case = Case(self.id, seed, query, site.path, set(symbols(query)))
        anchor = site.output.current
        labels = [Symbol(f"Pattern_p_match_label_{i}", "label") for i in range(2)]
        first, second = Symbol("Pattern_p_left_edge", "relationship"), Symbol("Pattern_p_right_edge", "relationship")
        candidate = rng.choice(GRAPH_CANDIDATES[self.mode])

        def pattern(start, steps, end):
            return Traversal([Step("as", [start])] + steps + [Step("as", [end])])

        if self.mode == "independent":
            # Two mid-traversal lookups share only the computed start label.
            selective = 0 if candidate == "selective_left" else 1
            patterns = [pattern(ROOT, [Step("V"), Step("hasLabel", [label])]
                                + ([anchor_filter()] if i == selective else []), f"p_match_{i}")
                        for i, label in enumerate(labels)]
        elif self.mode == "shared":
            patterns = [pattern(ROOT, [Step("out", [first]), Step("hasLabel", [labels[0]])], "p_match_0"),
                        pattern(ROOT, [Step("out", [second]), Step("hasLabel", [labels[1]])], "p_match_1")]
        elif self.mode == "chain":
            patterns = [pattern(ROOT, [Step("out", [first]), Step("hasLabel", [labels[0]])], "p_match_0"),
                        pattern("p_match_0", [Step("out", [second]), Step("hasLabel", [labels[1]])], "p_match_1")]
        elif self.mode == "optional":
            # coalesce() with a sentinel keeps a root whose branch is empty, as OPTIONAL MATCH does.
            patterns = [pattern(ROOT, [Step("coalesce", [Traversal([Step("out", [edge]), Step("hasLabel", [label])]),
                                                         Traversal([Step("constant", [Value(MISSING, "string")])])])],
                                f"p_match_{i}")
                        for i, (edge, label) in enumerate(zip((first, second), labels))]
        else:
            # The first pattern binds one target; the second only tests the edge or walk between the two.
            payload = {"second_type": second, "direction": "both" if self.mode == "walk" else rng.choice(("out", "in", "both")),
                       "bound": rng.randint(2, 3) if self.mode == "walk" else None}
            patterns = [pattern(ROOT, [Step("out", [first]), Step("hasLabel", [labels[0]])], "p_match_0"),
                        edge_filter(ROOT, "p_match_0", payload)]
        match = Step("match", patterns)
        index = site.path[1] + 1
        # The selected anchor keeps one workload root; select() restores the host's current node.
        query.steps[index:index] = [anchor_filter(), match, Step("select", [ROOT])]
        case.target = next(path for path, node in walk(query) if node is match)
        case.payload = {"anchor": anchor, "labels": labels, "first_type": first, "second_type": second,
                        "graph_candidate": candidate}
        if self.mode == "independent":
            case.payload["small_empty"] = rng.random() < .25
        if self.mode in {"edge", "walk"}:
            case.payload.update(direction=payload["direction"], bound=payload["bound"])
        return case

    def populate(self, graph, payload, total):
        candidate, (left, right) = payload["graph_candidate"], payload["labels"]
        root = graph.candidates(payload["anchor"], create=True)[0]
        graph.nodes[root]["properties"].update(p_anchor=1, keep=True)
        # HugeGraph folds V().hasLabel(X).has(p_anchor) into the start lookup and requires this index.
        graph.index(graph.nodes[root]["labels"][0], "p_anchor")
        payload.update(anchor_uids=[root], anchor_count=1, high_allocations=[total])
        first, second = payload["first_type"], payload["second_type"]
        if self.mode == "independent":
            selective, wide = (left, right) if candidate == "selective_left" else (right, left)
            graph.index(selective, "p_anchor")
            # The selective side has one node, or none when the whole match is empty.
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
        if self.mode == "optional":
            narrow, wide = ((left, first), (right, second)) if candidate == "right_fanout" else ((right, second), (left, first))
            # Four workload roots cover both, narrow-only, wide-only and neither branch matching.
            roots = [root] + [graph.node(graph.nodes[root]["labels"], keep=True, p_anchor=1) for _ in range(3)]
            both, narrow_only, wide_only, _ = roots
            payload.update(anchor_uids=roots, anchor_count=4, high_allocations=shares(total, 2) + [0, 0])
            for anchor in (both, narrow_only):
                graph.edge(anchor, graph.node([narrow[0]], keep=True), narrow[1])
            for anchor, quantity in zip((both, wide_only), shares(total, 2)):
                for _ in range(1 + quantity):
                    graph.edge(anchor, graph.node([wide[0]], keep=True), wide[1])
            return
        if self.mode == "edge":
            self.populate_edge(graph, payload, root, total)
            return
        if self.mode == "walk":
            self.populate_walk(graph, payload, root, total)
            return
        middle = graph.node([left], keep=True)
        graph.edge(root, middle, first)
        graph.edge(middle, graph.node([right], keep=True), second)
        # Detached right-stage edges cannot join the selected root's left stage.
        hub = graph.node([left], keep=True) if candidate == "detached_fanout" else None
        for _ in range(total):
            start = hub if hub is not None else graph.node([left], keep=True)
            graph.edge(start, graph.node([right], keep=True), second)

    @staticmethod
    def populate_edge(graph, payload, root, total):
        (target_label, other_label), relation = payload["labels"], payload["second_type"]
        target = graph.node([target_label], keep=True)
        graph.edge(root, target, payload["first_type"])

        def link(source, other):
            # The edge runs in the pattern's direction from source; "both" uses the outgoing edge.
            graph.edge(*((other, source) if payload["direction"] == "in" else (source, other)), relation)

        # Create the endpoint's fan-out before the matching edge.
        if payload["graph_candidate"] == "root_fanout":
            for _ in range(total):
                link(root, graph.node([other_label], keep=True))
        else:
            for _ in range(total):
                other = graph.node(graph.nodes[root]["labels"], keep=True, p_anchor=0)
                link(other, target)
        link(root, target)

    @staticmethod
    def populate_walk(graph, payload, root, total):
        (target_label, other_label), relation = payload["labels"], payload["second_type"]
        target = graph.node([target_label], keep=True)
        graph.edge(root, target, payload["first_type"])
        # A hub of N dead-end leaves hangs off one endpoint; leaves have degree 1, so walks of at most
        # three steps stay linear in N.
        side = root if payload["graph_candidate"] == "root_hub" else target
        hub = graph.node([other_label], keep=True)
        graph.edge(side, hub, relation)
        for _ in range(total):
            graph.edge(hub, graph.node([other_label], keep=True), relation)
        middle = graph.node([other_label], keep=True)
        graph.edge(root, middle, relation)
        graph.edge(middle, target, relation)

    def mutate(self, case):
        match = at(case.query, case.target)
        if self.mode in {"edge", "walk"}:
            match.args[1] = edge_filter("p_match_0", ROOT, case.payload)
        else:
            match.args.reverse()
        return case.query
