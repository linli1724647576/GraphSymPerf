"""Bounded, semantics-driven population for supported graph AST components.

The constructor never dispatches on a test-pattern ID or a wrapper combination.
It solves a small finite model of the *whole* query, then grows only families
excluded by a protected local filter or node-local ALL. Unsupported syntax and
unsatisfied demands are explicit errors; this is not a general Cypher solver.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from collections import Counter
from itertools import product
import json
import random
from typing import Any

from ir import (Alias, All, Boolean, Call, Case, Comparison, Count, Edge, Exists,
                Literal, Match, Negation, Node, Property, Query, Variable,
                UnionAll, projection_names, validate_query)
from state import GraphSpec, render_state_checks
from dependencies import analyze_dependencies, find_p3_sites


class PopulationError(ValueError):
    """The requested state cannot be proved within the supported fragment."""


@dataclass(frozen=True)
class PopulationResult:
    spec: GraphSpec
    expected_rows: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    constraints: dict[str, Any]
    evidence: dict[str, Any]

    @property
    def graph_spec(self) -> GraphSpec:
        return self.spec

    @property
    def setup(self) -> list[str]:
        return render_setup(self.spec)


@dataclass(frozen=True)
class MatchDemand:
    """A quantity obligation attached to an actual forest Edge occurrence.

    Forest addresses start with ('query',) or ('alternatives', i). role_atom
    identifies a symbolic role for evidence; it never selects a graph recipe.
    candidate_kind constrains the actual failed candidate: right excludes a
    fresh right endpoint, while right_scalar preserves its labels and fixed
    map and requires its own scalar predicate to reject it.
    """
    address: tuple[str | int, ...]
    role_atom: str
    min_matches: int = 0
    exact_matches: int | None = None
    candidate_matches: int | None = None
    scale_matches: bool = False
    relationship_type: str | None = None
    candidate_kind: str | None = None


@dataclass(frozen=True)
class PathDemand:
    """Length and three-valued ALL requirements on an actual bounded atom.

    ``all_values`` contains (forest ALL address, TRUE/FALSE/NULL) pairs.  An
    empty tuple leaves the quantifier value unconstrained; NULL is never a
    synonym for an absent requirement.
    """
    address: tuple[str | int, ...]
    min_length: int | None = None
    max_length: int | None = None
    all_values: tuple = ()


@dataclass(frozen=True)
class ValueDemand:
    """A scalar requirement at a lexical expression address in the forest."""
    address: tuple[str | int, ...]
    value: Any = None
    relation: str = "equal"


@dataclass(frozen=True)
class EntityBindingDemand:
    """Observe an actual Node definition/output or its later input binding.

    A new definition is observed after MATCH, a reused entity before MATCH.
    The row domain must be nonempty; a physical vertex alone is not a witness.
    """
    address: tuple[str | int, ...]
    relation: str = "nonnull"


@dataclass(frozen=True)
class InversePair:
    """Every forward relationship has a reverse on the same two vertices."""
    forward_type: str
    reverse_type: str
    pair_key: str | None = None


@dataclass(frozen=True)
class BindingRelation:
    """Equality/separation of physical node domains at two Node addresses."""
    left_address: tuple[str | int, ...]
    right_address: tuple[str | int, ...]
    equal: bool = True


def _walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from _walk(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk(item)


def _located(value, address=()):
    """Lexical addresses remain distinct even when immutable ASTs are reused."""
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from _located(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, (tuple, list)):
        for i, item in enumerate(value):
            yield from _located(item, (*address, i))


def _steps(query):
    pipeline = getattr(query, "pipeline", ())
    if pipeline:
        return pipeline
    return ((Match(query.pattern, query.where), *query.matches)
            if query.pattern is not None else ())


def _subquery(expression):
    body = getattr(expression, "body", None)
    if body is not None:
        return body
    if expression.pattern is None:
        raise PopulationError("A subquery requires a body or a pattern")
    return Query(expression.pattern, expression.where, (Alias(Literal(1), "row"),))


@dataclass(frozen=True)
class _Vertex:
    uid: int


@dataclass(frozen=True)
class _Path:
    vertices: tuple[int, ...]


@dataclass(frozen=True)
class _Relation:
    index: int


def _binding_evidence(value):
    if isinstance(value, _Vertex):
        return value.uid
    if isinstance(value, _Relation):
        # Parallel edges may share endpoints and properties. Their position
        # in the accompanying validation graph preserves physical identity.
        return {"kind": "relationship", "index": value.index}
    return value


def _compare(left, operator, right):
    if operator == "IS NULL":
        return left is None
    if operator == "IS NOT NULL":
        return left is not None
    if left is None or right is None:
        return None
    if operator == "=":
        numeric = type(left) in (int, float) and type(right) in (int, float)
        return (type(left) is type(right) or numeric) and left == right
    if operator == "<>":
        return not _compare(left, "=", right)
    if type(left) is bool or type(right) is bool:
        raise PopulationError("Boolean ordering is unsupported")
    return {"<": lambda: left < right, "<=": lambda: left <= right,
            ">": lambda: left > right, ">=": lambda: left >= right}[operator]()


def _logical(operator, values):
    if any(type(value) is not bool and value is not None for value in values):
        raise PopulationError("A boolean expression did not produce BOOLEAN/NULL")
    decisive = False if operator == "AND" else True
    if any(value is decisive for value in values):
        return decisive
    return None if None in values else not decisive


class _Interpreter:
    """Independent concrete bag interpreter used only on bounded proof models."""
    def __init__(self, spec):
        self.nodes = {node["uid"]: node for node in spec.iter_nodes()}
        self.edges = list(spec.iter_edges())
        self.subquery_outcomes = []

    def expression(self, value, env):
        if isinstance(value, Literal):
            return value.value
        if isinstance(value, Variable):
            if value.name not in env:
                raise PopulationError(f"Unbound value in population interpreter: {value.name}")
            return env[value.name]
        if isinstance(value, Property):
            vertex = env.get(value.variable)
            if vertex is None:
                return None
            if isinstance(vertex, _Relation):
                return self.edges[vertex.index]["properties"].get(value.key)
            if not isinstance(vertex, _Vertex):
                raise PopulationError("Property access requires a node binding")
            return self.nodes[vertex.uid]["properties"].get(value.key)
        if isinstance(value, Comparison):
            return _compare(self.expression(value.left, env), value.operator,
                            self.expression(value.right, env))
        if isinstance(value, Boolean):
            return _logical(value.operator, [self.expression(part, env) for part in value.terms])
        if isinstance(value, Negation):
            result = self.expression(value.expression, env)
            return None if result is None else not result
        if isinstance(value, Case):
            branch = value.then if self.expression(value.condition, env) is True else value.otherwise
            return self.expression(branch, env)
        if isinstance(value, (Count, Exists)):
            rows = self.run(_subquery(value), env)
            self.subquery_outcomes.append({"operator": type(value).__name__, "rows": len(rows),
                                           "bindings": {key: _binding_evidence(item)
                                                        for key, item in env.items()
                                                        if not isinstance(item, _Path)}})
            return len(rows) if isinstance(value, Count) else bool(rows)
        if isinstance(value, All):
            items = self.expression(value.items, env)
            if items is None:
                return None
            return _logical("AND", [self.expression(value.predicate, env | {value.variable: item})
                                    for item in items])
        if isinstance(value, Call):
            args = [self.expression(part, env) for part in value.args]
            if value.name == "coalesce":
                return next((arg for arg in args if arg is not None), None)
            if value.name == "nodes":
                return [_Vertex(uid) for uid in args[0].vertices]
            if value.name == "length":
                return len(args[0].vertices) - 1
            if value.name == "size":
                return None if args[0] is None else len(args[0])
        raise PopulationError(f"Unsupported population expression: {type(value).__name__}")

    def _node_matches(self, pattern, uid, env):
        node = self.nodes[uid]
        existing = env.get(pattern.variable)
        return ((pattern.variable not in env or existing == _Vertex(uid))
                and set(pattern.labels).issubset(node["labels"])
                and all(_compare(node["properties"].get(key), "=", val.value) is True
                        for key, val in pattern.properties))

    def _match(self, pattern, env):
        if isinstance(pattern, Node):
            for uid in self.nodes:
                if self._node_matches(pattern, uid, env):
                    yield env | {pattern.variable: _Vertex(uid)}
            return
        rel = pattern.relationship
        lower, upper = rel.min_hops, rel.max_hops
        if lower is None and upper is None:
            for edge_index, edge in enumerate(self.edges):
                if edge["type"] not in (((rel.type,) if rel.type else ()) + rel.alternatives):
                    continue
                if any(_compare(edge["properties"].get(key), "=", value.value) is not True
                       for key, value in rel.properties):
                    continue
                endpoints = ((edge["source"], edge["target"]),)
                if pattern.direction == "in":
                    endpoints = ((edge["target"], edge["source"]),)
                elif pattern.direction == "undirected":
                    endpoints += ((edge["target"], edge["source"]),)
                for source, target in endpoints:
                    if (self._node_matches(pattern.left, source, env)
                            and self._node_matches(pattern.right, target, env)):
                        result = env | {pattern.left.variable: _Vertex(source),
                                        pattern.right.variable: _Vertex(target)}
                        if rel.variable is not None:
                            existing = env.get(rel.variable)
                            if rel.variable in env and existing != _Relation(edge_index):
                                continue
                            result[rel.variable] = _Relation(edge_index)
                        yield result
            return
        if lower is None or upper is None or pattern.direction != "undirected":
            raise PopulationError("Only the analyzed undirected bounded path can be matched")
        for start in self.nodes:
            if not self._node_matches(pattern.left, start, env):
                continue
            def visit(uid, vertices, used):
                hops = len(vertices) - 1
                if hops >= lower and self._node_matches(pattern.right, uid, env):
                    result = env | {pattern.left.variable: _Vertex(start), pattern.right.variable: _Vertex(uid)}
                    if pattern.path_variable:
                        result[pattern.path_variable] = _Path(vertices)
                    yield result
                if hops == upper:
                    return
                for i, edge in enumerate(self.edges):
                    if i in used or edge["type"] != rel.type:
                        continue
                    if any(_compare(edge["properties"].get(k), "=", v.value) is not True
                           for k, v in rel.properties):
                        continue
                    target = edge["target"] if edge["source"] == uid else edge["source"] if edge["target"] == uid else None
                    if target is None or rel.path_mode == "acyclic" and target in vertices:
                        continue
                    yield from visit(target, (*vertices, target), used | {i})
            yield from visit(start, (start,), frozenset())

    def _project(self, projections, env):
        names = projection_names(Query(None, None, projections))
        return {name: self.expression(item.expression if isinstance(item, Alias) else item, env)
                for name, item in zip(names, projections)}

    def run(self, query, outer=None):
        if type(query).__name__ == "ConditionalQuery":
            branch = query.then if self.expression(query.condition, dict(outer or {})) is True else query.otherwise
            return self.run(branch, outer)
        if isinstance(query, UnionAll):
            return [row for branch in query.branches for row in self.run(branch, outer)]
        if type(query).__name__ == "RowLimit":
            return self.run(query.body, outer)[:query.limit]
        rows = self.bindings(query, outer)
        result = [self._project(query.returns, row) for row in rows]
        if query.distinct:
            result = _distinct(result)
        if query.limit is not None:
            result = result[:query.limit]
        return result

    def bindings(self, query, outer=None):
        """Actual row environments after a Query's clause pipeline."""
        rows = [dict(outer or {})]
        for clause in _steps(query):
            if isinstance(clause, Match):
                matched = []
                for row in rows:
                    candidates = [result for result in self._match(clause.pattern, row)
                                  if clause.where is None or self.expression(clause.where, result) is True]
                    if clause.optional and not candidates:
                        names = ([clause.pattern.variable] if isinstance(clause.pattern, Node) else
                                 [clause.pattern.left.variable, clause.pattern.right.variable,
                                  clause.pattern.relationship.variable, clause.pattern.path_variable])
                        candidates = [row | {name: None for name in names if name and name not in row}]
                    matched.extend(candidates)
                rows = matched
            elif type(clause).__name__ == "With":
                rows = [self._project(clause.projections, row) for row in rows]
                if clause.distinct:
                    rows = _distinct(rows)
                if clause.where is not None:
                    rows = [row for row in rows if self.expression(clause.where, row) is True]
            else:
                raise PopulationError("Unsupported population clause")
        return rows


