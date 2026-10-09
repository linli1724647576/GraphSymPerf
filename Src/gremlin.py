"""Compile the checked IR to a finite, lambda-free Gremlin traversal fragment.

This is an explicit traversal compiler, not a Cypher text translator. Its
start vertex and clause order are intentional traversal instructions, so a
timing difference alone does not establish a missed optimizer rule.
"""

from __future__ import annotations

from typing import Iterable

from ir import (
    Alias, Boolean, Call, Case, Comparison, ConditionalQuery, Count, Edge,
    Exists, Literal, Negation, Node, Property, Query, QueryTree,
    UnionAll, clauses, requirements,
)
from query import projection_names


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
    return False


class _Compiler:
    def __init__(self, target: str):
        self.target = target
        self.bindings = {}
        self.labels = {}
        self.serial = 0
        self.null_projection = False

    def unsupported(self, feature: str):
        from languages import UnsupportedFeature
        raise UnsupportedFeature(self.target, (feature,))

    def parameter(self, value) -> str:
        key = f"_gsp_p{len(self.bindings)}"
        self.bindings[key] = value
        return key

    def label(self, variable: str) -> str:
        if variable not in self.labels:
            self.labels[variable] = f"_gsp_v{len(self.labels)}"
        return _quote(self.labels[variable])

    def temporary(self) -> str:
        label = _quote(f"_gsp_tmp{self.serial}")
        self.serial += 1
        return label

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
                text += f".has({_quote(key)},P.eq({self.parameter(value.value)}))"
        return text

    def pattern(self, pattern, bound: set[str], prefix: str = "__") -> tuple[str, set[str]]:
        local = set(bound)
        left = pattern.left if isinstance(pattern, Edge) else pattern
        text = prefix + (f".select({self.label(left.variable)})" if left.variable in local else ".V()")
        text += self.constraints(left)
        if left.variable not in local:
            text += f".as({self.label(left.variable)})"
            local.add(left.variable)
        if isinstance(pattern, Node):
            return text, local
        rel = pattern.relationship
        if rel.min_hops is not None or pattern.path_variable is not None:
            self.unsupported("bounded_path")
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

    def subquery(self, expression: Exists | Count, bound: set[str]) -> str:
        text, local = self.pattern(expression.pattern, bound)
        if expression.where is not None:
            text += f".filter({self.predicate(expression.where, local)})"
        return text

    def scalar(self, expression, bound: set[str]) -> str:
        """One scalar per input, including explicit NULL for absent properties."""
        if isinstance(expression, Literal):
            self.null_projection |= expression.value is None
            return f"__.constant({self.parameter(expression.value)})"
        if isinstance(expression, Property):
            self.null_projection = True
            return (f"__.select({self.label(expression.variable)}).coalesce("
                    f"__.values({_quote(expression.key)}),__.constant(null))")
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
        if isinstance(expression, Comparison):
            operator = expression.operator
            if operator in {"IS NULL", "IS NOT NULL"}:
                null = (operator == "IS NULL") == truth
                if isinstance(expression.left, Property):
                    return (f"__.select({self.label(expression.left.variable)})"
                            f".{'hasNot' if null else 'has'}({_quote(expression.left.key)})")
                return self.scalar(expression.left, bound) + f".is(P.{'eq' if null else 'neq'}(null))"
            operator = operator if truth else _NEGATE[operator]
            left, right = expression.left, expression.right
            if isinstance(left, Literal) and isinstance(right, Literal):
                if left.value is None or right.value is None:
                    return "__.limit(0)"
                # Leave scalar equality/order to the engine's numeric comparer.
                return f"__.constant({self.parameter(left.value)}).is(P.{_PREDICATE[operator]}({self.parameter(right.value)}))"
            if isinstance(right, Literal):
                if right.value is None:
                    return "__.limit(0)"
                # Raw values preserve UNKNOWN when a property is absent and
                # avoid requiring nullable intermediate traversers for filters.
                if isinstance(left, Property):
                    value = f"__.select({self.label(left.variable)}).values({_quote(left.key)})"
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
        bound = set()
        text = prefix
        for clause in clauses(query):
            if clause.optional:
                self.unsupported("optional_match")
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
        return text


def generate_gremlin(query: QueryTree, target: str = "tinkergraph", *,
                     verified_features: Iterable[str] = ()):
    """Generate parameterized Gremlin after IR scope/type validation.

    The loader must use SINGLE vertex-property cardinality and preserve one
    relationship per source edge. Providers must probe NULL result transport
    before enabling ``gremlin_null_projection``. OPTIONAL and variable-length
    paths are rejected rather than translated with different semantics.
    """
    needed = requirements(query)
    from languages import CompiledQuery
    compiler = _Compiler(target)
    if isinstance(query, ConditionalQuery):
        compiler.unsupported("conditional_query")
    if isinstance(query, UnionAll):
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
