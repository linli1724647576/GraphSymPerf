"""Language-independent, deliberately restricted query intermediate representation.

Tuple order is part of an experimental input: no normalization sorts labels,
boolean operands, MATCH clauses, or UNION branches. The IR validates scope and
scalar types without invoking any language generator.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import TypeAlias

class QueryError(ValueError):
    """A query lies outside the supported syntax, types, or variable scope."""


@dataclass(frozen=True, eq=False)
class Literal:
    value: None | bool | int | float | str

    def __eq__(self, other):
        return isinstance(other, Literal) and type(self.value) is type(other.value) and self.value == other.value

    def __hash__(self):
        return hash((type(self.value), self.value))


@dataclass(frozen=True)
class Property:
    variable: str
    key: str


@dataclass(frozen=True)
class Variable:
    name: str


@dataclass(frozen=True)
class Comparison:
    left: Expression
    operator: str
    right: Expression


@dataclass(frozen=True)
class Boolean:
    operator: str
    terms: tuple[Expression, ...]


@dataclass(frozen=True)
class Negation:
    expression: Expression


@dataclass(frozen=True)
class Node:
    variable: str
    labels: tuple[str, ...] = ()
    properties: tuple[tuple[str, Literal], ...] = ()


@dataclass(frozen=True)
class Relationship:
    variable: str | None = None
    type: str | None = None
    properties: tuple[tuple[str, Literal], ...] = ()
    alternatives: tuple[str, ...] = ()
    min_hops: int | None = None
    max_hops: int | None = None
    path_mode: str = "trail"


@dataclass(frozen=True)
class Edge:
    left: Node
    relationship: Relationship
    right: Node
    direction: str = "out"
    path_variable: str | None = None


Pattern: TypeAlias = Node | Edge


@dataclass(frozen=True)
class Exists:
    pattern: Pattern
    where: Expression | None = None


@dataclass(frozen=True)
class Count:
    """A scalar COUNT subquery, not an aggregate over the enclosing row stream."""

    pattern: Pattern
    where: Expression | None = None


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Expression, ...]


@dataclass(frozen=True)
class Case:
    condition: Expression
    then: Expression
    otherwise: Expression


@dataclass(frozen=True)
class ListExpr:
    items: tuple[Expression, ...]


@dataclass(frozen=True)
class Concat:
    left: Expression
    right: Expression


@dataclass(frozen=True)
class All:
    variable: str
    items: Expression
    predicate: Expression


Expression: TypeAlias = Literal | Variable | Property | Comparison | Boolean | Negation | Exists | Count | Call | Case | ListExpr | Concat | All


@dataclass(frozen=True)
class Alias:
    expression: Expression
    name: str


@dataclass(frozen=True)
class Match:
    pattern: Pattern
    where: Expression | None = None
    optional: bool = False


@dataclass(frozen=True)
class Query:
    pattern: Pattern | None
    where: Expression | None
    returns: tuple[Expression | Alias, ...]
    matches: tuple[Match, ...] = ()
    limit: int | None = None
    distinct: bool = False

    def render(self) -> str:
        from query import render_query
        return render_query(self)


@dataclass(frozen=True)
class UnionAll:
    branches: tuple[Query, ...]

    def render(self) -> str:
        from query import render_query
        return render_query(self)


@dataclass(frozen=True)
class ConditionalQuery:
    """A conditional query with exactly two ordinary branches."""

    condition: Expression
    then: Query
    otherwise: Query

    def render(self) -> str:
        from query import render_query
        return render_query(self)


def clauses(query: Query) -> tuple[Match, ...]:
    return (Match(query.pattern, query.where), *query.matches) if query.pattern is not None else ()


def with_clauses(query: Query, matches: tuple[Match, ...]) -> Query:
    if not matches:
        return replace(query, pattern=None, where=None, matches=())
    if matches[0].optional:
        raise QueryError("The first MATCH must be mandatory")
    return replace(query, pattern=matches[0].pattern, where=matches[0].where, matches=tuple(matches[1:]))


def _identifier(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise QueryError(f"Unsupported identifier: {value!r}")


def _validate_literal(value: Literal) -> None:
    if not isinstance(value, Literal) or type(value.value) not in (type(None), bool, int, float, str):
        raise QueryError("Only scalar literals are supported")
    if type(value.value) is float and not math.isfinite(value.value):
        raise QueryError("Non-finite numeric literals are not supported")
    if type(value.value) is int and not -(2**63) <= value.value < 2**63:
        raise QueryError("Integer literals must fit the signed 64-bit range")


def _validate_properties(properties: tuple[tuple[str, Literal], ...]) -> None:
    if not isinstance(properties, tuple):
        raise QueryError("Properties require an ordered tuple")
    keys = []
    for item in properties:
        if not isinstance(item, tuple) or len(item) != 2:
            raise QueryError("Properties require (key, literal) pairs")
        key, value = item
        _identifier(key)
        _validate_literal(value)
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise QueryError("Duplicate property-map keys are not supported")


def validate_pattern(pattern: Pattern) -> None:
    if isinstance(pattern, Node):
        _identifier(pattern.variable)
        if not isinstance(pattern.labels, tuple):
            raise QueryError("Labels require an ordered tuple")
        for label in pattern.labels:
            _identifier(label)
        _validate_properties(pattern.properties)
        return
    if not isinstance(pattern, Edge) or pattern.direction not in ("out", "in", "undirected"):
        raise QueryError("Expected a node or a single edge pattern")
    if not isinstance(pattern.left, Node) or not isinstance(pattern.right, Node):
        raise QueryError("Edge endpoints must be nodes")
    validate_pattern(pattern.left)
    validate_pattern(pattern.right)
    rel = pattern.relationship
    if not isinstance(rel, Relationship):
        raise QueryError("An edge requires a relationship")
    if rel.variable is not None:
        _identifier(rel.variable)
    if not isinstance(rel.alternatives, tuple):
        raise QueryError("Relationship alternatives require an ordered tuple")
    types = (rel.type, *rel.alternatives) if rel.type is not None else ()
    if rel.alternatives and rel.type is None or len(types) != len(set(types)):
        raise QueryError("Relationship alternatives require a first type and unique types")
    for name in types:
        _identifier(name)
    _validate_properties(rel.properties)
    if rel.path_mode not in ("trail", "walk", "acyclic"):
        raise QueryError("Path mode must be trail, walk, or acyclic")
    if rel.min_hops is not None or rel.max_hops is not None:
        if type(rel.min_hops) is not int or type(rel.max_hops) is not int or not 0 <= rel.min_hops <= rel.max_hops:
            raise QueryError("Paths require finite nonnegative ordered hop bounds")
        if pattern.direction != "undirected" or rel.variable is not None:
            raise QueryError("Bounded paths must be undirected and hide the relationship list")
    elif rel.path_mode != "trail":
        raise QueryError("A path mode requires explicit hop bounds")
    if pattern.path_variable is not None:
        _identifier(pattern.path_variable)


def _validate_expression_shape(expression: Expression) -> None:
    if isinstance(expression, Property):
        _identifier(expression.variable)
        _identifier(expression.key)
    elif isinstance(expression, Comparison):
        if expression.operator not in ("=", "<>", "<", "<=", ">", ">=", "IS NULL", "IS NOT NULL"):
            raise QueryError("Unsupported comparison operator")
    elif isinstance(expression, Boolean):
        if expression.operator not in ("AND", "OR") or not isinstance(expression.terms, tuple) or len(expression.terms) < 2:
            raise QueryError("A Boolean expression requires AND/OR and at least two operands")
    elif isinstance(expression, Call):
        if expression.name not in ("length", "coalesce", "range", "size", "nodes"):
            raise QueryError(f"Unsupported function: {expression.name}")
        if not isinstance(expression.args, tuple):
            raise QueryError("Function arguments require an ordered tuple")


def _expression_label(expression: Expression) -> str:
    """A logical output identity; complex anonymous outputs use structural JSON.

    These labels are not executable language text. Cypher's historical display
    labels remain available through query.projection_names for old saved cases.
    """
    if isinstance(expression, Property):
        return f"{expression.variable}.{expression.key}"
    if isinstance(expression, Variable):
        return expression.name
    if isinstance(expression, Literal):
        _validate_literal(expression)
        return json.dumps(expression.value, ensure_ascii=False)
    return "expression:" + json.dumps(_encode(expression), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pattern_variables(pattern: Pattern) -> dict[str, str]:
    """Return the local node/relationship roles, rejecting conflicting bindings."""
    validate_pattern(pattern)
    nodes = (pattern,) if isinstance(pattern, Node) else (pattern.left, pattern.right)
    variables = {node.variable: "node" for node in nodes}
    if isinstance(pattern, Edge) and pattern.relationship.variable is not None:
        name = pattern.relationship.variable
        if name in variables:
            raise QueryError("A variable cannot denote both a node and a relationship")
        variables[name] = "relationship"
    if isinstance(pattern, Edge) and pattern.path_variable is not None:
        if pattern.path_variable in variables:
            raise QueryError("A path variable cannot shadow an element variable")
        variables[pattern.path_variable] = "path"
    return variables


def expression_variables(expression: Expression) -> frozenset[str]:
    """Referenced variables, with ALL's binder removed.

    Subquery pattern variables are conservative references: intersect with the
    surrounding scope when asking which of them are correlated outer bindings.
    """
    if isinstance(expression, Property):
        return frozenset((expression.variable,))
    if isinstance(expression, Variable):
        return frozenset((expression.name,))
    if isinstance(expression, Literal):
        return frozenset()
    if isinstance(expression, (Comparison, Concat)):
        parts = (expression.left, expression.right)
    elif isinstance(expression, Boolean):
        parts = expression.terms
    elif isinstance(expression, Negation):
        parts = (expression.expression,)
    elif isinstance(expression, Call):
        parts = expression.args
    elif isinstance(expression, Case):
        parts = (expression.condition, expression.then, expression.otherwise)
    elif isinstance(expression, ListExpr):
        parts = expression.items
    elif isinstance(expression, All):
        return expression_variables(expression.items) | (expression_variables(expression.predicate) - {expression.variable})
    elif isinstance(expression, (Exists, Count)):
        refs = expression_variables(expression.where) if expression.where is not None else frozenset()
        return refs | frozenset(pattern_variables(expression.pattern))
    else:
        raise QueryError("Unsupported expression")
    return frozenset().union(*(expression_variables(part) for part in parts))


def _join_types(types) -> str:
    types = tuple(types)
    concrete = {kind.rstrip("?") for kind in types if kind != "null"}
    if not concrete:
        return "null"
    if concrete <= {"integer", "float", "number"}:
        result = next(iter(concrete)) if len(concrete) == 1 else "number"
    else:
        result = next(iter(concrete)) if len(concrete) == 1 else "unknown"
    return result + "?" if "null" in types or any(kind.endswith("?") for kind in types) else result


def _is_boolean(kind: str) -> bool:
    return kind.rstrip("?") in ("boolean", "null")


def _bind_pattern(pattern: Pattern, scope: dict[str, str], optional: bool = False) -> dict[str, str]:
    local = pattern_variables(pattern)
    if any(scope[name].rstrip("?") != kind for name, kind in local.items() if name in scope):
        raise QueryError("A MATCH changes the type of an existing binding")
    return scope | {name: kind + ("?" if optional else "") for name, kind in local.items() if name not in scope}


def _node_local_predicate(expression: Expression, variable: str) -> bool:
    """Only the ALL binder's own scalar properties may be observed.

    No entity value, other variable, nested graph query or positional/path
    observation can escape through this restricted universal predicate.
    """
    if isinstance(expression, Literal):
        return True
    if isinstance(expression, Property):
        return expression.variable == variable
    if isinstance(expression, Comparison):
        parts = (expression.left, expression.right)
    elif isinstance(expression, Boolean):
        parts = expression.terms
    elif isinstance(expression, Negation):
        parts = (expression.expression,)
    elif isinstance(expression, Case):
        parts = (expression.condition, expression.then, expression.otherwise)
    elif isinstance(expression, Call) and expression.name == "coalesce":
        parts = expression.args
    else:
        return False
    return all(_node_local_predicate(part, variable) for part in parts)


def _validate_expression(expression: Expression, scope: dict[str, str]) -> str:
    _validate_expression_shape(expression)
    if isinstance(expression, Literal):
        _validate_literal(expression)
        return {type(None): "null", bool: "boolean", int: "integer", float: "float", str: "string"}[type(expression.value)]
    if isinstance(expression, Variable):
        _identifier(expression.name)
        if expression.name not in scope:
            raise QueryError(f"Unbound variable: {expression.name}")
        if scope[expression.name].rstrip("?") == "path":
            raise QueryError("A path may only be observed through length(path) or node-local ALL(nodes(path))")
        if scope[expression.name].rstrip("?") in ("node", "relationship"):
            raise QueryError("Use graph properties rather than graph entities in scalar expressions")
        return scope[expression.name]
    if isinstance(expression, Property):
        if expression.variable not in scope:
            raise QueryError(f"Unbound variable: {expression.variable}")
        if scope[expression.variable].rstrip("?") not in ("node", "relationship"):
            raise QueryError("Property access requires a node or relationship")
        return "unknown"
    if isinstance(expression, Comparison):
        left = _validate_expression(expression.left, scope).rstrip("?")
        right = _validate_expression(expression.right, scope).rstrip("?")
        if left in ("node", "relationship") or right in ("node", "relationship"):
            raise QueryError("Comparisons of graph entities are outside the scalar fragment")
        if expression.operator.startswith("IS ") and expression.right != Literal(None):
            raise QueryError("NULL tests require a null right operand")
        if expression.operator in ("<", "<=", ">", ">=") and "null" not in (left, right):
            numeric = {"integer", "float", "number"}
            if not ({left, right} <= numeric or left == right == "string"):
                raise QueryError("Ordering requires known comparable numeric or string operands")
        return "boolean"
    if isinstance(expression, (Boolean, Negation)):
        terms = expression.terms if isinstance(expression, Boolean) else (expression.expression,)
        if any(not _is_boolean(_validate_expression(term, scope)) for term in terms):
            raise QueryError("Boolean operands must have a known BOOLEAN-or-NULL type")
        return "boolean"
    if isinstance(expression, (Exists, Count)):
        local = pattern_variables(expression.pattern)
        if isinstance(expression, Exists) and not set(local).intersection(scope):
            raise QueryError("EXISTS must reference an outer variable")
        inner = _bind_pattern(expression.pattern, scope)
        if expression.where is not None and not _is_boolean(_validate_expression(expression.where, inner)):
            raise QueryError("Subquery WHERE must be boolean")
        return "boolean" if isinstance(expression, Exists) else "integer"
    if isinstance(expression, Call):
        if expression.name == "nodes":
            raise QueryError("nodes(path) is only allowed as ALL's direct input")
        if expression.name == "length":
            if len(expression.args) != 1:
                raise QueryError("length requires one path argument")
            argument = expression.args[0]
            if argument == Literal(None):
                return "null"
            if not isinstance(argument, Variable) or scope.get(argument.name, "").rstrip("?") != "path":
                raise QueryError("length requires a bound path variable")
            return "integer?" if scope[argument.name].endswith("?") else "integer"
        kinds = tuple(_validate_expression(arg, scope) for arg in expression.args)
        if expression.name == "coalesce":
            if not kinds:
                raise QueryError("coalesce requires at least one argument")
            return _join_types(kinds)
        if expression.name == "range":
            if len(kinds) not in (2, 3) or any(kind != "integer" for kind in kinds):
                raise QueryError("range requires two or three known non-null integers")
            if len(kinds) == 3 and (not isinstance(expression.args[2], Literal) or expression.args[2].value == 0):
                raise QueryError("range step must be a nonzero literal")
            return "list:integer"
        if len(kinds) != 1 or not (kinds[0].startswith("list:") or kinds[0] in ("string", "null")):
            raise QueryError("size requires a known list, string, or null")
        return "null" if kinds[0] == "null" else "integer"
    if isinstance(expression, Case):
        if not _is_boolean(_validate_expression(expression.condition, scope)):
            raise QueryError("CASE condition must have a known BOOLEAN-or-NULL type")
        return _join_types((_validate_expression(expression.then, scope), _validate_expression(expression.otherwise, scope)))
    if isinstance(expression, ListExpr):
        kinds = tuple(_validate_expression(item, scope) for item in expression.items)
        if any(kind.rstrip("?") in ("node", "relationship") for kind in kinds):
            raise QueryError("Lists of graph entities are outside the scalar fragment")
        return "list:" + (_join_types(kinds) if kinds else "empty")
    if isinstance(expression, Concat):
        left, right = (_validate_expression(item, scope) for item in (expression.left, expression.right))
        if any(kind != "null" and not kind.startswith("list:") for kind in (left, right)):
            raise QueryError("The + operator only concatenates lists in this fragment")
        if "null" in (left, right):
            return "null"
        elements = tuple(kind[5:] for kind in (left, right) if kind != "list:empty")
        return "list:" + (_join_types(elements) if elements else "empty")
    if isinstance(expression, All):
        _identifier(expression.variable)
        if expression.variable in scope:
            raise QueryError("ALL cannot shadow an existing variable")
        if isinstance(expression.items, Call) and expression.items.name == "nodes":
            args = expression.items.args
            if (len(args) != 1 or not isinstance(args[0], Variable)
                    or scope.get(args[0].name, "").rstrip("?") != "path"):
                raise QueryError("ALL nodes(path) requires one bound path variable")
            if not _node_local_predicate(expression.predicate, expression.variable):
                raise QueryError("ALL nodes(path) requires a node-local scalar property predicate")
            _validate_expression_shape(expression.items)
            kind = "list:node"
        else:
            kind = _validate_expression(expression.items, scope)
        if kind != "null" and not kind.startswith("list:"):
            raise QueryError("ALL requires a known list or null")
        element = kind[5:] if kind.startswith("list:") else "null"
        # An empty list has no values to evaluate. Treat its unused binder as
        # null-like for scalar type checks, rather than inventing an element type.
        inner = scope | {expression.variable: "null" if element == "empty" else element}
        if not _is_boolean(_validate_expression(expression.predicate, inner)):
            raise QueryError("ALL predicate must be boolean")
        return "boolean"
    raise QueryError("Unsupported expression")


def projection_names(query: Query | UnionAll | ConditionalQuery) -> tuple[str, ...]:
    if isinstance(query, UnionAll):
        return projection_names(query.branches[0])
    if isinstance(query, ConditionalQuery):
        return projection_names(query.then)
    return tuple(item.name if isinstance(item, Alias) else _expression_label(item) for item in query.returns)


def validate_query(query: Query | UnionAll | ConditionalQuery) -> None:
    if isinstance(query, ConditionalQuery):
        if not isinstance(query.then, Query) or not isinstance(query.otherwise, Query):
            raise QueryError("Conditional branches must be ordinary, non-nested queries")
        if not _is_boolean(_validate_expression(query.condition, {})):
            raise QueryError("Conditional query condition must be boolean without outer bindings")
        validate_query(query.then)
        validate_query(query.otherwise)
        if projection_names(query.then) != projection_names(query.otherwise):
            raise QueryError("Conditional branches must project identical column names in identical order")
        return
    if isinstance(query, UnionAll):
        if not isinstance(query.branches, tuple) or len(query.branches) < 2 or any(not isinstance(branch, Query) for branch in query.branches):
            raise QueryError("UNION ALL requires at least two ordinary query branches")
        for branch in query.branches:
            validate_query(branch)
        if any(projection_names(branch) != projection_names(query.branches[0]) for branch in query.branches[1:]):
            raise QueryError("UNION ALL branches must project identical column names in identical order")
        return
    if not isinstance(query, Query) or query.pattern is None and (query.where is not None or query.matches):
        raise QueryError("A RETURN-only query cannot have MATCH clauses or WHERE")
    if not isinstance(query.returns, tuple) or not isinstance(query.matches, tuple):
        raise QueryError("Query projections and clauses require ordered tuples")
    if type(query.distinct) is not bool:
        raise QueryError("distinct must be a boolean")
    if query.limit is not None:
        if type(query.limit) is not int or query.limit < 0:
            raise QueryError("LIMIT requires a nonnegative integer literal")
        _validate_literal(Literal(query.limit))
    scope = {}
    for clause in clauses(query):
        if not isinstance(clause, Match) or type(clause.optional) is not bool:
            raise QueryError("Invalid MATCH clause")
        scope = _bind_pattern(clause.pattern, scope, clause.optional)
        if clause.where is not None and not _is_boolean(_validate_expression(clause.where, scope)):
            raise QueryError("WHERE must have a known BOOLEAN-or-NULL type")
    if not query.returns:
        raise QueryError("RETURN requires at least one expression")
    names = projection_names(query)
    if len(set(names)) != len(names):
        raise QueryError("Duplicate projection names would lose columns during result collection")
    for item in query.returns:
        if isinstance(item, Alias):
            _identifier(item.name)
            item = item.expression
        if _validate_expression(item, scope).rstrip("?") in ("node", "relationship"):
            raise QueryError("RETURN graph properties instead of graph entities")


QueryTree: TypeAlias = Query | UnionAll | ConditionalQuery

_NODES = {cls.__name__: cls for cls in (
    Literal, Property, Variable, Comparison, Boolean, Negation, Node, Relationship,
    Edge, Exists, Count, Call, Case, ListExpr, Concat, All, Alias, Match, Query,
    UnionAll, ConditionalQuery,
)}


def _encode(value):
    if is_dataclass(value) and type(value).__name__ in _NODES:
        return {"kind": type(value).__name__, **{field.name: _encode(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise QueryError("Unsupported value in query IR")


def to_data(query: QueryTree) -> dict:
    """A versioned JSON-ready archive that preserves tuple order and value types."""
    validate_query(query)
    return {"version": 1, "query": _encode(query)}


def _decode(value):
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    if isinstance(value, dict):
        cls = _NODES.get(value.get("kind"))
        if cls is None or set(value) != {"kind", *(field.name for field in fields(cls))}:
            raise QueryError("Unknown IR node or unexpected fields")
        try:
            return cls(**{key: _decode(item) for key, item in value.items() if key != "kind"})
        except TypeError as exc:
            raise QueryError("Invalid IR node") from exc
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise QueryError("Unsupported value in saved IR")


def from_data(data: dict) -> QueryTree:
    if not isinstance(data, dict) or set(data) != {"version", "query"} or type(data["version"]) is not int or data["version"] != 1:
        raise QueryError("Unsupported query IR archive version")
    try:
        query = _decode(data["query"])
        validate_query(query)
    except (AttributeError, TypeError, KeyError) as exc:
        raise QueryError("Invalid saved query IR") from exc
    return query


def _static_kind(expression: Expression) -> str:
    """Known scalar/list kinds only; graph-property types need a graph schema."""
    if isinstance(expression, Literal):
        return {type(None): "null", bool: "boolean", int: "integer", float: "float", str: "string"}[type(expression.value)]
    if isinstance(expression, (Comparison, Boolean, Negation, Exists, All)):
        return "boolean"
    if isinstance(expression, Count):
        return "integer"
    if isinstance(expression, Case):
        return _join_types((_static_kind(expression.then), _static_kind(expression.otherwise)))
    if isinstance(expression, Call):
        if expression.name in ("length", "size"):
            return "null" if expression.args == (Literal(None),) else "integer"
        if expression.name == "range":
            return "list:integer"
        if expression.name == "coalesce":
            return _join_types(_static_kind(arg) for arg in expression.args)
    if isinstance(expression, ListExpr):
        return "list:" + (_join_types(_static_kind(item) for item in expression.items) if expression.items else "empty")
    return "unknown"


def requirements(query: QueryTree) -> frozenset[str]:
    """Identify syntax/semantic needs, without claiming an engine supports them."""
    validate_query(query)
    found = set()

    def visit(value):
        if isinstance(value, Node) and len(value.labels) > 1:
            found.add("multi_label_conjunction")
        elif isinstance(value, Relationship):
            if value.alternatives:
                found.add("relationship_alternatives")
            if value.min_hops is not None:
                found.update(("bounded_path", "path_mode_" + value.path_mode))
        elif isinstance(value, Match) and value.optional:
            found.add("optional_match")
        elif isinstance(value, Query):
            if value.distinct:
                found.add("distinct")
            if value.limit is not None:
                found.add("limit")
            if value.pattern is None:
                found.add("return_only")
            bound = set()
            for clause in clauses(value):
                nodes = (clause.pattern,) if isinstance(clause.pattern, Node) else (clause.pattern.left, clause.pattern.right)
                if any(node.variable in bound and node.labels for node in nodes):
                    found.add("label_rebinding")
                bound.update(node.variable for node in nodes)
        for cls, feature in ((Exists, "exists_subquery"), (Count, "count_subquery"),
                             (ConditionalQuery, "conditional_query"), (UnionAll, "union_all"),
                             (All, "all"), (Concat, "list_concat"), (ListExpr, "list"),
                             (Case, "case")):
            if isinstance(value, cls):
                found.add(feature)
        if isinstance(value, Call):
            found.add("function_" + value.name)
            if value.name == "coalesce":
                kinds = {_static_kind(arg).rstrip("?") for arg in value.args} - {"null", "unknown", "list:empty"}
                normalized = {"number" if kind in {"integer", "float", "number"} else kind for kind in kinds}
                if len(normalized) > 1:
                    found.add("mixed_type_coalesce")
                if (any(isinstance(arg, (Comparison, Boolean, Negation)) for arg in value.args)
                        and any(isinstance(arg, Literal) and (arg.value is None or type(arg.value) is bool)
                                for arg in value.args)):
                    found.add("boolean_expression_coalesce")
        if isinstance(value, All) and isinstance(value.items, Call) and value.items.name == "nodes":
            found.add("node_local_path_filter")
        if isinstance(value, Literal) and value.value is None:
            found.add("null")
        if is_dataclass(value):
            for field in fields(value):
                visit(getattr(value, field.name))
        elif isinstance(value, tuple):
            for item in value:
                visit(item)

    visit(query)
    return frozenset(found)