def synthesize_relational_state(source, *, alternatives=(), scale: int, data_seed: int,
                               demands=(), obligations=(), result_rows=None,
                               correspondence=None, min_result_rows=0):
    """Joint finite matching-atom solver; no test-pattern identifiers are read."""
    if any(isinstance(value, Edge) and value.relationship.max_hops is not None
           for tree in (source, *alternatives) for value in _walk(tree)):
        from path_population import synthesize
        return synthesize(source, alternatives=alternatives, scale=scale, data_seed=data_seed,
                          demands=demands, obligations=obligations, result_rows=result_rows,
                          correspondence=correspondence, min_result_rows=min_result_rows)
    from relational_population import synthesize
    return synthesize(source, alternatives=alternatives, scale=scale, data_seed=data_seed,
                      demands=demands, obligations=obligations, result_rows=result_rows,
                      correspondence=correspondence, min_result_rows=min_result_rows)


def _distinct(rows):
    unique, seen = [], set()
    for row in rows:
        key = _typed(row)
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique


def _typed(value):
    if isinstance(value, dict):
        return ("row", tuple(sorted((key, _typed(item)) for key, item in value.items())))
    if isinstance(value, (tuple, list)):
        return (type(value).__name__, tuple(_typed(item) for item in value))
    if isinstance(value, _Vertex):
        return ("node", value.uid)
    if isinstance(value, _Path):
        return ("path", value.vertices)
    if isinstance(value, _Relation):
        return ("relationship", value.index)
    return (type(value).__name__, value)


def _bag(rows):
    return Counter(_typed(row) for row in rows)


def evaluate_query(query: Query, spec: GraphSpec) -> list[dict[str, Any]]:
    """Concrete independent semantics for small generated states (not a DB oracle)."""
    if spec.counts()["nodes"] > 5000:
        raise PopulationError("Concrete interpretation is limited to 5000 nodes")
    return _Interpreter(spec).run(query)


def _scalar(value, assignments):
    """Evaluate node-local scalar predicates while searching property values."""
    if isinstance(value, Literal):
        return value.value
    if isinstance(value, Property):
        return assignments.get(value.key)
    if isinstance(value, Comparison):
        return _compare(_scalar(value.left, assignments), value.operator, _scalar(value.right, assignments))
    if isinstance(value, Negation):
        result = _scalar(value.expression, assignments)
        return None if result is None else not result
    if isinstance(value, Boolean):
        return _logical(value.operator, [_scalar(part, assignments) for part in value.terms])
    if isinstance(value, Case):
        return _scalar(value.then if _scalar(value.condition, assignments) is True else value.otherwise, assignments)
    if isinstance(value, Call) and value.name == "coalesce":
        return next((result for part in value.args if (result := _scalar(part, assignments)) is not None), None)
    raise PopulationError("ALL must contain supported node-local scalar expressions")


def _valuations(keys, fixed, domains=None):
    free = sorted(set(keys) - set(fixed))
    if len(free) > 8:
        raise PopulationError("Boolean property search is limited to eight free properties")
    for values in product(*[(domains or {}).get(key, (True, False)) for key in free]):
        yield fixed | dict(zip(free, values))


def _property_domains(values, variables=None):
    domains = {}
    for value in values:
        if not isinstance(value, Comparison):
            continue
        for prop, literal in ((value.left, value.right), (value.right, value.left)):
            if (isinstance(prop, Property) and (variables is None or prop.variable in variables)
                    and isinstance(literal, Literal) and type(literal.value) is int):
                domains.setdefault(prop.key, []).extend(candidate for candidate in
                                                       (literal.value, literal.value + 1, literal.value - 1)
                                                       if -(2**63) <= candidate < 2**63)
    return {key: tuple(dict.fromkeys(items)) for key, items in domains.items()}


def _count_bounds(located, analysis):
    """Follow lexical producers, so a shadowed alias cannot inherit a COUNT."""
    def node_id(address):
        return "root" + "".join(f"/{part}" for part in address)

    nodes = {node_id(address): (address, value) for address, value in located}
    uses = {use["node_id"]: use["symbol_id"] for use in analysis.uses}
    producers = {symbol["symbol_id"]: symbol.get("producer_node") for symbol in analysis.symbols}

    def consumes_count(address, value, seen=frozenset()):
        if isinstance(value, Count):
            return True
        if isinstance(value, Variable):
            symbol = uses.get(node_id(address))
            producer = producers.get(symbol)
            if symbol in seen or producer not in nodes:
                return False
            return consumes_count(*nodes[producer], seen | {symbol})
        if is_dataclass(value):
            return any(consumes_count((*address, *relative), child, seen)
                       for relative, child in _located(value) if relative and isinstance(child, Count | Variable))
        return False

    return [max(0, literal.value) for address, value in located if isinstance(value, Comparison)
            and consumes_count(address, value)
            for literal in (value.left, value.right)
            if isinstance(literal, Literal) and type(literal.value) is int]


def _merge_properties(nodes):
    result = {}
    for node in nodes:
        for key, literal in node.properties:
            if key in result and not _compare(result[key], "=", literal.value):
                raise PopulationError(f"Conflicting properties on the shared anchor: {key}")
            result[key] = literal.value
    return result


def _affine(base, step=1):
    return {"affine": {"base": base, "step": step}}


