from dataclasses import dataclass
from ast_nodes import *
from ast_nodes.base import replace_at
from .operands import prepare, rows, install, populate_regions, SCALAR_TYPES


@dataclass
class DemandPattern:
    id: str
    mode: str

    def accepts(self, site):
        return isinstance(site.node, Query) and all(v.kind in SCALAR_TYPES for v in site.node.output())

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        outputs = site.node.output()
        low = rows(case.payload, "low", outputs)
        high = rows(case.payload, "high", outputs)
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
            empty_var = Variable("p_empty", "node", (Symbol("p_empty", "label"),))
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
        populate_regions(graph, payload, total, witness=payload["limit"] if self.mode == "bounded" else 0)
        if self.mode == "empty":
            graph.node([payload["empty_label"]], keep=True)

    def mutate(self, case):
        return replace_at(case.query, case.target, case.payload["replacement"])
