from dataclasses import dataclass
from cypher.ast_nodes import Binary, at
from cypher.ast_nodes.expressions import Expression
from .operands import prepare, guard, install, choose_work, populate_work


@dataclass
class BooleanPattern:
    id: str
    operator: str
    swap: bool = False
    work_kind: str = "count"

    def accepts(self, site):
        return isinstance(site.node, Expression) and site.node.kind == "bool"

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng, self.work_kind)
        if self.swap:
            case.payload["variant"] = rng.choice(("guard_swap", "predicate_swap"))
            if case.payload["variant"] == "predicate_swap":
                candidate = choose_work(case.payload, "bool", rng)
                return install(case, Binary(candidate.expression(case.payload, "low"), self.operator,
                                            candidate.expression(case.payload, "high")))
        # OR uses a true guard; AND uses a false guard, including the three-valued case.
        # OR 使用真决定项，AND 使用假决定项；另一项为空值时仍满足吸收关系。
        decisive = guard(case.payload, self.operator == "OR", rng)
        candidate = choose_work(case.payload, "bool", rng)
        case.payload["high"] = candidate.expression(case.payload, "high")
        other = case.payload["high"] if self.swap else candidate.expression(case.payload, "low")
        return install(case, Binary(decisive, self.operator, other))

    def populate(self, graph, payload, total):
        populate_work(graph, payload, total)

    def mutate(self, case):
        expression = at(case.query, case.target)
        # Symmetry swaps unchanged operands; short-circuit mutation substitutes the protected one.
        # 对称变换只交换原操作数；短路变异替换受决定项保护的操作数。
        if self.swap:
            expression.left, expression.right = expression.right, expression.left
        else:
            expression.right = case.payload["high"]
        return case.query