def _build_model(path, predicate, roots, root_values, true_values, false_values,
                 accepted, failed, valid_length, failure_length, seed, failure_depth=1,
                 sharing="shared"):
    root_labels = tuple(dict.fromkeys(label for node in roots for label in node.labels))
    target_labels = path.right.labels
    root_uid = root_values.get("uid", 1)
    if type(root_uid) is not int:
        raise PopulationError("Anchor uid must be an integer")
    next_uid = max(10, root_uid + 10) if root_uid < 2**63 - 10 else 10
    root_values = root_values | {"uid": root_uid}
    nodes = [{"count": 1, "labels": list(root_labels), "properties": root_values}]
    edges = []
    internal_label = "GspSynthInterior"
    while internal_label in set(root_labels) | set(target_labels):
        internal_label += "Fresh"

    def node(labels, values):
        nonlocal next_uid
        uid = values.get("uid", next_uid)
        if type(uid) is not int or any(g["properties"]["uid"] == uid for g in nodes):
            raise PopulationError("Node-map uid values conflict with fresh graph identities")
        next_uid = max(next_uid + 1, uid + 1)
        nodes.append({"count": 1, "labels": list(labels), "properties": values | {"uid": uid}})
        return uid

    def edge(source, target, source_label, target_label, count=1):
        edges.append({"count": count, "source": source, "target": target,
                      "source_label": source_label, "target_label": target_label,
                      "type": path.relationship.type,
                      "properties": {key: val.value for key, val in path.relationship.properties}})

    target_fixed = _merge_properties([path.right])
    for _ in range(accepted):
        previous, label = root_uid, root_labels[0]
        for depth in range(1, valid_length + 1):
            labels = target_labels if depth == valid_length else (internal_label,)
            values = true_values | (target_fixed if depth == valid_length else {})
            current = node(labels, values)
            edge(previous, current, label, labels[0])
            previous, label = current, labels[0]

    if failed:
        # A chosen prefix is constant-size. Each remaining suffix is a fresh
        # chain. Tree/TRAIL semantics prevents both bypasses and recombination.
        previous, previous_label = root_uid, root_labels[0]
        shared_depth = failure_depth if sharing == "shared" else 0
        for depth in range(1, shared_depth + 1):
            failure = node((internal_label,), false_values if depth == failure_depth else true_values)
            edge(previous, failure, previous_label, internal_label)
            previous, previous_label = failure, internal_label
        base = max(next_uid + 1000, random.Random(seed).randrange(10000, 1000000))
        for depth in range(shared_depth + 1, failure_length + 1):
            labels = target_labels if depth == failure_length else (internal_label,)
            values = (false_values if depth == failure_depth else true_values) | (target_fixed if depth == failure_length else {})
            if "uid" in values:
                raise PopulationError("A replicated endpoint cannot have a fixed uid")
            uid = _affine(base + (depth - shared_depth - 1) * failed)
            nodes.append({"count": failed, "labels": list(labels), "properties": values | {"uid": uid}})
            edge(previous, uid, previous_label, labels[0], failed)
            previous, previous_label = uid, labels[0]
    indexes = []
    access_keys = tuple(dict.fromkeys(key for root in roots for key, _ in root.properties))
    if access_keys:
        indexes.append({"name": "gsp_cost_lookup", "label": root_labels[0], "property": access_keys[0]})
    return GraphSpec("AST", max(1, failed), seed, include_reference=False, version=2,
                     node_groups=tuple(nodes), edge_groups=tuple(edges), index_requirements=tuple(indexes))


def synthesize_state(query: Query, *, scale: int, data_seed: int, result_rows: int = 1,
                     prefix_budget: int = 1, max_models: int = 50_000,
                     expected_values: dict[str, Any] | None = None) -> PopulationResult:
    """Solve supported single-anchor component demands from the actual query.

    The numeric domain is derived from comparison literals; candidates are
    tested with the independent full-query interpreter, so COUNT thresholds,
    boolean contexts and WITH aliases constrain the *same* path cardinality.
    A successful model is lifted by a protected local-filter or false-prefix
    construction certificate. Optional output values are explicit test intent.
    """
    if type(scale) is not int or scale < 1 or type(data_seed) is not int or data_seed < 0:
        raise PopulationError("scale must be positive and data_seed nonnegative")
    if type(result_rows) is not int or result_rows < 0 or result_rows > 64:
        raise PopulationError("Requested result bag size must lie between zero and 64")
    if type(prefix_budget) is not int or prefix_budget < 0 or prefix_budget > 8:
        raise PopulationError("The early-failure prefix budget must lie in 0..8")
    if type(max_models) is not int or max_models < 1:
        raise PopulationError("The finite model budget must be positive")
    if not isinstance(query, Query):
        raise PopulationError("Automatic population requires an ordinary Query, not UNION or conditional query statements")
    if expected_values is not None:
        if not isinstance(expected_values, dict) or any(type(value) not in (type(None), bool, int)
                                                        for value in expected_values.values()):
            raise PopulationError("Output value requirements must map column names to boolean/integer/null scalars")
        if not set(expected_values).issubset(projection_names(query)):
            raise PopulationError("An output value requirement names an unknown projection")
    try:
        validate_query(query)
    except ValueError as error:
        raise PopulationError(str(error)) from error
    located = list(_located(query))
    values = [value for _, value in located]
    paths = [(address, value) for address, value in located
             if isinstance(value, Edge) and value.relationship.max_hops is not None]
    if not paths:
        return _synthesize_fixed_components(query, located, scale=scale, data_seed=data_seed,
                                            result_rows=result_rows, max_models=max_models,
                                            expected_values=expected_values)
    # Occurrences are lexical sites, not structurally equal values: a second
    # identical-looking COUNT could observe the newly added rejected paths.
    if len(paths) != 1:
        raise PopulationError("Automatic population requires exactly one lexical bounded-path occurrence")
    path_address, path = paths[0]
    if (path.direction != "undirected" or path.relationship.path_mode not in ("trail", "acyclic")
            or not path.relationship.type or path.relationship.alternatives
            or not path.path_variable or not path.right.labels
            or not 1 <= path.relationship.min_hops <= path.relationship.max_hops <= 8
            or path.relationship.max_hops < 2):
        raise PopulationError("A labeled, typed TRAIL/ACYCLIC path with bounds inside 1..8 and room for a failure suffix is required")
    if any(isinstance(value, Edge) and value is not path for value in values):
        raise PopulationError("Additional relationship components require joint synthesis and are unsupported")
    all_values = []
    for value in values:
        if isinstance(value, All) and value not in all_values:
            all_values.append(value)
    matching = [value for value in all_values if isinstance(value.items, Call)
                and value.items.name == "nodes" and value.items.args == (Variable(path.path_variable),)]
    if len(matching) != 1 or len(all_values) != 1:
        raise PopulationError("One node-local ALL over the selected path is required")
    universal = matching[0]
    guard_sites = []
    for value in values:
        if isinstance(value, Query):
            guard_sites.extend(clause.where for clause in _steps(value)
                               if isinstance(clause, Match) and clause.pattern is path)
        elif isinstance(value, (Count, Exists)) and getattr(value, "body", None) is None and value.pattern is path:
            guard_sites.append(value.where)
    def guards_failure(expression):
        if expression is universal:
            return True
        return (isinstance(expression, Boolean) and expression.operator == "AND"
                and any(guards_failure(term) for term in expression.terms))
    if len(guard_sites) != 1 or not guards_failure(guard_sites[0]):
        raise PopulationError("The unique path site must be filtered by ALL as a positive mandatory conjunct")
    # The IR checks node locality, while this search checks actual truth values.
    roots = [value for address, value in located
             if isinstance(value, Node) and address != (*path_address, "right")]
    root_labels = {label for root in roots for label in root.labels}
    if not root_labels or root_labels.intersection(path.right.labels):
        raise PopulationError("Anchor and endpoint labels must be nonempty and disjoint for the tree proof")
    # Every other graph read must remain confined to the fixed anchor. An
    # unlabelled node scan could observe the growing family at a high threshold
    # even when several small probes happened to agree.
    for address, value in located:
        if (isinstance(value, Node) and address not in ((*path_address, "left"), (*path_address, "right"))
                and not value.labels):
            raise PopulationError("Unlabelled node scans outside the protected path are unsupported")
    analysis = analyze_dependencies(query)
    sites = [site for site in find_p3_sites(query) if site.address == path_address]
    if len(sites) != 1:
        raise PopulationError("The path's lexical binding could not be resolved")
    left_symbol = sites[0].endpoint_symbols["left"]
    left_definition = next(symbol["definition"] for symbol in analysis.symbols
                           if symbol["symbol_id"] == left_symbol)
    fixed_definitions = {"root" + "".join(f"/{part}" for part in (*address, "variable"))
                         for address, value in located
                         if isinstance(value, Node) and value.labels
                         and address not in ((*path_address, "left"), (*path_address, "right"))}
    if not path.left.labels and left_definition not in fixed_definitions:
        raise PopulationError("A bare path start must import the fixed anchor binding, not introduce an unbound start")
    root_fixed = _merge_properties(roots)
    end_fixed = _merge_properties([path.right])
    root_names = {node.variable for node in roots}
    if path.right.variable in root_names:
        raise PopulationError("A bound endpoint requires a different state-construction proof")
    local_keys = {value.key for value in _walk(universal.predicate) if isinstance(value, Property)}
    root_keys = local_keys | {value.key for value in values if isinstance(value, Property) and value.variable in root_names}
    if any(isinstance(value, Literal) and type(value.value) not in (type(None), bool, int) for value in values):
        raise PopulationError("Automatic population currently supports boolean/integer/null literals only")
    # Large IDs in node maps are not count thresholds.
    comparison_bounds = _count_bounds(located, analysis)
    maximum = min(64, max([result_rows, 1, *comparison_bounds]) + 1)
    local_domains = _property_domains(list(_walk(universal.predicate)), {universal.variable})
    root_domains = dict(local_domains)
    for key, domain in _property_domains(values, root_names).items():
        root_domains[key] = tuple(dict.fromkeys((*domain, *root_domains.get(key, ()))))
    root_domains.setdefault("uid", (1,))
    true_assignments = [props for props in _valuations(local_keys, {}, local_domains) if _scalar(universal.predicate, props) is True]
    false_assignments = [props for props in _valuations(local_keys, {}, local_domains) if _scalar(universal.predicate, props) is False]
    if not true_assignments or not false_assignments:
        raise PopulationError("The ALL predicate needs both a true witness and an explicit false valuation")
    structural_candidates = []
    rejected_structures = []
    for length in range(path.relationship.max_hops, max(2, path.relationship.min_hops) - 1, -1):
        for depth in range(length):
            for sharing in ("shared", "independent"):
                # The demanded number of inspected failing prefixes stays
                # bounded as scale grows. Independent prefixes violate it.
                work = depth if sharing == "shared" else depth * scale
                if work > prefix_budget:
                    rejected_structures.append({"length": length, "failure_depth": depth,
                                                "sharing": sharing, "reason": "prefix inspection budget exceeded"})
                else:
                    structural_candidates.append((length, depth, sharing))
    attempted_models = 0
    for root_props in _valuations(root_keys, root_fixed, root_domains):
        root_truth = _scalar(universal.predicate, root_props)
        for good in true_assignments:
            end_props = good | end_fixed
            if _scalar(universal.predicate, end_props) is not True:
                continue
            for bad in false_assignments:
                for accepted in range(maximum + 1):
                    if accepted and root_truth is not True:
                        continue
                    for valid_length in range(max(1, path.relationship.min_hops), path.relationship.max_hops + 1):
                        for failure_length, failure_depth, sharing in structural_candidates:
                            if (failure_depth == 0) != (root_truth is False):
                                continue
                            attempted_models += 1
                            if attempted_models > max_models:
                                raise PopulationError("Finite model search budget exhausted; satisfiability is unknown")
                            try:
                                small = _build_model(path, universal.predicate, roots, root_props, good, bad,
                                                     accepted, 1, valid_length, failure_length, data_seed, failure_depth, sharing)
                                interpreter = _Interpreter(small)
                                observed = interpreter.run(query)
                                if not _satisfies_output(observed, result_rows, expected_values) or any(isinstance(item, (_Vertex, _Path))
                                                                     for row in observed for item in row.values()):
                                    continue
                                # Check the entire bag, not just the chosen subquery count,
                                # on independent expansions before applying the tree proof.
                                for probe in (0, 2, 3):
                                    trial = _build_model(path, universal.predicate, roots, root_props, good, bad,
                                                         accepted, probe, valid_length, failure_length, data_seed, failure_depth, sharing)
                                    if _bag(evaluate_query(query, trial)) != _bag(observed):
                                        raise PopulationError("Failed candidates changed an enclosing query result")
                                spec = _build_model(path, universal.predicate, roots, root_props, good, bad,
                                                    accepted, scale, valid_length, failure_length, data_seed, failure_depth, sharing)
                            except (TypeError, KeyError, IndexError) as error:
                                raise PopulationError(f"Unsupported correlated population context: {error}") from error
                            constraints = {"path_variable": path.path_variable, "valid_paths": accepted,
                                           "failed_paths": scale, "path_bounds": [path.relationship.min_hops, path.relationship.max_hops],
                                           "valid_length": valid_length, "failure_length": failure_length,
                                           "failure_depth": failure_depth, "root_properties": root_props,
                                           "true_properties": good, "false_properties": bad,
                                           "requested_result_rows": result_rows,
                                           "count_search_domain": [0, maximum]}
                            evidence = {"method": "whole_query_cardinality_and_truth_search",
                                        "proof": "fresh tree branches share only a node-local false prefix; every added endpoint path fails ALL",
                                        "checked_failure_counts": [0, 1, 2, 3],
                                        "normal_form": "single-anchor fresh tree; branch sharing only on the failing prefix",
                                        "selected_sharing": sharing,
                                        "prefix_budget": prefix_budget, "models_tried": attempted_models,
                                        "structural_choices": len(structural_candidates),
                                        "rejected_structures": rejected_structures,
                                        "subquery_outcomes": interpreter.subquery_outcomes,
                                        "validation_graph_spec": small.to_dict(),
                                        "anchor_symbol": left_symbol,
                                        "anchor_definition": left_definition,
                                        "scope": "single shared anchor; finite trail/acyclic paths; no OPTIONAL/LIMIT/other edges",
                                        "physical_cost_guaranteed": False}
                            return PopulationResult(spec, observed, render_state_checks(spec.state_checks()), constraints, evidence)
    raise PopulationError("No compatible model found in the finite property/cardinality domain and tree construction budget")


