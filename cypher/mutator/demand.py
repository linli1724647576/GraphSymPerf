from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import at, replace_at
from data_structures.graph import shares
from .operands import prepare, rows, install, populate_regions, choose_row_source, ROW_SOURCES, SCALAR_TYPES


@dataclass
class DemandPattern:
    id: str
    mode: str

    def accepts(self, site):
        if self.mode == "native_limit":
            return (len(site.path) == 2 and site.path[0] == "clauses"
                    and isinstance(site.node, Projection) and site.node.keyword == "RETURN"
                    and all(item.alias.kind in SCALAR_TYPES for item in site.node.items))
        return isinstance(site.node, Query) and all(v.kind in SCALAR_TYPES for v in site.node.output())

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng, "native_limit" if self.mode == "native_limit" else "count")
        # The native LIMIT pair shares the whole path up to its terminal edge.
        roles = ("low",) if self.mode == "native_limit" else ("low", "high")
        two_hops = choose_row_source(case.payload, rng, roles)
        if self.mode == "native_limit":
            outputs = [item.alias for item in site.node.items]
            tail = rows(case.payload, "low", outputs, predicate=False, two_hops=two_hops)
            case.payload["limit"] = rng.randint(1, 3)
            case.payload["variant"] = "native_limit"
            tail.clauses[-1].limit = Literal(case.payload["limit"])
            index = site.path[1]
            # Preserve the random prefix; only the final projection becomes MATCH + RETURN LIMIT.
            # 保留随机前缀，仅将最终投影替换为 MATCH 与 RETURN LIMIT。
            query.clauses[index:] = tail.clauses
            last_edge = len(tail.clauses[0].paths[0].relationships) - 1
            case.target = ("clauses", index, "paths", 0, "relationships", last_edge)
            return case
        outputs = site.node.output()
        low = rows(case.payload, "low", outputs, two_hops=two_hops)
        high = rows(case.payload, "high", outputs, two_hops=two_hops)
        case.payload["limit"] = rng.randint(1, 3)
        if self.mode == "zero":
            # Zero demand is attached to both complete branch queries, not to the graph size.
            # 两个分支查询都保留零输出需求；图中的候选数量仍可不同。
            low.clauses[-1].limit = Literal(0)
            high.clauses[-1].limit = Literal(0)
        elif self.mode == "bounded":
            witness = rows(case.payload, "witness", outputs)
            low = self.limit_union(case.payload, witness, low, outputs)
            high = self.limit_union(case.payload, witness, high, outputs)
        else:
            # The empty input stays mandatory; no aggregate may create a row from it.
            # 空输入必须保留为必选输入，不能插入会从空输入产生行的聚合。
            empty_var = Variable("p_empty", "node", (Symbol("Pattern_p_empty", "label"),))
            empty = Match([PathPattern([NodePattern(empty_var, list(empty_var.labels))], [])],
                          where=Literal(False, "bool"))
            low.clauses.insert(0, empty)
            high.clauses.insert(0, empty)
            case.payload["empty_label"] = empty_var.labels[0]
        case.payload["replacement"] = high
        return install(case, low)

    def limit_union(self, payload, witness, branch, outputs):
        # Limit the combined stream, not an individual UNION branch.
        # LIMIT 作用于合并后的结果流，而非某一个 UNION 分支。
        imports = [payload["anchor"]] if payload["correlated"] else []
        return Query([Call(imports, Union([witness, branch])),
                      Projection("RETURN", [ProjectionItem(Reference(v), v) for v in outputs], limit=Literal(payload["limit"]))])

    def populate(self, graph, payload, total):
        # Bounded demand needs n constant witness rows per correlated input anchor.
        # 有限需求模式需要为每个相关输入锚点提供 n 行常量见证。
        two_hops = ROW_SOURCES[payload["work_candidate"]]
        if self.mode == "native_limit":
            roots = graph.candidates(payload["anchor"], create=True)
            allocations = shares(total, len(roots))
            payload.update(anchor_uids=roots, high_allocations=allocations, anchor_count=len(roots))
            for root, count in zip(roots, allocations):
                start = root
                if two_hops:
                    start = graph.node([payload["low_middle_label"]])
                    graph.edge(root, start, payload["low_first_type"])
                # Shared witnesses supply k rows on both sides; only HIGH gets the expansion.
                # 两侧共用 k 个见证节点，只有 HIGH 关系获得额外扩张节点。
                for _ in range(payload["limit"]):
                    node = graph.node([payload["low_label"]], keep=True, value=1)
                    graph.edge(start, node, payload["low_type"])
                    graph.edge(start, node, payload["high_type"])
                for _ in range(count):
                    node = graph.node([payload["low_label"]], keep=True, value=1)
                    graph.edge(start, node, payload["high_type"])
            return
        populate_regions(graph, payload, total, witness=payload["limit"] if self.mode == "bounded" else 0,
                         two_hops=two_hops)
        if self.mode == "empty":
            graph.node([payload["empty_label"]], keep=True)

    def mutate(self, case):
        if self.mode == "native_limit":
            at(case.query, case.target).types = [case.payload["high_type"]]
            return case.query
        return replace_at(case.query, case.target, case.payload["replacement"])
