"""Compile the checked IR to a finite, lambda-free Gremlin traversal fragment.

This is an explicit traversal compiler, not a Cypher text translator. Its
start vertex and clause order are intentional traversal instructions, so a
timing difference alone does not establish a missed optimizer rule.
"""

from __future__ import annotations

import json
from typing import Iterable

from ir import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count, Edge,
    Exists, ListExpr, Literal, Negation, Node, Property, Query, QueryTree,
    UnionAll, Variable, clauses, requirements,
)
from query import projection_names
from languages import gremlin_property_key


_PREDICATE = {"=": "eq", "<>": "neq", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
_DUAL = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_NEGATE = {"=": "<>", "<>": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def _quote(identifier: str) -> str:
    # All identifiers have already passed the shared IR identifier validator.
    return "'" + identifier + "'"


def _floating_projection(expression) -> bool:
    if isinstance(expression, Literal):
        return type(expression.value) is float
    if isinstance(expression, Alias):
        return _floating_projection(expression.expression)
    if isinstance(expression, Case):
        return _floating_projection(expression.then) or _floating_projection(expression.otherwise)
    if isinstance(expression, Call) and expression.name == "coalesce":
        return any(_floating_projection(item) for item in expression.args)
    if isinstance(expression, ListExpr):
        return any(_floating_projection(item) for item in expression.items)
    if isinstance(expression, Concat):
        return _floating_projection(expression.left) or _floating_projection(expression.right)
    return False


class _Compiler:
    def __init__(self, target: str):
        self.target = target
        self.bindings = {}
        self.labels = {}
        self.serial = 0
        self.null_projection = False
        self.nullable = set()
        self.boxed_scalars = set()

    def unsupported(self, feature: str):
        from languages import UnsupportedFeature
        raise UnsupportedFeature(self.target, (feature,))

    def parameter(self, value) -> str:
        key = f"_gsp_p{len(self.bindings)}"
        self.bindings[key] = value
        return key

    def constant(self, value) -> str:
        if self.target != "arcadedb":
            return self.parameter(value)
        # ArcadeDB 26.10.2's Java Gremlin grammar accepts variables in P.eq,
        # but constant() requires a literal. The IR has already checked the
        # scalar type, finite floats and signed 64-bit integer range.
        if value is None:
            return "null"
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) is int:
            return f"{value}L"
        if type(value) is float:
            return repr(value) + "D"
        if type(value) is str:
            # Single quotes also avoid Groovy interpolation if an operator
            # explicitly selected that engine outside this client.
            return "'" + json.dumps(value, ensure_ascii=True)[1:-1].replace("'", "\\'") + "'"
        self.unsupported("gremlin_literal_type")

    def label(self, variable: str) -> str:
        if variable not in self.labels:
            self.labels[variable] = f"_gsp_v{len(self.labels)}"
        return _quote(self.labels[variable])

    def temporary(self) -> str:
        label = _quote(f"_gsp_tmp{self.serial}")
        self.serial += 1
        return label

    def entity_guard(self, variable: str) -> str:
        # TinkerPop 3.5.1 (HugeGraph 1.7.0) cannot attach path labels to a
        # NULL object. Only its nullable entity bindings use Boolean FALSE as
        # an internal sentinel. The IR never binds a boolean as a node, edge
        # or path, so this cannot collide with any legitimate entity value.
        marker = "false" if self.target == "hugegraph" and variable in self.nullable else "null"
        return f".is(P.neq({marker}))"

    def constraints(self, node_or_rel) -> str:
        text = ""
        if isinstance(node_or_rel, Node):
            if len(node_or_rel.labels) > 1:
                self.unsupported("multi_label_conjunction")
            for label in node_or_rel.labels:
                text += f".hasLabel({_quote(label)})"
        for key, value in node_or_rel.properties:
            # A Cypher property-map NULL comparison never evaluates to true.
            if value.value is None:
                text += ".limit(0)"
            else:
                text += f".has({_quote(gremlin_property_key(key, self.target))},P.eq({self.parameter(value.value)}))"
        return text

    def pattern(self, pattern, bound: set[str], prefix: str = "__") -> tuple[str, set[str]]:
        local = set(bound)
        left = pattern.left if isinstance(pattern, Edge) else pattern
        text = prefix + (f".select({self.label(left.variable)})" if left.variable in local else ".V()")
        if left.variable in local and left.variable in self.nullable:
            text += self.entity_guard(left.variable)
        text += self.constraints(left)
        if left.variable not in local:
            text += f".as({self.label(left.variable)})"
            local.add(left.variable)
        if isinstance(pattern, Node):
            return text, local
        rel = pattern.relationship
        if rel.min_hops is not None or pattern.path_variable is not None:
            return self.path(pattern, local, text)
        types = (rel.type, *rel.alternatives) if rel.type else ()
        arguments = ",".join(_quote(name) for name in types)
        relation = self.constraints(rel)
        if rel.variable is not None:
            relation += (f".where(P.eq({self.label(rel.variable)}))" if rel.variable in local
                         else f".as({self.label(rel.variable)})")
            local.add(rel.variable)
        if pattern.direction == "undirected":
            # bothE() counts a self-loop twice. Emit every outgoing edge and
            # only incoming non-self-loops; parallel edges retain their rows.
            outgoing = f"__.outE({arguments}){relation}.inV()"
            incoming = (f"__.inE({arguments}){relation}.outV()"
                        f".where(P.neq({self.label(left.variable)}))")
            text += f".union({outgoing},{incoming})"
        else:
            step, endpoint = ("outE", "inV") if pattern.direction == "out" else ("inE", "outV")
            text += f".{step}({arguments}){relation}.{endpoint}()"
        right = pattern.right
        if right.variable in local:
            text += f".where(P.eq({self.label(right.variable)}))"
        text += self.constraints(right)
        if right.variable not in local:
            text += f".as({self.label(right.variable)})"
            local.add(right.variable)
        return text, local

    def path(self, pattern: Edge, local: set[str], text: str) -> tuple[str, set[str]]:
        """A per-traverser TRAIL: remember edges, never deduplicate result rows.

        Repeated edge labels retain the complete edge history (Pop.all).
        Testing the newly selected edge's occurrence count rejects only edge
        reuse; repeated vertices and parallel-edge paths remain legal.
        """
        rel = pattern.relationship
        if rel.path_mode != "trail":
            self.unsupported("gremlin_path_mode_" + rel.path_mode)
        if rel.variable is not None:
            self.unsupported("gremlin_named_path_relationship")
        low = rel.min_hops if rel.min_hops is not None else 1
        high = rel.max_hops if rel.max_hops is not None else 1
        # These paths use loops()/times(), whose oldest supported traverser
        # has a signed-short loop counter. Existing P3 uses at most 3 hops.
        if high > 2**15 - 1:
            self.unsupported("gremlin_path_hop_bound")
        nodes, edges, previous = self.temporary(), self.temporary(), self.temporary()
        text += f".as({nodes})"
        arguments = ",".join(_quote(name) for name in ((rel.type, *rel.alternatives) if rel.type else ()))
        relation = (self.constraints(rel) + f".as({edges})"
                    f".filter(__.select(Pop.all,{edges}).unfold().where(P.eq({edges}))"
                    ".count().is(P.eq(1)))")
        outgoing = f"__.outE({arguments}){relation}.inV()"
        incoming = f"__.inE({arguments}){relation}.outV()"
        if pattern.direction == "undirected":
            hop = f"__.as({previous}).union({outgoing},{incoming}.where(P.neq({previous})))"
        else:
            hop = outgoing if pattern.direction == "out" else incoming
        hop += f".as({nodes})"
        if high:
            if low == 0:
                text += f".emit().repeat({hop}).times({high})"
            else:
                text += f".repeat({hop}).emit(__.loops().is(P.gte({low}))).times({high})"
        right = pattern.right
        if right.variable in local:
            text += f".where(P.eq({self.label(right.variable)}))"
        text += self.constraints(right)
        if right.variable not in local:
            text += f".as({self.label(right.variable)})"
            local.add(right.variable)
        if pattern.path_variable is not None:
            if pattern.path_variable in local:
                self.unsupported("gremlin_rebound_path")
            # The zero-hop path has no edge label, hence its length is zero.
            # CountLocalStep and integer HTTP bindings use Long. Keep zero
            # the same type, including when nested in a DISTINCT list key.
            zero = "0L" if low == 0 else "0"
            text += (".project('_nodes','_length')"
                     f".by(__.select(Pop.all,{nodes}))"
                     f".by(__.coalesce(__.select(Pop.all,{edges}).count(Scope.local),__.constant({zero})))"
                     f".as({self.label(pattern.path_variable)}).select({self.label(right.variable)})")
            local.add(pattern.path_variable)
        return text, local

    def range_list(self, expression: Call) -> str:
        # Bounds remain scalar literals; only the cardinality/last bound are
        # calculated here. Every element and the resulting list are generated
        # on the server. Long literals avoid Integer overflow and MathStep's
        # conversion to double (which loses precision above 2**53).
        if any(not isinstance(arg, Literal) or type(arg.value) is not int for arg in expression.args):
            self.unsupported("gremlin_dynamic_range")
        start, end = (arg.value for arg in expression.args[:2])
        step = expression.args[2].value if len(expression.args) == 3 else 1
        count = max(0, (end - start) // step + 1)
        if count > 2**31:
            self.unsupported("gremlin_range_repeat_bound")
        if count == 0:
            return "__.limit(0).fold()"
        text = f"__.constant({start}L)"
        if count > 1:
            last = start + (count - 1) * step
            # TinkerPop 3.5.1's traverser loop counter is a signed short.
            # Terminate by the exact last value, not times(count), so ranges
            # above 32767 elements cannot wrap that counter and run forever.
            text += (".sack(Operator.assign).by(__.identity()).emit()"
                     f".until(__.is(P.eq({last}L)))"
                     f".repeat(__.sack(Operator.sum).by(__.constant({step}L)).sack())")
        return text + ".fold()"

    def list_items_total(self, expression) -> bool:
        """Conservative nullability proof, never evaluate the experimental predicate."""
        if isinstance(expression, ListExpr):
            return all(isinstance(item, Literal) and item.value is not None for item in expression.items)
        if isinstance(expression, Concat):
            return self.list_items_total(expression.left) and self.list_items_total(expression.right)
        return isinstance(expression, Call) and expression.name == "range"

    def total_predicate(self, expression, variable: str) -> bool:
        def scalar_total(item):
            return ((isinstance(item, Variable) and item.name == variable)
                    or isinstance(item, Literal) and item.value is not None)
        if isinstance(expression, Literal):
            return type(expression.value) is bool
        if isinstance(expression, Comparison):
            return (expression.operator in {"IS NULL", "IS NOT NULL"}
                    or scalar_total(expression.left) and scalar_total(expression.right))
        if isinstance(expression, Negation):
            return self.total_predicate(expression.expression, variable)
        if isinstance(expression, Boolean):
            return all(self.total_predicate(item, variable) for item in expression.terms)
        if isinstance(expression, Case):
            return (self.total_predicate(expression.then, variable)
                    and self.total_predicate(expression.otherwise, variable))
        if isinstance(expression, Call) and expression.name == "coalesce":
            return any(self.total_predicate(item, variable) for item in expression.args)
        return False

    def all_items(self, expression: All, bound: set[str]) -> str:
        if isinstance(expression.items, Call) and expression.items.name == "nodes":
            path = expression.items.args[0]
            self.null_projection = True
            return (f"__.coalesce(__.select({self.label(path.name)}){self.entity_guard(path.name)}"
                    ".select('_nodes'),__.constant(null))")
        return self.scalar(expression.items, bound)

    def all_binding(self, expression: All) -> tuple[str, set[str]]:
        previous = self.boxed_scalars
        self.boxed_scalars = set(previous)
        elements = "__.unfold()"
        nodes = isinstance(expression.items, Call) and expression.items.name == "nodes"
        if self.target == "hugegraph" and not nodes and not self.list_items_total(expression.items):
            # A list may contain NULL, and 3.5.1 cannot label that traverser.
            # Box only nullable scalar binders. Existing non-null P16 lists
            # stay unboxed; a real boolean FALSE remains an ordinary value.
            elements += ".project('_item').by(__.identity())"
            self.boxed_scalars.add(expression.variable)
        return elements + f".as({self.label(expression.variable)})", previous

    def all_scalar(self, expression: All, bound: set[str]) -> str:
        self.null_projection = True
        local = bound | {expression.variable}
        elements, previous_boxed = self.all_binding(expression)
        yes = self.predicate(expression.predicate, local)
        if self.list_items_total(expression.items) and self.total_predicate(expression.predicate, expression.variable):
            # Total-predicate P16 variants take this one ordered scan.
            outcome = f"__.choose({elements}.not({yes}),__.constant(false),__.constant(true))"
        else:
            no = self.predicate(expression.predicate, local, False)
            # NULL cannot terminate ALL: a later FALSE must dominate it.
            # The general nullable fallback may scan twice; the second scan
            # is not entered when a P16 FALSE has already won.
            unknown = f"{elements}.not(__.or({yes},{no}))"
            outcome = (f"__.choose({elements}.filter({no}),__.constant(false),"
                       f"__.choose({unknown},__.constant(null),__.constant(true)))")
        self.boxed_scalars = previous_boxed
        return (f"__.map({self.all_items(expression, bound)}).choose(__.is(P.eq(null)),"
                f"__.constant(null),{outcome})")

    def subquery(self, expression: Exists | Count, bound: set[str]) -> str:
        text, local = self.pattern(expression.pattern, bound)
        if expression.where is not None:
            text += f".filter({self.predicate(expression.where, local)})"
        return text

    def scalar(self, expression, bound: set[str]) -> str:
        """One scalar per input, including explicit NULL for absent properties."""
        if isinstance(expression, Literal):
            self.null_projection |= expression.value is None
            return f"__.constant({self.constant(expression.value)})"
        if isinstance(expression, Property):
            self.null_projection = True
            guard = self.entity_guard(expression.variable) if expression.variable in self.nullable else ""
            return (f"__.select({self.label(expression.variable)}).coalesce("
                    f"__{guard}.values({_quote(gremlin_property_key(expression.key, self.target))}),__.constant(null))")
        if isinstance(expression, Variable):
            unbox = ".select('_item')" if expression.name in self.boxed_scalars else ""
            return f"__.select({self.label(expression.name)}){unbox}"
        if isinstance(expression, ListExpr):
            if not expression.items:
                return "__.limit(0).fold()"
            return "__.union(" + ",".join(self.scalar(item, bound) for item in expression.items) + ").fold()"
        if isinstance(expression, Concat):
            self.null_projection = True
            if self.target == "hugegraph":
                # Evaluate both lists once, in order, without labelling a
                # possible NULL list. ProjectStep carries NULL map values.
                return (f"__.project('_left','_right').by({self.scalar(expression.left, bound)})"
                        f".by({self.scalar(expression.right, bound)})"
                        ".choose(__.or(__.select('_left').is(P.eq(null)),__.select('_right').is(P.eq(null))),"
                        "__.constant(null),__.union(__.select('_left').unfold(),__.select('_right').unfold()).fold())")
            left, right = self.temporary(), self.temporary()
            return (f"__.map({self.scalar(expression.left, bound)}).as({left})"
                    f".map({self.scalar(expression.right, bound)}).as({right})"
                    f".choose(__.or(__.select({left}).is(P.eq(null)),__.select({right}).is(P.eq(null))),"
                    f"__.constant(null),__.union(__.select({left}).unfold(),__.select({right}).unfold()).fold())")
        if isinstance(expression, All):
            return self.all_scalar(expression, bound)
        if isinstance(expression, Call) and expression.name == "range":
            return self.range_list(expression)
        if isinstance(expression, Call) and expression.name == "length":
            self.null_projection = True
            if expression.args[0] == Literal(None):
                return "__.constant(null)"
            variable = expression.args[0].name
            return (f"__.coalesce(__.select({self.label(variable)}){self.entity_guard(variable)}"
                    ".select('_length'),__.constant(null))")
        if isinstance(expression, Call) and expression.name == "size":
            # count(local) matches list size, but counts a string as one.
            argument = expression.args[0]
            if not isinstance(argument, (ListExpr, Concat)) and not (
                    isinstance(argument, Call) and argument.name == "range") and argument != Literal(None):
                self.unsupported("gremlin_size_nonlist")
            self.null_projection = True
            return (f"__.map({self.scalar(argument, bound)}).choose(__.is(P.eq(null)),"
                    "__.constant(null),__.count(Scope.local))")
        if isinstance(expression, Count):
            return self.subquery(expression, bound) + ".count()"
        if isinstance(expression, Exists):
            return f"__.choose({self.subquery(expression, bound)},__.constant(true),__.constant(false))"
        if isinstance(expression, Case):
            return (f"__.choose({self.predicate(expression.condition, bound)},"
                    f"{self.scalar(expression.then, bound)},{self.scalar(expression.otherwise, bound)})")
        if isinstance(expression, Call) and expression.name == "coalesce":
            self.null_projection = True
            children = [self.scalar(arg, bound) + ".is(P.neq(null))" for arg in expression.args]
            return "__.coalesce(" + ",".join((*children, "__.constant(null)")) + ")"
        if isinstance(expression, (Boolean, Negation, Comparison)):
            self.null_projection = True
            return (f"__.choose({self.predicate(expression, bound)},__.constant(true),"
                    f"__.choose({self.predicate(expression, bound, False)},"
                    "__.constant(false),__.constant(null)))")
        self.unsupported("gremlin_scalar_" + type(expression).__name__.lower())

    def predicate(self, expression, bound: set[str], truth: bool = True) -> str:
        """Emit inputs for exactly TRUE or exactly FALSE, never for UNKNOWN.

        NOT is compiled by exchanging the truth tests. Negating a traversal
        that merely tests TRUE would incorrectly turn missing values into TRUE.
        """
        if isinstance(expression, Literal):
            return "__.identity()" if expression.value is truth else "__.limit(0)"
        if isinstance(expression, Negation):
            return self.predicate(expression.expression, bound, not truth)
        if isinstance(expression, Boolean):
            conjunction = (expression.operator == "AND") == truth
            children = ",".join(self.predicate(term, bound, truth) for term in expression.terms)
            return f"__.{'and' if conjunction else 'or'}({children})"
        if isinstance(expression, Exists):
            text = self.subquery(expression, bound)
            return text if truth else f"__.not({text})"
        if isinstance(expression, All):
            local = bound | {expression.variable}
            elements, previous_boxed = self.all_binding(expression)
            condition = (f"__.not({elements}.not({self.predicate(expression.predicate, local)}))" if truth
                         else f"{elements}.filter({self.predicate(expression.predicate, local, False)})")
            self.boxed_scalars = previous_boxed
            return f"__.map({self.all_items(expression, bound)}).is(P.neq(null)).filter({condition})"
        if isinstance(expression, Comparison):
            operator = expression.operator
            if operator in {"IS NULL", "IS NOT NULL"}:
                null = (operator == "IS NULL") == truth
                if isinstance(expression.left, Property) and expression.left.variable not in self.nullable:
                    return (f"__.select({self.label(expression.left.variable)})"
                            f".{'hasNot' if null else 'has'}({_quote(gremlin_property_key(expression.left.key, self.target))})")
                return self.scalar(expression.left, bound) + f".is(P.{'eq' if null else 'neq'}(null))"
            operator = operator if truth else _NEGATE[operator]
            left, right = expression.left, expression.right
            if isinstance(left, Literal) and isinstance(right, Literal):
                if left.value is None or right.value is None:
                    return "__.limit(0)"
                # Leave scalar equality/order to the engine's numeric comparer.
                return f"__.constant({self.constant(left.value)}).is(P.{_PREDICATE[operator]}({self.parameter(right.value)}))"
            if isinstance(right, Literal):
                if right.value is None:
                    return "__.limit(0)"
                # Raw values preserve UNKNOWN when a property is absent and
                # avoid requiring nullable intermediate traversers for filters.
                if isinstance(left, Property):
                    guard = self.entity_guard(left.variable) if left.variable in self.nullable else ""
                    value = (f"__.select({self.label(left.variable)}){guard}"
                             f".values({_quote(gremlin_property_key(left.key, self.target))})")
                else:
                    value = self.scalar(left, bound) + ".is(P.neq(null))"
                return value + f".is(P.{_PREDICATE[operator]}({self.parameter(right.value)}))"
            saved = self.temporary()
            lhs = self.scalar(left, bound) + f".is(P.neq(null)).as({saved})"
            rhs = (self.scalar(right, bound) + ".is(P.neq(null))"
                   f".where(P.{_PREDICATE[_DUAL[operator]]}({saved}))")
            return lhs + f".flatMap({rhs})"
        # Only known scalar expressions that the IR accepts as BOOLEAN reach
        # this branch; is(true/false) deliberately excludes NULL.
        return self.scalar(expression, bound) + f".is(P.eq({'true' if truth else 'false'}))"

    def query(self, query: Query, prefix: str) -> str:
        # Provider map equality for DISTINCT may distinguish integral and
        # floating representations that Cypher treats as the same number.
        if query.distinct and any(_floating_projection(item) for item in query.returns):
            self.unsupported("gremlin_numeric_distinct")
        previous_nullable, self.nullable = self.nullable, set()
        bound = set()
        text = prefix
        for clause in clauses(query):
            if clause.optional:
                branch, local = self.pattern(clause.pattern, bound)
                if clause.where is not None:
                    branch += f".filter({self.predicate(clause.where, local)})"
                added = local - bound
                self.null_projection |= bool(added)
                if added:
                    # CoalesceStep is a FlatMapStep: child path labels do not
                    # escape it. Carry new bindings as explicit map values,
                    # then bind them outside coalesce, including the provider's
                    # checked representation of a nullable entity.
                    names = sorted(added)
                    keys = tuple(f"b{index}" for index in range(len(names)))
                    project = ".project(" + ",".join(_quote(key) for key in keys) + ")"
                    branch += project + "".join(f".by(__.select({self.label(name)}))" for name in names)
                    marker = "false" if self.target == "hugegraph" else "null"
                    fallback = "__" + project + f".by(__.constant({marker}))" * len(names)
                    saved = self.temporary()
                    text += f".coalesce({branch},{fallback}).as({saved})"
                    for key, name in zip(keys, names):
                        text += f".select({saved}).select({_quote(key)}).as({self.label(name)})"
                else:
                    text += f".coalesce({branch},__.identity())"
                bound = local
                self.nullable.update(added)
            else:
                text, bound = self.pattern(clause.pattern, bound, text)
                if clause.where is not None:
                    text += f".filter({self.predicate(clause.where, bound)})"
        # A return-only query receives precisely the single injected input.
        physical = tuple(f"c{index}" for index in range(len(query.returns)))
        text += ".project(" + ",".join(_quote(name) for name in physical) + ")"
        for item in query.returns:
            expression = item.expression if isinstance(item, Alias) else item
            text += f".by({self.scalar(expression, bound)})"
        if query.distinct:
            text += ".dedup()"
        if query.limit is not None:
            text += f".limit({query.limit})"
        self.nullable = previous_nullable
        return text


def generate_gremlin(query: QueryTree, target: str = "tinkergraph", *,
                     verified_features: Iterable[str] = ()):
    """Generate parameterized Gremlin after IR scope/type validation.

    The loader must use SINGLE vertex-property cardinality and preserve one
    relationship per source edge. Providers must probe NULL result transport
    before enabling ``gremlin_null_projection``. Paths retain relationship
    uniqueness, OPTIONAL binds explicit NULL, and lists are built server-side.
    Dynamic range bounds, non-TRAIL paths and multi-label nodes remain rejected.
    """
    needed = requirements(query)
    from languages import CompiledQuery
    compiler = _Compiler(target)
    if isinstance(query, ConditionalQuery):
        # choose() routes the input only to the selected traversal. Test
        # exactly TRUE so FALSE and NULL both select ELSE, and retain both
        # branches in the text rather than folding the experimental guard.
        condition = compiler.predicate(query.condition, set())
        then = compiler.query(query.then, "__")
        otherwise = compiler.query(query.otherwise, "__")
        text = f"g.inject(1).choose({condition},{then},{otherwise})"
    elif isinstance(query, UnionAll):
        text = "g.inject(1).union(" + ",".join(compiler.query(branch, "__") for branch in query.branches) + ")"
    else:
        text = compiler.query(query, "g.inject(1)")
    if compiler.null_projection and "gremlin_null_projection" not in set(verified_features):
        compiler.unsupported("gremlin_null_projection")
    logical = projection_names(query)
    required = needed | {"gremlin_single_properties"}
    if compiler.null_projection:
        required |= {"gremlin_null_projection"}
    return CompiledQuery(text, tuple((f"c{index}", name) for index, name in enumerate(logical)),
                         required, compiler.bindings)


def validate_gremlin(query: QueryTree, target: str = "tinkergraph", *,
                     verified_features: Iterable[str] = ()) -> frozenset[str]:
    """Apply exactly the same finite feature gates used for generation."""
    return generate_gremlin(query, target, verified_features=verified_features).required_features