def _satisfies_output(rows, result_rows, expected_values):
    if len(rows) != result_rows:
        return False
    if not expected_values:
        return True
    if not rows:
        return False
    return all(all(key in row and _typed(row[key]) == _typed(value)
                   for key, value in expected_values.items()) for row in rows)


def _symbol_lookup(analysis):
    result = {use["node_id"]: use["symbol_id"] for use in analysis.uses}
    result.update({symbol["definition"]: symbol["symbol_id"] for symbol in analysis.symbols})
    return result


def _address_id(address):
    return "root" + "".join(f"/{part}" for part in address)


def _component_domains(located, symbols, symbol):
    """Property literals belong to lexical bindings, not just printed names."""
    result = {}
    for address, value in located:
        if not isinstance(value, Comparison):
            continue
        for field, expression, literal in (("left", value.left, value.right), ("right", value.right, value.left)):
            if not isinstance(literal, Literal) or type(literal.value) is not int:
                continue
            if any(isinstance(part, (Count, Exists, All)) for part in _walk(expression)):
                continue
            for relative, prop in _located(expression):
                if isinstance(prop, Property) and symbols.get(_address_id((*address, field, *relative))) == symbol:
                    result.setdefault(prop.key, []).extend(candidate for candidate in
                                                           (literal.value, literal.value + 1, literal.value - 1)
                                                           if -(2**63) <= candidate < 2**63)
    return {key: tuple(dict.fromkeys(values)) for key, values in result.items()}


def _fixed_model(components, root_nodes, root_values, good_values, bad_value,
                 quantities, expanded, failed, data_seed):
    """Compatibility adapter for the local-filter amplification objective."""
    extras = tuple(failed if i == expanded else 0 for i in range(len(components)))
    bad_values = tuple(bad_value if i == expanded else {} for i in range(len(components)))
    return _realize_star(components, root_nodes, root_values, good_values, bad_values,
                         quantities, extras, max(1, failed), data_seed)


def _realize_star(components, root_nodes, root_values, good_values, bad_values,
                  quantities, extra_candidates, scale, data_seed):
    """Realize solved endpoint quantities; valid and rejected groups both stream.

    Neither role names nor wrapper combinations participate in realization.
    A large valid workload is represented by one affine group, just like a
    large rejected workload. All graph reads must be proved closed beforehand.
    """
    labels = tuple(dict.fromkeys(label for node in root_nodes for label in node.labels))
    root_uid = root_values.get("uid", 1)
    if type(root_uid) is not int:
        raise PopulationError("The shared anchor requires an integer identity")
    nodes = [{"count": 1, "labels": list(labels), "properties": root_values | {"uid": root_uid}}]
    edges = []
    occupied = [(root_uid, root_uid)]
    next_uid = max(10, root_uid + 10) if root_uid < 2**63 - 1000 else 10
    for component, properties, quantity in zip(components, good_values, quantities):
        pattern = component["pattern"]
        if not quantity:
            continue
        base = properties.get("uid", next_uid)
        if (type(base) is not int or ("uid" in properties and quantity != 1)
                or any(not (base + quantity - 1 < low or base > high) for low, high in occupied)
                or base + quantity - 1 >= 2**63):
            raise PopulationError("Fixed component identities conflict")
        occupied.append((base, base + quantity - 1))
        uid = base if quantity == 1 else _affine(base)
        next_uid = max(next_uid, base + quantity)
        nodes.append({"count": quantity, "labels": list(pattern.right.labels),
                      "properties": properties | {"uid": uid}})
        edges.append({"count": quantity, "source": root_uid, "target": uid,
                      "source_label": labels[0], "target_label": pattern.right.labels[0],
                      "type": pattern.relationship.type,
                      "properties": {key: literal.value for key, literal in pattern.relationship.properties}})
    for component, bad_value, failed in zip(components, bad_values, extra_candidates):
        if not failed:
            continue
        pattern = component["pattern"]
        if "uid" in bad_value:
            raise PopulationError("An amplified endpoint cannot have a fixed identity")
        base = max(next_uid + 1000, random.Random(data_seed).randrange(10000, 1000000))
        if base + failed - 1 >= 2**63:
            raise PopulationError("The candidate family exceeds the signed integer identity range")
        uid = _affine(base)
        next_uid = base + failed
        nodes.append({"count": failed, "labels": list(pattern.right.labels),
                      "properties": bad_value | {"uid": uid}})
        edges.append({"count": failed, "source": root_uid, "target": uid,
                      "source_label": labels[0], "target_label": pattern.right.labels[0],
                      "type": pattern.relationship.type,
                      "properties": {key: literal.value for key, literal in pattern.relationship.properties}})
    keys = tuple(dict.fromkeys(key for node in root_nodes for key, _ in node.properties))
    indexes = ({"name": "gsp_cost_lookup", "label": labels[0], "property": keys[0]},) if keys else ()
    return GraphSpec("AST", max(1, scale), data_seed, version=2, include_reference=False,
                     node_groups=tuple(nodes), edge_groups=tuple(edges), index_requirements=indexes)


