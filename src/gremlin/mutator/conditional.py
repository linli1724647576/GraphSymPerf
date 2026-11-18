from dataclasses import dataclass
from gremlin.ast_nodes import Traversal, Step, Value
from .operands import VALUES, anchors, prepare, prefix, work, guard, install, populate, mutate


@dataclass
class ConditionalPattern:
    id: str
    coalesce: bool = False

    def accepts(self, site):
        return site.node.name == "map" and site.output.current.kind in VALUES and bool(anchors(site.scope))

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        kind = site.output.current.kind
        case.payload["value"] = Value(rng.choice(VALUES[kind]), kind)
        expensive, expansion = work(case.payload, "low", kind)
        chosen = Traversal([Step("constant", [case.payload["value"]])])
        if self.coalesce:
            case.payload["prefix_name"] = rng.choice(["constant", "property"])
            if case.payload["prefix_name"] == "property":
                chosen = Traversal([Step("values", ["p_prefix", kind])])
            # Productivity is independent of truth: constant(false) and constant(0) emit values.
            branch = Step("coalesce", [chosen, expensive])
            case.payload["variant"] = "first_productive"
        else:
            truth = rng.choice([True, False])
            decisive = guard(case.payload, truth, rng)
            branch = Step("choose", [decisive, chosen if truth else expensive, expensive if truth else chosen])
            case.payload["variant"] = "unselected_branch"
        replacement = Step("map", [Traversal(prefix(case.payload) + [branch])])
        return install(case, replacement, expansion)

    populate = staticmethod(populate)
    mutate = staticmethod(mutate)
