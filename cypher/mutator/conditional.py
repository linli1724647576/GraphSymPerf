from dataclasses import dataclass
from cypher.ast_nodes import *
from data_structures.ast import at
from cypher.ast_nodes.expressions import Expression
from .operands import prepare, guard, choose_work, nonnull, constant, install, populate_work, SCALAR_TYPES


@dataclass
class ConditionalPattern:
    id: str
    coalesce: bool = False
    work_kind: str = "count"

    def accepts(self, site):
        kinds = {"bool"} if self.coalesce and self.work_kind == "pattern_list" else SCALAR_TYPES
        return isinstance(site.node, Expression) and site.node.kind in kinds

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng, self.work_kind)
        kind = site.node.kind
        if self.coalesce:
            case.payload["variant"] = "coalesce"
        elif self.work_kind == "pattern_list" and kind != "bool":
            # A Boolean work item fits a WHEN condition even when the CASE result is scalar.
            # 即使 CASE 返回其他标量类型，布尔工作项仍可放入 WHEN 条件。
            case.payload["variant"] = "later_condition"
        else:
            case.payload["variant"] = rng.choice(["unselected_result", "later_condition"])
        result_kind = "bool" if case.payload["variant"] == "later_condition" else kind
        candidate = choose_work(case.payload, result_kind, rng)
        low = candidate.expression(case.payload, "low", result_kind)
        case.payload["high"] = candidate.expression(case.payload, "high", result_kind)
        if self.coalesce:
            expression = Function("coalesce", [Literal(None, kind), nonnull(case.payload, kind, rng), low], kind)
        else:
            decisive = guard(case.payload, True, rng)
            if case.payload["variant"] == "later_condition":
                # The first true branch also makes later WHEN conditions unnecessary.
                # 首个真分支确定结果后，后续 WHEN 条件也无需参与求值。
                expression = CaseExpression([(decisive, constant(kind)),
                                             (low, constant(kind))], constant(kind))
            else:
                expression = CaseExpression([(decisive, constant(kind))], low)
        return install(case, expression)

    def populate(self, graph, payload, total):
        populate_work(graph, payload, total)

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