def _local_key(value):
    """Normalize local names while retaining property names and scalar types."""
    if isinstance(value, Property):
        return ("property", value.key)
    if isinstance(value, Literal):
        return ("literal", _typed(value.value))
    if is_dataclass(value):
        return (type(value).__name__, tuple((field.name, _local_key(getattr(value, field.name)))
                                           for field in fields(value)))
    if isinstance(value, tuple):
        return tuple(_local_key(part) for part in value)
    return _typed(value)


def _constant_rows(flow):
    """A limit can select arbitrary rows only when their projected bags agree."""
    if isinstance(flow, UnionAll):
        values = [_constant_rows(branch) for branch in flow.branches]
        if not values or any(_typed(value) != _typed(values[0]) for value in values[1:]):
            raise PopulationError("A saturated union requires identical typed constant projections")
        return values[0]
    if type(flow).__name__ == "RowLimit":
        return _constant_rows(flow.body)
    if not isinstance(flow, Query):
        raise PopulationError("Unsupported constant row-flow producer")
    names = projection_names(flow)
    row = {}
    for name, item in zip(names, flow.returns):
        value = item.expression if isinstance(item, Alias) else item
        if not isinstance(value, Literal):
            raise PopulationError("A saturated union requires constant projections in every branch")
        row[name] = value.value
    return row


def _flow_clauses(flow, address):
    if flow.pipeline:
        return [(clause, (*address, "pipeline", i)) for i, clause in enumerate(flow.pipeline)]
    result = [(Match(flow.pattern, flow.where), address)] if flow.pattern is not None else []
    result.extend((clause, (*address, "matches", i)) for i, clause in enumerate(flow.matches))
    return result


