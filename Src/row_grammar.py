"""A bounded row grammar shared by matching and conditional mutation laws.

Productions describe syntax and interfaces only. They never assign graph
cardinalities, choose a work axis, or construct a graph state.
"""

from dataclasses import dataclass
import random

import ir


@dataclass(frozen=True)
class GeneratedRows:
    source: object
    matching_site: object
    conditional_site: object | None
    branch_path: tuple
    binding_ports: tuple = ()


class RowGrammar:
    def __init__(self, anchor, *, seed, symbol, flag, region):
        self.anchor = anchor
        self.random = random.Random(seed)
        self.symbol, self.flag, self.region = symbol, flag, region

    def fresh(self, stem, used):
        name, index = stem, 0
        while name in used:
            name = f"{stem}_{index}"
            index += 1
        used.add(name)
        return name

    def matching(self, length, *, name="chain", projection="bindings", binding="chain", optional=False):
        if type(length) is not int or not 1 <= length <= 6:
            raise ValueError("A matching production requires one to six producers")
        if projection not in {"bindings", "constant"}:
            raise ValueError("Unknown row projection interface")
        if binding not in {"chain", "shared", "fresh"} or type(optional) is not bool:
            raise ValueError("A matching production needs a binding policy and mandatory/optional interface")
        if optional and binding != "shared":
            raise ValueError("Optional branch productions require their common input binding")
        if length == 1 and projection == "constant" and binding == "chain" and not optional:
            return self.region(self.anchor, name)
        nodes, parts, used = [self.anchor], [], {self.anchor.variable}
        if binding == "shared":
            parts.append(ir.Match(self.anchor))
        elif binding == "fresh":
            scan = ir.Node(self.fresh("component", used), (self.symbol(
                f"label:{name}:scan", "identifier", (f"Gsp_{name}_scan",)),))
            nodes = [scan]
            parts.append(ir.Match(scan))
        for index in range(length):
            if binding == "fresh" and index == 0:
                continue
            port = f"{name}:{index}"
            if binding == "shared":
                root = self.anchor
            elif binding == "fresh":
                root = ir.Node(self.fresh(f"root_{index}", used), (self.symbol(
                    f"label:{port}:root", "identifier", (f"Gsp_{name}_root_{index}",)),),
                    (("id", ir.Literal(index + 1)),))
            else:
                root = nodes[-1]
            target = ir.Node(self.fresh(f"n{index + 1}", used), (self.symbol(
                f"label:{port}", "identifier", (f"GspSegment{index}" if name == "chain" else f"Gsp_{name}_{index}",)),))
            relationship = ir.Relationship(type=self.symbol(
                f"type:{port}", "identifier", (f"GSP_SEGMENT_{index}" if name == "chain" else f"EDGE_{name}_{index}",)))
            edge = ir.Edge(root, relationship, target)
            parts.append(ir.Match(edge, self.flag(target.variable, "keep", port=f"segment:{port}"), optional=optional))
            nodes.append(target)
        returns = (tuple(ir.Property(node.variable, "uid") for node in nodes) if projection == "bindings"
                   else (ir.Alias(ir.Literal(1), "c"),))
        return ir.Query(parts[0].pattern, parts[0].where, returns, matches=tuple(parts[1:]))

    def guard(self, level, kind):
        root = ir.Node(self.fresh(f"guard_root_{level}", {self.anchor.variable}), self.anchor.labels, self.anchor.properties)
        if kind == "property":
            return ir.Exists(body=ir.Query(root, self.flag(root.variable, f"enabled_{level}"),
                                           (ir.Alias(ir.Literal(1), "c"),)))
        body = self.region(root, f"guard_{level}")
        if kind == "exists":
            return ir.Exists(body=body)
        if kind != "count":
            raise ValueError("Unknown guard production")
        return ir.Comparison(ir.Count(body=body), ">", ir.Literal(
            self.symbol(f"guard_threshold:{level}", "integer", (0, 1, 2))))

    def bounded_path(self, bounds, *, projection="bindings"):
        """A path-producing row term, with an invariant node-local consumer.

        Bounds describe syntax. Neither the number of paths nor a failing
        node's position is selected by this production.
        """
        if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
                or any(type(bound) is not int for bound in bounds)
                or not 1 <= bounds[0] <= bounds[1] <= 6):
            raise ValueError("A path production needs finite bounds inside 1..6")
        if projection not in {"bindings", "constant"}:
            raise ValueError("Unknown path projection interface")
        used = {self.anchor.variable}
        endpoint = ir.Node(self.fresh("v", used), (self.symbol(
            "label:endpoint", "identifier", ("GspHigh",)),))
        path = self.fresh("p", used)
        member = self.fresh("x", used)
        edge = ir.Edge(self.anchor, ir.Relationship(type=self.symbol(
            "type:path", "identifier", ("GSP_LINK",)), min_hops=bounds[0], max_hops=bounds[1]),
            endpoint, "undirected", path_variable=path)
        predicate = ir.All(member, ir.Call("nodes", (ir.Variable(path),)),
                           self.flag(member, "keep", port="path:member"))
        returns = ((ir.Property(self.anchor.variable, "uid"), ir.Property(endpoint.variable, "uid"),
                    ir.Alias(ir.Call("length", (ir.Variable(path),)), "hops")) if projection == "bindings"
                   else (ir.Alias(ir.Literal(1), "c"),))
        return ir.Query(edge, predicate, returns)

    def expand(self, *, length, depth, guard_kind="count", branch_path=None, projection="constant",
               binding="chain", optional=False, path_bounds=None):
        if type(depth) is not int or not 0 <= depth <= 3:
            raise ValueError("A conditional production requires zero to three layers")
        if depth and projection != "constant":
            raise ValueError("Conditional branches require the shared constant-row interface")
        path = tuple(branch_path) if branch_path is not None else tuple(
            self.random.choice(("otherwise", "then")) for _ in range(max(0, depth - 1)))
        if len(path) != max(0, depth - 1) or any(branch not in ("then", "otherwise") for branch in path):
            raise ValueError("branch_path must select one child at each non-leaf conditional layer")

        def produce(level):
            if level == depth:
                leaf = (self.bounded_path(path_bounds, projection=projection) if path_bounds is not None else
                        self.matching(length, name="chain" if not depth else f"selected_{level - 1}",
                                      projection=projection, binding=binding, optional=optional))
                # A shared-input production starts with its mandatory anchor;
                # the first exchangeable producer is the following MATCH.
                site = leaf.pattern if path_bounds is not None else leaf.matches[0] if binding == "shared" else leaf
                ports = (tuple(part.pattern.right for part in ir.clauses(leaf)
                               if isinstance(part, ir.Match) and part.optional) if optional else ())
                return leaf, site, None, ports
            selected, matching, conditional, ports = produce(level + 1)
            sibling = self.matching(1, name=f"unselected_{level}", projection="constant")
            branch = path[level] if level < len(path) else "then"
            then, otherwise = (selected, sibling) if branch == "then" else (sibling, selected)
            result = ir.ConditionalQuery(self.guard(level, guard_kind), then, otherwise)
            return result, matching, conditional or result, ports

        source, matching, conditional, ports = produce(0)
        return GeneratedRows(source, matching, conditional, path, ports)
