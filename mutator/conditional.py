from dataclasses import dataclass
from ast_nodes import *
from ast_nodes.base import at
from ast_nodes.expressions import Expression
from .operands import prepare, guard, work, nonnull, constant, install, populate_regions, SCALAR_TYPES


@dataclass
class ConditionalPattern:
    id: str
    coalesce: bool = False

    def accepts(self, site):
        return isinstance(site.node, Expression) and site.node.kind in SCALAR_TYPES

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        kind = site.node.kind
        low = work(case.payload, "low", kind)
        case.payload["high"] = work(case.payload, "high", kind)
        case.payload["variant"] = "coalesce" if self.coalesce else rng.choice(["unselected_result", "later_condition"])
        if self.coalesce:
            expression = Function("coalesce", [Literal(None, kind), nonnull(case.payload, kind, rng), low], kind)
        else:
            decisive = guard(case.payload, True, rng)
            if case.payload["variant"] == "later_condition":
                # The first true branch also makes later WHEN conditions unnecessary.
                # 首个真分支确定结果后，后续 WHEN 条件也无需参与求值。
                case.payload["high"] = work(case.payload, "high", "bool")
                expression = CaseExpression([(decisive, constant(kind)),
                                             (work(case.payload, "low", "bool"), constant(kind))], constant(kind))
            else:
                expression = CaseExpression([(decisive, constant(kind))], low)
        return install(case, expression)

    def populate(self, graph, payload, total):
        populate_regions(graph, payload, total)

    def mutate(self, case):
        expression = at(case.query, case.target)
        # Keep the selected result or non-null prefix intact while increasing unused work.
        # 增加未使用部分的潜在工作量，保持已选结果或非空前缀不变。
        if self.coalesce:
            expression.arguments[-1] = case.payload["high"]
        elif case.payload["variant"] == "later_condition":
            expression.branches[1] = (case.payload["high"], expression.branches[1][1])
        else:
            expression.otherwise = case.payload["high"]
        return case.query