def synthesize_symbolic_state(query, *, demands: tuple[MatchDemand, ...], alternatives=(),
                              scale: int, data_seed: int, result_rows: int | None = None,
                              expected_values: dict[str, Any] | None = None,
                              max_models: int = 50_000) -> PopulationResult:
    """Jointly populate an AST forest with explicit, address-based workloads.

    Match quantities are input obligations. In contrast, row-limit and EXISTS
    witnesses are derived from actual flow operators. Repeated occurrences of
    the same relation domain share one population, including across alternative
    queries. Active growth needs an observation-level saturation certificate;
    it is never replaced with a family of rejected endpoint candidates.
    """
    if type(scale) is not int or scale < 1 or type(data_seed) is not int or data_seed < 0:
        raise PopulationError("scale must be positive and data_seed nonnegative")
    if result_rows is not None and (type(result_rows) is not int or not 0 <= result_rows <= 64):
        raise PopulationError("A requested result size must be a nonnegative integer within 64")
    if type(max_models) is not int or max_models < 1:
        raise PopulationError("The finite model budget must be positive")
    trees = [(('query',), query), *((('alternatives', i), tree) for i, tree in enumerate(alternatives))]
    if expected_values is not None:
        if (not isinstance(expected_values, dict)
                or any(type(value) not in (type(None), bool, int, float, str) for value in expected_values.values())
                or not set(expected_values).issubset(projection_names(query))):
            raise PopulationError("Output demands must map existing projection names to scalar values")
    all_located, tree_info = [], []
    for prefix, tree in trees:
        try:
            validate_query(tree)
            analysis = analyze_dependencies(tree)
        except ValueError as error:
            raise PopulationError(str(error)) from error
        local = list(_located(tree))
        all_located.extend(((*prefix, *address), value) for address, value in local)
        tree_info.append((prefix, tree, local, analysis, _symbol_lookup(analysis)))
    demand_map = {}
    for demand in demands:
        if not isinstance(demand, MatchDemand) or not isinstance(demand.role_atom, str) or not demand.role_atom:
            raise PopulationError("Workloads must be address-based MatchDemand values")
        for value in (demand.min_matches, demand.exact_matches, demand.candidate_matches):
            if value is not None and (type(value) is not int or value < 0):
                raise PopulationError("Match quantities must be nonnegative integers")
        if type(demand.scale_matches) is not bool:
            raise PopulationError("scale_matches must be boolean")
        demand_map.setdefault(tuple(demand.address), []).append(demand)
    occurrences, root_nodes, root_candidates, all_symbols = [], [], [], []
    for prefix, tree, local, analysis, symbols in tree_info:
        lookup = dict(local)
        local_edges = [(address, edge) for address, edge in local if isinstance(edge, Edge)]
        endpoints = {(*address, "right") for address, _ in local_edges}
        root_symbols = set()
        for address, edge in local_edges:
            if (edge.direction != "out" or edge.relationship.min_hops is not None
                    or edge.relationship.max_hops is not None or not edge.relationship.type
                    or edge.relationship.alternatives or edge.relationship.variable is not None
                    or edge.path_variable is not None or not edge.right.labels):
                raise PopulationError("Symbolic workloads require typed outgoing one-hop components")
            left = symbols.get(_address_id((*address, "left", "variable")))
            right = symbols.get(_address_id((*address, "right", "variable")))
            if left is None or right is None or left == right:
                raise PopulationError("A workload edge requires distinct resolved endpoint bindings")
            right_def = next(record["definition"] for record in analysis.symbols if record["symbol_id"] == right)
            if right_def != _address_id((*address, "right", "variable")):
                raise PopulationError("A workload endpoint must be fresh at its lexical matching site")
            parent = lookup.get(address[:-1])
            if isinstance(parent, Match) and parent.optional:
                raise PopulationError("Optional workload components require a separate null-row proof")
            predicate = getattr(parent, "where", None)
            if predicate is not None:
                for relative, part in _located(predicate):
                    if isinstance(part, Property):
                        reference = symbols.get(_address_id((*address[:-1], "where", *relative)))
                        if reference != right or part.key == "uid":
                            raise PopulationError("Workload filters must be endpoint-local and independent of identities")
                    elif isinstance(part, (Count, Exists, All, Variable)):
                        raise PopulationError("Nested or correlated endpoint filters need a richer joint solver")
            fixed = _merge_properties([edge.right])
            signature = (edge.relationship.type, tuple(sorted(edge.right.labels)),
                         _typed(fixed), tuple((key, _typed(val.value)) for key, val in edge.relationship.properties),
                         _local_key(predicate))
            occurrences.append({"address": (*prefix, *address), "pattern": edge, "predicate": predicate,
                                "anchor_symbol": (prefix, left), "endpoint_symbol": (prefix, right),
                                "signature": signature, "fixed": fixed,
                                "domains": _component_domains(local, symbols, right)})
            root_symbols.add(left)
        for address, node in local:
            if not isinstance(node, Node) or address in endpoints:
                continue
            symbol = symbols.get(_address_id((*address, "variable")))
            if symbol not in root_symbols:
                raise PopulationError("An additional graph read is outside the common-anchor workload closure")
            root_nodes.append(node)
        definitions = {record["symbol_id"]: record["definition"] for record in analysis.symbols}
        labeled_definitions = {_address_id((*address, "variable")) for address, value in local
                               if isinstance(value, Node) and value.labels and address not in endpoints}
        for address, node in local:
            if isinstance(node, Node) and address not in endpoints and not node.labels:
                symbol = symbols.get(_address_id((*address, "variable")))
                if definitions[symbol] not in labeled_definitions:
                    raise PopulationError("An unbound anchor node scan has no fixed labeled domain")
        for address, edge in local_edges:
            symbol = symbols.get(_address_id((*address, "left", "variable")))
            if not edge.left.labels and definitions[symbol] not in labeled_definitions:
                raise PopulationError("A bare workload start must import a labeled fixed anchor")
        keys = {part.key for address, part in local if isinstance(part, Property)
                and symbols.get(_address_id(address)) in root_symbols}
        domains = {}
        for symbol in root_symbols:
            for key, values in _component_domains(local, symbols, symbol).items():
                domains.setdefault(key, []).extend(values)
        root_candidates.append((keys, domains))
        all_symbols.extend({"tree": list(prefix), "symbol": symbol, "uid": 1} for symbol in sorted(root_symbols))
    if not occurrences or len(occurrences) > 32:
        raise PopulationError("The symbolic forest needs one to 32 component occurrences")
    if set(demand_map) - {occurrence["address"] for occurrence in occurrences}:
        raise PopulationError("A workload address does not identify an Edge occurrence in the AST forest")
    labels = {label for node in root_nodes for label in node.labels}
    if not labels:
        raise PopulationError("The shared physical anchor requires a nonempty fixed label domain")
    components, occurrence_index, role_signatures = [], {}, {}
    for occurrence in occurrences:
        if labels.intersection(occurrence["pattern"].right.labels):
            raise PopulationError("Workload endpoint and anchor label domains overlap")
        match = next((i for i, component in enumerate(components)
                      if component["signature"] == occurrence["signature"]), None)
        if match is None:
            match = len(components)
            components.append({**occurrence, "occurrences": [], "demands": [], "role_atoms": [], "minimum": 0})
        component = components[match]
        component["occurrences"].append(occurrence["address"])
        for demand in demand_map.get(occurrence["address"], ()):
            if demand.role_atom in role_signatures and role_signatures[demand.role_atom] != occurrence["signature"]:
                raise PopulationError("One role atom refers to incompatible relation domains")
            role_signatures[demand.role_atom] = occurrence["signature"]
            component["demands"].append(demand)
            if demand.role_atom not in component["role_atoms"]:
                component["role_atoms"].append(demand.role_atom)
            component["minimum"] = max(component["minimum"], demand.min_matches)
        occurrence_index[occurrence["address"]] = match
    if len(components) > 8:
        raise PopulationError("The common-anchor workload solver supports at most eight relation domains")
    for i, first in enumerate(components):
        for second in components[i + 1:]:
            left, right = first["pattern"], second["pattern"]
            if (left.relationship.type == right.relationship.type
                    and (set(left.right.labels).issubset(right.right.labels)
                         or set(right.right.labels).issubset(left.right.labels))):
                raise PopulationError("Nonidentical workload component read domains overlap")
    certificates, guard_obligations = [], []

    def transfer(flow, address, required, reason):
        if type(flow).__name__ == "RowLimit":
            if required > flow.limit:
                raise PopulationError("A row limit cannot provide the required witness cardinality")
            return transfer(flow.body, (*address, "body"), required, reason)
        if isinstance(flow, UnionAll):
            if not flow.branches:
                raise PopulationError("An empty union cannot provide a row witness")
            return transfer(flow.branches[0], (*address, "branches", 0), required, reason)
        if not isinstance(flow, Query):
            raise PopulationError("Unsupported witness-producing row flow")
        if flow.limit is not None:
            raise PopulationError("Witness limits must be explicit RowLimit operators")
        if flow.distinct and required > 1:
            raise PopulationError("DISTINCT prevents a constant branch from guaranteeing multiple rows")
        branch_components = []
        for clause, clause_address in _flow_clauses(flow, address):
            if not isinstance(clause, Match):
                raise PopulationError("A witness query with WITH requires a separate row-count transfer")
            if clause.optional:
                raise PopulationError("Optional witnesses require a separate row-count transfer")
            if isinstance(clause.pattern, Edge):
                index = occurrence_index[(*clause_address, "pattern")]
                branch_components.append(index)
        if len(branch_components) > 1:
            raise PopulationError("A witness MATCH product needs a joint minimum-product demand")
        if branch_components:
            index = branch_components[0]
            components[index]["minimum"] = max(components[index]["minimum"], required)
        elif required > 1:
            raise PopulationError("A fixed anchor or RETURN-only branch provides at most one witness row")
        guard_obligations.append((flow, address, required, reason))
        return branch_components

    for address, value in all_located:
        if type(value).__name__ == "RowLimit" and isinstance(value.body, UnionAll):
            if value.limit == 0:
                certificates.append({"operator": "RowLimit", "address": list(address),
                                     "covered_prefix": [*address, "body"], "required_rows": 0,
                                     "witness_components": [], "constant_projection": None})
                continue
            projection = _constant_rows(value.body)
            indices = transfer(value.body.branches[0], (*address, "body", "branches", 0), value.limit,
                               "row_limit_saturation")
            certificates.append({"operator": "RowLimit", "address": list(address),
                                 "covered_prefix": [*address, "body"], "required_rows": value.limit,
                                 "witness_components": indices, "constant_projection": projection})
        if isinstance(value, Exists) and getattr(value, "body", None) is not None:
            body = value.body
            producer = body
            while type(producer).__name__ == "RowLimit":
                producer = producer.body
            # An enclosing Query/WITH may observe an already saturated child
            # through arbitrary scalar aliases. It does not need another
            # closed-form row-count transfer: the child certificate plus full
            # query interpretation preserves those values. Only a UNION that
            # is directly the existential row producer needs this witness.
            if not isinstance(producer, UnionAll):
                continue
            indices = transfer(body, (*address, "body"), 1, "existence_witness")
            certificates.append({"operator": "Exists", "address": list(address),
                                 "covered_prefix": [*address, "body"], "required_rows": 1,
                                 "witness_components": indices})
    dynamic = {i for i, component in enumerate(components)
               if any(demand.scale_matches for demand in component["demands"])}
    if not dynamic:
        raise PopulationError("A symbolic workload needs an explicit scale-dependent match demand")
    if any(index in dynamic for certificate in certificates for index in certificate["witness_components"]):
        raise PopulationError("Saturation witnesses must use an independent fixed component, not the growing workload")
    for index in dynamic:
        for address in components[index]["occurrences"]:
            if not any(address[:len(certificate["covered_prefix"])] == tuple(certificate["covered_prefix"])
                       for certificate in certificates):
                raise PopulationError("A growing valid workload has a read outside a saturation or existence certificate")
    root_fixed = _merge_properties(root_nodes)
    root_keys, root_domains = set(), {}
    for keys, domains in root_candidates:
        root_keys.update(keys)
        for key, values in domains.items():
            root_domains.setdefault(key, []).extend(values)
    root_domains = {key: tuple(dict.fromkeys(values)) for key, values in root_domains.items()}
    root_domains.setdefault("uid", (1,))
    good_choices, bad_choices = [], []
    for component in components:
        predicate = component["predicate"]
        keys = {value.key for value in _walk(predicate) if isinstance(value, Property)} if predicate is not None else set()
        assignments = list(_valuations(keys, component["fixed"], component["domains"]))
        good = [props for props in assignments if predicate is None or _scalar(predicate, props) is True]
        bad = [props for props in assignments if predicate is not None and _scalar(predicate, props) is False]
        if not good:
            raise PopulationError("A valid workload demand has no true endpoint valuation")
        good_choices.append(good)
        bad_choices.append(bad or [{}])

    def quantities(at_scale, *, probe=False):
        matches, extras = [], []
        for i, component in enumerate(components):
            exact = {demand.exact_matches for demand in component["demands"] if demand.exact_matches is not None}
            if any(demand.scale_matches for demand in component["demands"]):
                exact.add(at_scale)
            if len(exact) > 1:
                raise PopulationError("Shared workload domains have conflicting exact match demands")
            count = next(iter(exact)) if exact else component["minimum"]
            if count < component["minimum"]:
                raise PopulationError("An exact match demand is below the AST-derived witness minimum")
            candidates = {demand.candidate_matches for demand in component["demands"] if demand.candidate_matches is not None}
            if len(candidates) > 1 or candidates and next(iter(candidates)) < count:
                raise PopulationError("Candidate and effective-match demands conflict")
            extra = next(iter(candidates)) - count if candidates else 0
            if extra and (component["predicate"] is None or bad_choices[i] == [{}]):
                raise PopulationError("Rejected candidates require a local filter with an explicit false valuation")
            matches.append(count)
            extras.append(min(extra, 3) if probe else extra)
        return tuple(matches), tuple(extras)

    full_counts, full_extras = quantities(scale)
    witness_bound = max([1, *(component["minimum"] for component in components),
                         *(count for i, count in enumerate(full_counts) if i not in dynamic)])
    if witness_bound > 64:
        raise PopulationError("The fixed witness exceeds the 64-row finite proof budget")
    small_scale = min(scale, max(3, witness_bound))
    small_counts, small_extras = quantities(small_scale, probe=True)
    attempts = 0
    for root_values in _valuations(root_keys, root_fixed, root_domains):
        for good_values in product(*good_choices):
            for bad_values in product(*bad_choices):
                attempts += 1
                if attempts > max_models:
                    raise PopulationError("Finite model search budget exhausted; satisfiability is unknown")
                small = _realize_star(components, root_nodes, root_values, good_values, bad_values,
                                      small_counts, small_extras, small_scale, data_seed)
                interpreter = _Interpreter(small)
                observed = interpreter.run(query)
                if result_rows is not None and len(observed) != result_rows:
                    continue
                if expected_values is not None and not _satisfies_output(observed, len(observed), expected_values):
                    continue
                if any(isinstance(value, (_Vertex, _Path)) for row in observed for value in row.values()):
                    raise PopulationError("Symbolic row-flow outputs must be invariant scalars")
                if any(_bag(interpreter.run(tree)) != _bag(observed) for tree in alternatives):
                    continue
                root_uid = root_values.get("uid", 1)
                guards_valid = True
                for flow, address, required, _ in guard_obligations:
                    prefix = next(prefix for prefix, _ in trees if address[:len(prefix)] == prefix)
                    info = next(info for info in tree_info if info[0] == prefix)
                    env = {record["name"]: _Vertex(root_uid) for record in info[3].symbols
                           if record["kind"] == "node" and any(item["tree"] == list(prefix)
                                                               and item["symbol"] == record["symbol_id"]
                                                               for item in all_symbols)}
                    if len(interpreter.run(flow, env)) < required:
                        guards_valid = False
                        break
                if not guards_valid:
                    continue
                for probe_scale in sorted({1, small_scale, max(4, witness_bound + 1)}):
                    probe_counts, probe_extras = quantities(probe_scale, probe=True)
                    trial = _realize_star(components, root_nodes, root_values, good_values, bad_values,
                                          probe_counts, probe_extras, probe_scale, data_seed)
                    if any(_bag(evaluate_query(tree, trial)) != _bag(observed) for _, tree in trees):
                        raise PopulationError("A workload changed an observable bag beyond the certified prefix")
                spec = _realize_star(components, root_nodes, root_values, good_values, bad_values,
                                     full_counts, full_extras, scale, data_seed)
                facts = [{"role_atoms": list(component["role_atoms"]),
                          "occurrences": [list(address) for address in component["occurrences"]],
                          "relationship_type": component["pattern"].relationship.type,
                          "endpoint_labels": list(component["pattern"].right.labels),
                          "derived_minimum": component["minimum"], "valid_matches": count,
                          "failed_candidates": extra, "candidate_matches": count + extra,
                          "true_properties": props}
                         for component, count, extra, props in zip(components, full_counts, full_extras, good_values)]
                assignment = {atom: {"relationship_type": fact["relationship_type"],
                                      "endpoint_labels": fact["endpoint_labels"],
                                      "valid_matches": fact["valid_matches"],
                                      "true_properties": fact["true_properties"]}
                              for fact in facts for atom in fact["role_atoms"]}
                return PopulationResult(spec, observed, render_state_checks(spec.state_checks()),
                                        {"components": facts, "root_properties": root_values,
                                         "assignment": assignment, "scale": scale},
                                        {"method": "joint_AST_workload_and_row_flow_witness_construction",
                                         "stage": "graph_construction_on_safe_symbolic_probe",
                                         "saturation_certificates": certificates,
                                         "validation_graph_spec": small.to_dict(),
                                         "subquery_outcomes": interpreter.subquery_outcomes,
                                         "anchor_unification": [item | {"uid": root_uid} for item in all_symbols],
                                         "checked_workload_scales": sorted({1, small_scale, max(4, witness_bound + 1)}),
                                         "proof": "exclusive relation domains preserve the common prefix; fixed witnesses saturate constant LIMIT rows or make EXISTS true independently of valid workload growth",
                                         "physical_cost_guaranteed": False})
    raise PopulationError("No compatible joint witness and workload assignment satisfies the complete query forest")


