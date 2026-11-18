from dataclasses import dataclass
from data_structures.ast import walk
from gremlin.ast_nodes import Traversal, Step, Value
from .operands import VALUES, anchors, prepare, prefix, install, populate, mutate


@dataclass
class DemandPattern:
    id: str

    def accepts(self, site):
        if not anchors(site.scope):
            return False
        if self.id == "SC8":
            return site.node.name == "filter"
        return site.terminal and site.output.current.kind in VALUES

    def embed(self, query, site, seed, rng):
        case = prepare(query, site, self.id, seed, rng)
        expansion = Step("out", [case.payload["low_type"]])
        if self.id == "SC8":
            empty = Step("not", [Traversal([Step("identity")])])
            case.payload["variant"] = "empty_input"
            return install(case, Step("filter", [Traversal(prefix(case.payload) + [empty, expansion])]), expansion)
        kind = site.output.current.kind
        case.payload["value"] = Value(rng.choice(VALUES[kind]), kind)
        case.payload["limit"] = 0 if self.id == "SC5" else rng.randint(1, 3)
        case.payload["variant"] = "zero_limit" if self.id == "SC5" else "native_limit"
        # The global limit terminates the root traversal; only identical constants reach it.
        query.steps[site.path[1]:] = prefix(case.payload) + [expansion,
            Step("constant", [case.payload["value"]]), Step("limit", [case.payload["limit"]])]
        case.target = next(path for path, node in walk(query) if node is expansion)
        return case

    populate = staticmethod(populate)
    mutate = staticmethod(mutate)
