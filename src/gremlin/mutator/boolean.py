from dataclasses import dataclass
from data_structures.ast import at
from gremlin.ast_nodes import Traversal, Step
from .operands import anchors, prepare, prefix, work, guard, install, populate, mutate


@dataclass
class BooleanPattern:
    id: str
    operator: str
    swap: bool = False

    def accepts(self, site):
        return site.node.name == "filter" and bool(anchors(site.scope))

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        decisive = guard(case.payload, self.operator == "or", rng)
        expensive, expansion = work(case.payload, "high" if self.swap else "low")
        boolean = Step(self.operator, [decisive, expensive])
        replacement = Step("filter", [Traversal(prefix(case.payload) + [boolean])])
        case.payload["variant"] = "operand_swap" if self.swap else "short_circuit"
        return install(case, replacement, boolean if self.swap else expansion)

    populate = staticmethod(populate)

    def mutate(self, case):
        if self.swap:
            # Swap the same two pure predicate subtrees; do not change either operand.
            at(case.query, case.target).args.reverse()
            return case.query
        return mutate(case)