def _skipping_facts(located, symbols, anchor, component_address, root_values, analysis):
    """A generic work objective: a scalar sibling decides AND/OR's result.

    This establishes an opportunity to omit the selected subtree, not a claim
    about an optimizer's physical evaluation strategy. It is a search priority;
    the enclosing query's exact output requirements can rule it out.
    """
    nodes = {_address_id(address): (address, value) for address, value in located}
    producers = {symbol["symbol_id"]: symbol.get("producer_node") for symbol in analysis.symbols}

    def scalar(value, address, seen=frozenset()):
        if isinstance(value, Literal):
            return value.value
        if isinstance(value, Property):
            if symbols.get(_address_id(address)) != anchor:
                raise PopulationError("A guard reads outside the fixed anchor")
            return root_values.get(value.key)
        if isinstance(value, Variable):
            symbol = symbols.get(_address_id(address))
            producer = producers.get(symbol)
            if symbol in seen or producer not in nodes:
                raise PopulationError("A guard alias lacks a resolvable scalar producer")
            location, expression = nodes[producer]
            return scalar(expression, location, seen | {symbol})
        if isinstance(value, Comparison):
            return _compare(scalar(value.left, (*address, "left"), seen), value.operator,
                            scalar(value.right, (*address, "right"), seen))
        if isinstance(value, Boolean):
            return _logical(value.operator, [scalar(term, (*address, "terms", i), seen)
                                              for i, term in enumerate(value.terms)])
        if isinstance(value, Negation):
            result = scalar(value.expression, (*address, "expression"), seen)
            return None if result is None else not result
        if isinstance(value, Case):
            field = "then" if scalar(value.condition, (*address, "condition"), seen) is True else "otherwise"
            return scalar(getattr(value, field), (*address, field), seen)
        if isinstance(value, Call) and value.name == "coalesce":
            return next((result for i, part in enumerate(value.args)
                         if (result := scalar(part, (*address, "args", i), seen)) is not None), None)
        raise PopulationError("A guard is not a pure root scalar")

    facts = []
    for address, value in located:
        if not isinstance(value, Boolean):
            continue
        decisive = False if value.operator == "AND" else True
        containing = [i for i in range(len(value.terms))
                      if component_address[:len(address) + 2] == (*address, "terms", i)]
        if not containing:
            continue
        for i, term in enumerate(value.terms):
            if i in containing:
                continue
            location = (*address, "terms", i)
            try:
                result = scalar(term, location)
            except PopulationError:
                continue
            if result is decisive:
                facts.append({"boolean_address": list(address), "guard_address": list(location),
                              "operator": value.operator, "guard_value": result,
                              "expanded_subtree": list(component_address)})
    return facts


def _quantity_relations(located, components, quantities):
    """Record products only inside the actual MATCH chain that composes them.

    EXISTS projections and separate correlated subqueries do not multiply their
    child row counts into the enclosing query. COUNT's actual observation is
    recorded separately by the concrete interpreter.
    """
    by_address = {component["address"]: i for i, component in enumerate(components)}
    chains = []
    for address, value in located:
        if not isinstance(value, Query):
            continue
        if value.pipeline:
            clauses = [(clause, (*address, "pipeline", i)) for i, clause in enumerate(value.pipeline)]
        else:
            clauses = [(Match(value.pattern, value.where), address)] if value.pattern is not None else []
            clauses.extend((clause, (*address, "matches", i)) for i, clause in enumerate(value.matches))
        factors, indices, start = [], [], 0
        def finish():
            if factors:
                chains.append({"query_address": list(address), "segment_start": start,
                               "components": list(indices), "factors": list(factors),
                               "operator": "per_input_anchor_match_product_before_projection_distinct_and_other_filters",
                               "incoming_bag_multiplicity": "accounted separately; this is a relative product per incoming binding"})
        for position, (clause, clause_address) in enumerate(clauses):
            if not isinstance(clause, Match):
                finish()
                factors, indices, start = [], [], position + 1
                continue
            index = by_address.get((*clause_address, "pattern"))
            if index is not None:
                factors.append(quantities[index])
                indices.append(index)
        finish()
    return {"component_match_quantities": list(quantities), "mandatory_match_chains": chains,
            "consumer_rows": "actual COUNT/EXISTS observations are in subquery_outcomes"}


