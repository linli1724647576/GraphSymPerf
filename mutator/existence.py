from dataclasses import dataclass
from ast_nodes import *
from ast_nodes.base import replace_at
from ast_nodes.expressions import Expression
from .operands import prepare, rows, install, populate_regions


@dataclass
class ExistencePattern:
    id: str = "S7"

    def accepts(self, site):
        return isinstance(site.node, Expression) and site.node.kind == "bool"

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        variant = rng.choice(["exists", "not_exists", "union_witness"])
        case.payload["variant"] = variant
        source, replacement = rows(case.payload, "low"), rows(case.payload, "high")
        if variant == "union_witness":
            # An unchanged witness determines EXISTS even if the replacement branch is empty.
            # 固定见证已经决定 EXISTS 的结果，即使替换分支为空也不改变结论。
            witness = rows(case.payload, "witness")
            source, replacement = Union([witness, source]), Union([witness, replacement])
        source, replacement = SubqueryExpression("EXISTS", source), SubqueryExpression("EXISTS", replacement)
        if variant == "not_exists":
            source, replacement = Unary("NOT", source), Unary("NOT", replacement)
        case.payload["replacement"] = replacement
        return install(case, source)

    def populate(self, graph, payload, total):
        roots = graph.candidates(payload["anchor"], create=True)
        # Direct EXISTS/NOT EXISTS requires a match on both sides for each eligible anchor.
        # 直接存在性及否定存在性变体要求每个适用锚点在两侧都有匹配。
        if payload["variant"] != "union_witness" and total < len(roots):
            from data_structures.case import Inapplicable
            raise Inapplicable("Direct existence needs at least one high-region witness per anchor")
        populate_regions(graph, payload, total, witness=1 if payload["variant"] == "union_witness" else 0)

    def mutate(self, case):
        return replace_at(case.query, case.target, case.payload["replacement"])
