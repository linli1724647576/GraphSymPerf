from dataclasses import dataclass
from data_structures.case import Inapplicable
from data_structures.graph import shares
from gremlin.ast_nodes import Traversal, Step
from .operands import anchors, prepare, prefix, work, install, mutate


@dataclass
class ExistencePattern:
    id: str = "SC7"

    def accepts(self, site):
        return site.node.name == "filter" and bool(anchors(site.scope))

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        case.payload["variant"] = rng.choice(["exists", "not_exists"])
        expensive, expansion = work(case.payload, "low")
        # A traversal used as a filter is an existence test; not() negates it.
        # 作为过滤条件的子遍历就是存在性判断；not() 对其取反。
        body = expensive.steps if case.payload["variant"] == "exists" else [Step("not", [expensive])]
        return install(case, Step("filter", [Traversal(prefix(case.payload) + body)]), expansion)

    def populate(self, graph, payload, total):
        roots = graph.candidates(payload["anchor"], create=True)
        # The existence result is fixed only if both regions match on every eligible anchor.
        # 每个适用锚点在两侧区域都有匹配，存在性判断的结果才保持不变。
        if total < len(roots):
            raise Inapplicable("Existence needs at least one high-region witness per anchor")
        allocation = shares(total, len(roots))
        payload.update(anchor_uids=roots, anchor_count=len(roots), high_allocations=allocation)
        for root, count in zip(roots, allocation):
            for role, quantity in (("low", 1), ("high", count)):
                for _ in range(quantity):
                    node = graph.node([payload[role + "_label"]], keep=True)
                    graph.edge(root, node, payload[role + "_type"])

    mutate = staticmethod(mutate)