def _synthesize_fixed_components(query, located, *, scale, data_seed, result_rows,
                                 max_models, expected_values):
    """Solve local truth and match quantities for a lexical shared-anchor star.

    This rule is selected by AST operator shape, not a pattern identifier. All
    components share one imported entity; endpoints remain fresh. Relations or
    label domains that make a component read another's rows are rejected.
    """
    values = [value for _, value in located]
    edges = [(address, value) for address, value in located if isinstance(value, Edge)]
    if not edges or len(edges) > 3:
        raise PopulationError("Fixed-state synthesis supports one to three outgoing matching components")
    if any(isinstance(value, All) for value in values):
        raise PopulationError("Mixed path and fixed-component quantifiers need a joint construction proof")
    if any(isinstance(value, Literal) and type(value.value) not in (type(None), bool, int) for value in values):
        raise PopulationError("Automatic population supports boolean/integer/null literals only")
    analysis = analyze_dependencies(query)
    symbols = _symbol_lookup(analysis)
    address_map = dict(located)
    components = []
    for address, edge in edges:
        if (edge.direction != "out" or not edge.relationship.type or edge.relationship.alternatives
                or edge.relationship.variable is not None or edge.path_variable is not None
                or edge.relationship.min_hops is not None or edge.relationship.max_hops is not None
                or not edge.right.labels):
            raise PopulationError("Fixed components require typed outgoing single edges with labeled fresh endpoints")
        left = symbols.get(_address_id((*address, "left", "variable")))
        right = symbols.get(_address_id((*address, "right", "variable")))
        if left is None or right is None or left == right:
            raise PopulationError("Fixed component endpoints need distinct lexical bindings")
        right_definition = next(symbol["definition"] for symbol in analysis.symbols if symbol["symbol_id"] == right)
        if right_definition != _address_id((*address, "right", "variable")):
            raise PopulationError("A fixed component endpoint must be freshly defined at its matching site")
        parent = address_map.get(address[:-1])
        predicate = getattr(parent, "where", None)
        predicate_address = (*address[:-1], "where")
        if predicate is not None:
            for relative, part in _located(predicate):
                if isinstance(part, Property):
                    if (symbols.get(_address_id((*predicate_address, *relative))) != right
                            or part.key == "uid"):
                        raise PopulationError("Component filters must read only local non-identity endpoint properties")
                elif isinstance(part, (Count, Exists, All, Variable)):
                    raise PopulationError("Cross-component or nested filter dependencies are outside the star proof")
        components.append({"address": address, "pattern": edge, "anchor_symbol": left,
                           "endpoint_symbol": right, "predicate": predicate})
    anchor = components[0]["anchor_symbol"]
    if any(component["anchor_symbol"] != anchor for component in components):
        raise PopulationError("Fixed components must share the same lexical anchor binding")
    endpoint_addresses = {(*address, "right") for address, _ in edges}
    root_nodes = []
    for address, value in located:
        if not isinstance(value, Node) or address in endpoint_addresses:
            continue
        if symbols.get(_address_id((*address, "variable"))) != anchor:
            raise PopulationError("An additional node scan is outside the fixed-anchor read closure")
        root_nodes.append(value)
    root_labels = {label for node in root_nodes for label in node.labels}
    if not root_labels:
        raise PopulationError("The common anchor needs a labeled fixed domain")
    for component in components:
        if root_labels.intersection(component["pattern"].right.labels):
            raise PopulationError("Anchor and amplified endpoint label domains overlap")
    for i, first in enumerate(components):
        for second in components[i + 1:]:
            left, right = first["pattern"], second["pattern"]
            if (left.relationship.type == right.relationship.type
                    and (set(left.right.labels).issubset(right.right.labels)
                         or set(right.right.labels).issubset(left.right.labels))):
                raise PopulationError("Component relationship/endpoint read domains overlap")
    # Every graph read has now been assigned to a fixed root or one exclusive
    # component. Each amplified match must fail its own mandatory local WHERE.
    root_fixed = _merge_properties(root_nodes)
    root_keys = {value.key for address, value in located if isinstance(value, Property)
                 and symbols.get(_address_id(address)) == anchor}
    root_domains = _component_domains(located, symbols, anchor)
    root_domains.setdefault("uid", (1,))
    choices = []
    false_choices = []
    for component in components:
        edge, predicate = component["pattern"], component["predicate"]
        fixed = _merge_properties([edge.right])
        keys = {part.key for part in _walk(predicate) if isinstance(part, Property)} if predicate is not None else set()
        domains = _component_domains(located, symbols, component["endpoint_symbol"])
        assignments = list(_valuations(keys, fixed, domains))
        good = [props for props in assignments if predicate is None or _scalar(predicate, props) is True]
        bad = [props for props in assignments if predicate is not None and _scalar(predicate, props) is False
               and "uid" not in props]
        choices.append(good or [fixed])
        false_choices.append(bad)
        component["can_match"] = bool(good)
    expanding = [i for i in reversed(range(len(components))) if false_choices[i]]
    if not expanding:
        raise PopulationError("No local endpoint filter has an explicit false valuation for safe amplification")
    maximum = min(64, max([result_rows, 1, *_count_bounds(located, analysis)]) + 1)
    attempts = 0
    root_assignments = list(_valuations(root_keys, root_fixed, root_domains))
    root_assignments.sort(key=lambda props: -max(len(_skipping_facts(located, symbols, anchor,
                                                                     components[i]["address"], props, analysis))
                                                for i in expanding))
    for root_values in root_assignments:
        for good_values in product(*choices):
            for quantities in product(range(maximum + 1), repeat=len(components)):
                if sum(quantities) > 64 or any(count and not component["can_match"]
                                               for count, component in zip(quantities, components)):
                    continue
                ordered_expansion = sorted(expanding, key=lambda i: -len(_skipping_facts(
                    located, symbols, anchor, components[i]["address"], root_values, analysis)))
                for expanded in ordered_expansion:
                    for bad_value in false_choices[expanded]:
                        attempts += 1
                        if attempts > max_models:
                            raise PopulationError("Finite model search budget exhausted; satisfiability is unknown")
                        small = _fixed_model(components, root_nodes, root_values, good_values, bad_value,
                                             quantities, expanded, 1, data_seed)
                        interpreter = _Interpreter(small)
                        observed = interpreter.run(query)
                        if (not _satisfies_output(observed, result_rows, expected_values)
                                or any(isinstance(item, (_Vertex, _Path)) for row in observed for item in row.values())):
                            continue
                        for probe in (0, 2, 3):
                            trial = _fixed_model(components, root_nodes, root_values, good_values, bad_value,
                                                 quantities, expanded, probe, data_seed)
                            if _bag(evaluate_query(query, trial)) != _bag(observed):
                                raise PopulationError("A rejected component candidate changed the complete result bag")
                        spec = _fixed_model(components, root_nodes, root_values, good_values, bad_value,
                                            quantities, expanded, scale, data_seed)
                        facts = []
                        for i, (component, quantity, props) in enumerate(zip(components, quantities, good_values)):
                            failed = scale if i == expanded else 0
                            facts.append({"address": list(component["address"]),
                                          "anchor_symbol": anchor, "endpoint_symbol": component["endpoint_symbol"],
                                          "relationship_type": component["pattern"].relationship.type,
                                          "endpoint_labels": list(component["pattern"].right.labels),
                                          "required_domain": [0, maximum], "valid_matches": quantity,
                                          "failed_candidates": failed, "candidate_matches": quantity + failed,
                                          "true_properties": props,
                                          "false_properties": bad_value if i == expanded else None})
                        constraints = {"components": facts, "expanded_component": expanded,
                                       "root_properties": root_values, "requested_result_rows": result_rows,
                                       "expected_values": expected_values, "count_search_domain": [0, maximum]}
                        skipping = _skipping_facts(located, symbols, anchor,
                                                  components[expanded]["address"], root_values, analysis)
                        constraints.update(skippable=bool(skipping), decisive_guards=skipping)
                        evidence = {"method": "whole_query_cardinality_and_truth_search",
                                    "normal_form": "fixed lexical anchor with exclusive outgoing endpoint domains",
                                    "proof": "all graph reads are closed over the anchor or an exclusive component; added candidates fail their own local mandatory filter",
                                    "checked_failure_counts": [0, 1, 2, 3], "models_tried": attempts,
                                    "validation_graph_spec": small.to_dict(),
                                    "subquery_outcomes": interpreter.subquery_outcomes,
                                    "quantity_relation": _quantity_relations(located, components, quantities),
                                    "work_objective": "prefer a decisive root-scalar sibling when the complete output contract permits it",
                                    "skippable": bool(skipping), "decisive_guards": skipping,
                                    "physical_cost_guaranteed": False}
                        return PopulationResult(spec, observed, render_state_checks(spec.state_checks()), constraints, evidence)
    raise PopulationError("No compatible model found in the finite component truth/cardinality domain")


def render_setup(spec: GraphSpec) -> list[str]:
    """Render one bounded explicit state; SQL never reconstructs a pattern ID."""
    nodes, edges = spec._groups()
    declared_indexes = [f"CREATE INDEX {index['name']} IF NOT EXISTS FOR (n:{index['label']}) ON (n.{index['property']})"
                        for index in spec.indexes]
    large = any(group.count > 128 for group in edges)
    result = [] if large else list(declared_indexes)
    def literal(value):
        if hasattr(value, "base"):
            return f"({value.base} + {value.step} * i)"
        if value is None:
            return "null"
        if type(value) is bool:
            return str(value).lower()
        return json.dumps(value, ensure_ascii=False)
    for group in nodes:
        props = ", ".join(f"{key}: {literal(value)}" for key, value in group.properties.items())
        prefix = f"UNWIND range(0, {group.count - 1}) AS i "
        result.append(prefix + f"CREATE (n{''.join(':' + label for label in group.labels)} {{{props}}})")
    def relation(group):
        props = ", ".join(f"{key}: {literal(value)}" for key, value in group.properties.items())
        return f"[:{group.type}" + (" {" + props + "}" if props else "") + "]"

    if not large:
        for group in edges:
            result.append(f"UNWIND range(0, {group.count - 1}) AS i "
                          f"MATCH (u:{group.source_label}), (v:{group.target_label}) "
                          f"WHERE u.uid = {literal(group.source)} AND v.uid = {literal(group.target)} "
                          f"CREATE (u)-{relation(group)}->(v)")
        return result

    # Loading must not perform N unindexed label scans for N endpoints. Reuse
    # the existing reserved index name for one temporary target-UID index at
    # a time, then remove it and restore precisely the declared query indexes.
    # A failed load still leaves only the index name accepted by restore().
    for label in dict.fromkeys(group.target_label for group in edges):
        result.extend(("DROP INDEX gsp_cost_lookup IF EXISTS",
                       f"CREATE INDEX gsp_cost_lookup FOR (n:{label}) ON (n.uid)",
                       "CALL db.awaitIndexes(20)"))
        for group in (item for item in edges if item.target_label == label):
            if hasattr(group.source, "base") and group.source.step != 0:
                source = group.source
                prefix = (f"MATCH (u:{group.source_label}) "
                          f"WITH u, toInteger((u.uid - {source.base}) / {source.step}) AS i "
                          f"WHERE i >= 0 AND i < {group.count} AND u.uid = {literal(source)} ")
            else:
                source = group.source.base if hasattr(group.source, "base") else group.source
                prefix = (f"MATCH (u:{group.source_label} {{uid: {source}}}) "
                          f"UNWIND range(0, {group.count - 1}) AS i ")
            result.append(prefix + f"MATCH (v:{label}) WHERE v.uid = {literal(group.target)} "
                          f"CREATE (u)-{relation(group)}->(v)")
    result.append("DROP INDEX gsp_cost_lookup IF EXISTS")
    result.extend(declared_indexes)
    return result
