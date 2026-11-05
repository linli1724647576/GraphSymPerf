from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import replace_at
from cypher.ast_nodes.expressions import Expression
from .operands import prepare, rows, install, populate_regions, choose_row_source, ROW_SOURCES


@dataclass
class ExistencePattern:
    id: str = "SC7"

    def accepts(self, site):
        return isinstance(site.node, Expression) and site.node.kind == "bool"

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        variant = rng.choice(["exists", "not_exists"])
        case.payload["variant"] = variant
        two_hops = choose_row_source(case.payload, rng)
        source = rows(case.payload, "low", two_hops=two_hops)
        replacement = rows(case.payload, "high", two_hops=two_hops)
        source, replacement = SubqueryExpression("EXISTS", source), SubqueryExpression("EXISTS", replacement)
        if variant == "not_exists":
            source, replacement = Unary("NOT", source), Unary("NOT", replacement)
        case.payload["replacement"] = replacement
        return install(case, source)

    def populate(self, graph, payload, total):
        roots = graph.candidates(payload["anchor"], create=True)
        # EXISTS/NOT EXISTS keeps its value only if both sides match for each eligible anchor.
        # 每个适用锚点在两侧都有匹配，EXISTS／NOT EXISTS 的取值才保持不变。
        if total < len(roots):
            from data_structures.case import Inapplicable
            raise Inapplicable("Existence needs at least one high-region witness per anchor")
        populate_regions(graph, payload, total, witness=0,
                         two_hops=ROW_SOURCES[payload["work_candidate"]])

    def mutate(self, case):
        return replace_at(case.query, case.target, case.payload["replacement"])
