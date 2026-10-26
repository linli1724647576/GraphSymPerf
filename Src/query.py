"""A deliberately small, structured Cypher fragment for reproducible tests.

Only the grammar below is accepted. Unsupported Cypher is rejected, rather than
silently rewritten. Cost placeholders and database contents belong to generator.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace

from lark import Lark, Transformer, UnexpectedInput
from lark.exceptions import VisitError


from ir import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count,
    Edge, Exists, Expression, ListExpr, Literal, Match, Negation, Node, Pattern, Property,
    Query, QueryError, Relationship, RowLimit, RowStream, UnionAll, Variable, With, clauses,
    expression_variables, pattern_variables, validate_query,
    row_stream_imports, with_clauses,
)

_GRAMMAR = r"""
query: cypher_version? row_stream ";"?
cypher_version: "CYPHER"i "25"
?row_stream: conditional_query | union_query
union_query: row_query ("UNION"i "ALL"i row_query)*
?row_query: single_query | row_call
row_call: "CALL"i "(" import_names? ")" "{" row_stream "}" return_clause limit_clause?
import_names: NAME ("," NAME)*
conditional_query: "WHEN"i expr "THEN"i "{" row_query "}" "ELSE"i "{" row_query "}"
single_query: pipeline_clause* return_clause limit_clause?
?pipeline_clause: match_clause | with_clause
with_clause: "WITH"i distinct? projection where_clause?
return_clause: "RETURN"i distinct? projection
distinct: "DISTINCT"i
limit_clause: "LIMIT"i INT
match_clause: "MATCH"i pattern where_clause? -> mandatory_match
            | "OPTIONAL"i "MATCH"i pattern where_clause? -> optional_match
where_clause: "WHERE"i expr
projection: projection_item ("," projection_item)*
?projection_item: expr "AS"i NAME -> alias
                | expr
?pattern: NAME "=" edge -> named_path
        | edge
        | node
?edge: node "-" relationship? "->" node -> outgoing
        | node "<-" relationship? "-" node -> incoming
        | node "-" relationship? "-" node -> undirected
node: "(" NAME label* property_map? ")"
label: ":" NAME
relationship: "[" rel_variable? rel_type? hops? property_map? "]"
rel_variable: NAME
rel_type: ":" NAME ("|" ":"? NAME)*
hops: "*" INT ".." INT
property_map: "{" [map_entry ("," map_entry)*] "}"
map_entry: NAME ":" literal
?expr: or_expr
?or_expr: and_expr ("OR"i and_expr)* -> or_expression
?and_expr: not_expr ("AND"i not_expr)* -> and_expression
?not_expr: "NOT"i not_expr -> negation
         | predicate
?predicate: concatenation COMP_OP concatenation -> comparison
          | concatenation "IS"i "NULL"i -> is_null
          | concatenation "IS"i "NOT"i "NULL"i -> is_not_null
          | concatenation
?concatenation: atom ("+" atom)* -> concatenation
?atom: exists | count | case | all_expr | function | list_expr | property | literal
     | NAME -> variable
     | "(" expr ")"
exists: "EXISTS"i "{" subquery "}"
count: "COUNT"i "{" subquery "}"
?subquery: row_stream
         | "MATCH"i pattern where_clause? -> short_subquery
case: "CASE"i "WHEN"i expr "THEN"i expr "ELSE"i expr "END"i
all_expr: "all"i "(" NAME "IN"i expr "WHERE"i expr ")"
function: NAME "(" [expr ("," expr)*] ")"
list_expr: "[" [expr ("," expr)*] "]"
property: NAME "." NAME
?literal: SIGNED_NUMBER -> number
        | STRING -> string
        | "true"i -> true
        | "false"i -> false
        | "null"i -> null
COMP_OP: "<>" | "<=" | ">=" | "=" | "<" | ">"
NAME: /[A-Za-z_][A-Za-z0-9_]*/
STRING: /'(?:[^'\\]|\\.)*'/ | /"(?:[^"\\]|\\.)*"/
%import common.SIGNED_NUMBER
%import common.INT
%import common.WS
%ignore WS
%ignore /\/\/[^\n]*/
%ignore /(?s:\/\*.*?\*\/)/
"""


@dataclass(frozen=True)
class _RowCall:
    body: object
    imports: tuple[str, ...]
    returns: tuple[Expression | Alias, ...]
    distinct: bool
    limit: int | None


class _Build(Transformer):
    def number(self, items):
        text = str(items[0])
        return Literal(float(text) if any(c in text.lower() for c in ".e") else int(text))

    def string(self, items):
        return Literal(ast.literal_eval(str(items[0])))

    def true(self, _):
        return Literal(True)

    def false(self, _):
        return Literal(False)

    def null(self, _):
        return Literal(None)

    def property(self, items):
        return Property(*map(str, items))

    def variable(self, items):
        return Variable(str(items[0]))

    def comparison(self, items):
        return Comparison(items[0], str(items[1]), items[2])

    def is_null(self, items):
        return Comparison(items[0], "IS NULL", Literal(None))

    def is_not_null(self, items):
        return Comparison(items[0], "IS NOT NULL", Literal(None))

    def and_expression(self, items):
        return items[0] if len(items) == 1 else Boolean("AND", tuple(items))

    def or_expression(self, items):
        return items[0] if len(items) == 1 else Boolean("OR", tuple(items))

    def negation(self, items):
        return Negation(items[0])

    def label(self, items):
        return str(items[0])

    def map_entry(self, items):
        return str(items[0]), items[1]

    def property_map(self, items):
        return tuple(items)

    def node(self, items):
        properties = items[-1] if isinstance(items[-1], tuple) else ()
        labels = items[1:-1] if properties or isinstance(items[-1], tuple) else items[1:]
        return Node(str(items[0]), tuple(labels), properties)

    def rel_variable(self, items):
        return "variable", str(items[0])

    def rel_type(self, items):
        return "types", tuple(map(str, items))

    def hops(self, items):
        return "hops", tuple(map(int, items))

    def relationship(self, items):
        kwargs = {}
        for item in items:
            if len(item) == 2 and item[0] == "variable":
                kwargs[item[0]] = item[1]
            elif len(item) == 2 and item[0] == "types":
                kwargs.update(type=item[1][0], alternatives=item[1][1:])
            elif len(item) == 2 and item[0] == "hops":
                kwargs.update(min_hops=item[1][0], max_hops=item[1][1])
            else:
                kwargs["properties"] = item
        return Relationship(**kwargs)

    def outgoing(self, items):
        return self._edge(items, "out")

    def incoming(self, items):
        return self._edge(items, "in")

    def undirected(self, items):
        return self._edge(items, "undirected")

    @staticmethod
    def _edge(items, direction):
        relationship = items[1] if len(items) == 3 else Relationship()
        return Edge(items[0], relationship, items[-1], direction)

    def where_clause(self, items):
        return items[0]

    def named_path(self, items):
        return replace(items[1], path_variable=str(items[0]))

    def exists(self, items):
        return Exists(body=items[0]) if isinstance(items[0], (Query, UnionAll, RowLimit, ConditionalQuery, _RowCall)) else Exists(*items[0])

    def count(self, items):
        return Count(body=items[0]) if isinstance(items[0], (Query, UnionAll, RowLimit, ConditionalQuery, _RowCall)) else Count(*items[0])

    def short_subquery(self, items):
        return items[0], items[1] if len(items) == 2 else None

    def projection(self, items):
        return tuple(items)

    def alias(self, items):
        return Alias(items[0], str(items[1]))

    def function(self, items):
        return Call(str(items[0]).lower(), tuple(items[1:]))

    def case(self, items):
        return Case(*items)

    def list_expr(self, items):
        return ListExpr(tuple(items))

    def concatenation(self, items):
        value = items[0]
        for item in items[1:]:
            value = Concat(value, item)
        return value

    def all_expr(self, items):
        return All(str(items[0]), items[1], items[2])

    def mandatory_match(self, items):
        return Match(items[0], items[1] if len(items) == 2 else None)

    def optional_match(self, items):
        return Match(items[0], items[1] if len(items) == 2 else None, True)

    def distinct(self, _):
        return True

    def with_clause(self, items):
        distinct = items[0] is True
        projections = items[1] if distinct else items[0]
        where_items = items[2:] if distinct else items[1:]
        return With(projections, where_items[0] if where_items else None, distinct)

    def return_clause(self, items):
        return items[-1], len(items) == 2

    def limit_clause(self, items):
        return int(items[0])

    def single_query(self, items):
        limit = items.pop() if type(items[-1]) is int else None
        returns, distinct = items[-1]
        return with_clauses(Query(None, None, returns, limit=limit, distinct=distinct), tuple(items[:-1]))

    def union_query(self, items):
        return items[0] if len(items) == 1 else UnionAll(tuple(items))

    def import_names(self, items):
        return tuple(map(str, items))

    def row_call(self, items):
        imports = items.pop(0) if isinstance(items[0], tuple) and all(isinstance(item, str) for item in items[0]) else ()
        limit = items.pop() if type(items[-1]) is int else None
        returns, distinct = items[-1]
        return _RowCall(items[0], imports, returns, distinct, limit)

    def conditional_query(self, items):
        return ConditionalQuery(*items)

    def cypher_version(self, _):
        return "cypher25"

    def query(self, items):
        return items[-1]


_PARSER = Lark(_GRAMMAR, parser="lalr", start=["query", "pattern", "expr"], maybe_placeholders=False)


def _parse(text: str, start: str):
    try:
        return _Build().transform(_PARSER.parse(text, start=start))
    except (UnexpectedInput, VisitError, SyntaxError, ValueError) as exc:
        raise QueryError(f"Unsupported Cypher {start}: {exc}") from exc


def _lower_calls(value, scope: frozenset[str] = frozenset(), *, _group_allowed: bool = False):
    """Accept only transparent generated CALL boundaries, never general CALL."""
    if isinstance(value, _RowCall):
        if len(value.imports) != len(set(value.imports)) or not set(value.imports) <= scope:
            raise QueryError("CALL imports must be distinct bindings in the enclosing scope")
        body = _lower_calls(value.body, frozenset(value.imports))
        if row_stream_imports(body, scope) != frozenset(value.imports):
            raise QueryError("CALL imports do not match the row stream's lexical bindings")
        names = projection_names(body)
        if value.distinct or value.returns != tuple(Variable(name) for name in names):
            raise QueryError("Generated CALL must transparently return its row-stream columns")
        _check_call_boundary(body, scope, frozenset(value.imports))
        if value.limit is None:
            if not isinstance(body, (UnionAll, ConditionalQuery)) or not _group_allowed:
                raise QueryError("An unlimited generated CALL only groups a nested composed row stream")
            return body
        return RowLimit(body, value.limit)
    if isinstance(value, UnionAll):
        return replace(value, branches=tuple(_lower_calls(branch, scope, _group_allowed=True) for branch in value.branches))
    if isinstance(value, ConditionalQuery):
        return replace(value, condition=_lower_calls(value.condition, scope),
                       then=_lower_calls(value.then, scope, _group_allowed=True),
                       otherwise=_lower_calls(value.otherwise, scope, _group_allowed=True))
    if isinstance(value, Query):
        env = set(scope)
        stages = []
        for clause in clauses(value):
            if isinstance(clause, Match):
                env.update(pattern_variables(clause.pattern))
                stages.append(replace(clause, where=_lower_calls(clause.where, frozenset(env))))
            else:
                projections = tuple(_lower_calls(item, frozenset(env)) for item in clause.projections)
                if any(not isinstance(item, (Alias, Variable)) for item in projections):
                    raise QueryError("WITH expressions require an alias; existing variables may pass through")
                env = {item.name for item in projections}
                stages.append(replace(clause, projections=projections,
                                      where=_lower_calls(clause.where, frozenset(env))))
        result = with_clauses(value, tuple(stages))
        return replace(result, returns=tuple(_lower_calls(item, frozenset(env)) for item in value.returns))
    if isinstance(value, (Count, Exists)):
        if value.body is not None:
            return replace(value, body=_lower_calls(value.body, scope))
        inner = scope | frozenset(pattern_variables(value.pattern))
        return replace(value, where=_lower_calls(value.where, inner))
    if isinstance(value, All):
        return replace(value, items=_lower_calls(value.items, scope),
                       predicate=_lower_calls(value.predicate, scope | {value.variable}))
    if isinstance(value, tuple):
        return tuple(_lower_calls(item, scope) for item in value)
    if is_dataclass(value):
        return replace(value, **{field.name: _lower_calls(getattr(value, field.name), scope) for field in fields(value)})
    return value


def parse_query(text: str) -> RowStream | ConditionalQuery:
    query = _lower_calls(_parse(text, "query"))
    validate_query(query)
    return query


def _identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise QueryError(f"Unsupported identifier: {value!r}")
    return value


def _literal(value: Literal) -> str:
    if not isinstance(value, Literal) or type(value.value) not in (type(None), bool, int, float, str):
        raise QueryError("Only scalar literals are supported")
    if isinstance(value.value, float) and not math.isfinite(value.value):
        raise QueryError("Non-finite numeric literals are not supported")
    if type(value.value) is int and not -(2**63) <= value.value < 2**63:
        raise QueryError("Integer literals must fit Cypher's signed 64-bit range")
    return json.dumps(value.value, ensure_ascii=False)


def _properties(properties: tuple[tuple[str, Literal], ...]) -> str:
    keys = [_identifier(key) for key, _ in properties]
    if len(keys) != len(set(keys)):
        raise QueryError("Duplicate property-map keys are not supported")
    return " {" + ", ".join(f"{key}: {_literal(value)}" for key, value in properties) + "}" if properties else ""


def render_pattern(pattern: Pattern, dialect: str = "cypher") -> str:
    if isinstance(pattern, Node):
        labels = "".join(":" + _identifier(label) for label in pattern.labels)
        return f"({_identifier(pattern.variable)}{labels}{_properties(pattern.properties)})"
    if not isinstance(pattern, Edge) or pattern.direction not in ("out", "in", "undirected"):
        raise QueryError("Expected a node or a single edge pattern")
    rel = pattern.relationship
    name = _identifier(rel.variable) if rel.variable is not None else ""
    types = (rel.type, *rel.alternatives) if rel.type is not None else ()
    if rel.alternatives and rel.type is None or len(types) != len(set(types)):
        raise QueryError("Relationship alternatives require a first type and unique types")
    label = ":" + "|".join(map(_identifier, types)) if types else ""
    if dialect != "ladybug" and rel.path_mode != "trail":
        raise QueryError("This Cypher fragment supports trail paths only")
    hops = ""
    if rel.min_hops is not None or rel.max_hops is not None:
        if type(rel.min_hops) is not int or type(rel.max_hops) is not int or not 0 <= rel.min_hops <= rel.max_hops:
            raise QueryError("Paths require finite nonnegative ordered hop bounds")
        if pattern.direction != "undirected" or rel.variable is not None:
            raise QueryError("Bounded paths must be undirected and hide the relationship list")
        mode = " " + rel.path_mode.upper() + " " if dialect == "ladybug" else ""
        hops = f"*{mode}{rel.min_hops}..{rel.max_hops}"
    middle = f"[{name}{label}{hops}{_properties(rel.properties)}]"
    arrows = {"out": ("-", "->"), "in": ("<-", "-"), "undirected": ("-", "-")}
    left, right = arrows[pattern.direction]
    prefix = _identifier(pattern.path_variable) + " = " if pattern.path_variable is not None else ""
    return f"{prefix}{render_pattern(pattern.left, dialect)}{left}{middle}{right}{render_pattern(pattern.right, dialect)}"


def render_expression(expression: Expression, dialect: str = "cypher", *, _scope: frozenset[str] = frozenset()) -> str:
    if isinstance(expression, Literal):
        return _literal(expression)
    if isinstance(expression, Variable):
        return _identifier(expression.name)
    if isinstance(expression, Property):
        return f"{_identifier(expression.variable)}.{_identifier(expression.key)}"
    if isinstance(expression, Comparison):
        left = render_expression(expression.left, dialect, _scope=_scope)
        if expression.operator in ("IS NULL", "IS NOT NULL"):
            return f"({left} {expression.operator})"
        if expression.operator not in ("=", "<>", "<", "<=", ">", ">="):
            raise QueryError("Unsupported comparison operator")
        return f"({left} {expression.operator} {render_expression(expression.right, dialect, _scope=_scope)})"
    if isinstance(expression, Boolean):
        if expression.operator not in ("AND", "OR") or len(expression.terms) < 2:
            raise QueryError("A Boolean expression requires AND/OR and at least two operands")
        return "(" + f" {expression.operator} ".join((render_expression(item, dialect, _scope=_scope) for item in expression.terms)) + ")"
    if isinstance(expression, Negation):
        return f"(NOT {render_expression(expression.expression, dialect, _scope=_scope)})"
    if isinstance(expression, (Exists, Count)):
        operator = "EXISTS" if isinstance(expression, Exists) else "COUNT"
        if expression.body is not None:
            return f"{operator} {{ {_render_query(expression.body, dialect, _scope)} }}"
        inner = _scope | frozenset(pattern_variables(expression.pattern))
        where = " WHERE " + render_expression(expression.where, dialect, _scope=inner) if expression.where is not None else ""
        return f"{operator} {{ MATCH {render_pattern(expression.pattern, dialect)}{where} }}"
    if isinstance(expression, Call):
        if expression.name not in ("length", "coalesce", "range", "size", "nodes"):
            raise QueryError(f"Unsupported function: {expression.name}")
        return expression.name + "(" + ", ".join((render_expression(item, dialect, _scope=_scope) for item in expression.args)) + ")"
    if isinstance(expression, Case):
        return f"CASE WHEN {render_expression(expression.condition, dialect, _scope=_scope)} THEN {render_expression(expression.then, dialect, _scope=_scope)} ELSE {render_expression(expression.otherwise, dialect, _scope=_scope)} END"
    if isinstance(expression, ListExpr):
        return "[" + ", ".join((render_expression(item, dialect, _scope=_scope) for item in expression.items)) + "]"
    if isinstance(expression, Concat):
        return f"({render_expression(expression.left, dialect, _scope=_scope)} + {render_expression(expression.right, dialect, _scope=_scope)})"
    if isinstance(expression, All):
        return f"all({_identifier(expression.variable)} IN {render_expression(expression.items, dialect, _scope=_scope)} WHERE {render_expression(expression.predicate, dialect, _scope=_scope | {expression.variable})})"
    raise QueryError("Unsupported expression")


def projection_names(query: RowStream | ConditionalQuery) -> tuple[str, ...]:
    """Historical Cypher result labels, for compatibility with existing archives."""
    if isinstance(query, RowLimit):
        return projection_names(query.body)
    if isinstance(query, UnionAll):
        return projection_names(query.branches[0])
    if isinstance(query, ConditionalQuery):
        return projection_names(query.then)
    scope = set()
    for clause in clauses(query):
        if isinstance(clause, Match):
            scope.update(pattern_variables(clause.pattern))
        else:
            scope = {item.name for item in clause.projections}
    return tuple(item.name if isinstance(item, Alias) else render_expression(item, _scope=frozenset(scope)) for item in query.returns)


def render_query(query: RowStream | ConditionalQuery, dialect: str = "cypher") -> str:
    validate_query(query)
    text = _render_query(query, dialect)

    def conditional(value):
        if isinstance(value, ConditionalQuery):
            return True
        if is_dataclass(value):
            return any(conditional(getattr(value, field.name)) for field in fields(value))
        return isinstance(value, tuple) and any(conditional(item) for item in value)

    return "CYPHER 25 " + text if conditional(query) else text


def _render_projection(items: tuple[Expression | Alias, ...], dialect: str, scope: frozenset[str]) -> str:
    return ", ".join(render_expression(item.expression, dialect, _scope=scope) + " AS " + item.name
                     if isinstance(item, Alias) else render_expression(item, dialect, _scope=scope) for item in items)


def _check_call_boundary(body: RowStream, outer: frozenset[str], imports: frozenset[str]) -> None:
    names = projection_names(body)
    for name in names:
        _identifier(name)  # Computed anonymous columns need an explicit alias.
    if set(names) & outer:
        raise QueryError("A generated CALL output cannot collide with an outer binding")

    def check(value, scope):
        if isinstance(value, Query):
            env = set(scope)
            for clause in clauses(value):
                if isinstance(clause, Match):
                    local = set(pattern_variables(clause.pattern))
                    if (local & imports) - env:
                        raise QueryError("A generated CALL cannot rebind an imported variable after WITH")
                    env.update(local)
                else:
                    if any(isinstance(item, Alias) and item.name in imports for item in clause.projections):
                        raise QueryError("A generated CALL cannot redefine an imported variable")
                    for item in clause.projections:
                        check(item, frozenset(env))
                    env = {item.name for item in clause.projections}
                check(clause.where, frozenset(env))
            for item in value.returns:
                check(item, frozenset(env))
        elif isinstance(value, (Count, Exists)):
            if value.body is not None:
                check(value.body, scope)
            else:
                local = frozenset(pattern_variables(value.pattern))
                if (local & imports) - scope:
                    raise QueryError("A generated CALL cannot rebind an imported variable after WITH")
                check(value.where, scope | local)
        elif isinstance(value, Alias) and value.name in imports:
            raise QueryError("A generated CALL cannot redefine an imported variable")
        elif isinstance(value, All):
            check(value.items, scope)
            check(value.predicate, scope | {value.variable})
        elif is_dataclass(value):
            for field in fields(value):
                check(getattr(value, field.name), scope)
        elif isinstance(value, tuple):
            for child in value:
                check(child, scope)

    check(body, imports)


def _render_row_call(body: RowStream, dialect: str, scope: frozenset[str], limit: int | None = None) -> str:
    if dialect != "cypher":
        raise QueryError("Global row-stream scopes are only compiled for Neo4j Cypher")
    imports = row_stream_imports(body, scope)
    _check_call_boundary(body, scope, imports)
    names = projection_names(body)
    text = f"CALL ({', '.join(sorted(imports))}) {{ {_render_query(body, dialect, imports)} }} RETURN {', '.join(names)}"
    return text + (f" LIMIT {limit}" if limit is not None else "")


def _render_query(query: RowStream | ConditionalQuery, dialect: str, scope: frozenset[str] = frozenset()) -> str:
    """Render a tree whose enclosing query has already validated lexical scope."""
    if isinstance(query, ConditionalQuery):
        def branch(value):
            return (_render_query(value, dialect, scope) if isinstance(value, (Query, RowLimit))
                    else _render_row_call(value, dialect, scope))
        return f"WHEN {render_expression(query.condition, dialect, _scope=scope)} THEN {{ {branch(query.then)} }} ELSE {{ {branch(query.otherwise)} }}"
    if isinstance(query, RowLimit):
        return _render_row_call(query.body, dialect, scope, query.limit)
    if isinstance(query, UnionAll):
        return " UNION ALL ".join(_render_row_call(branch, dialect, scope) if isinstance(branch, (UnionAll, ConditionalQuery))
                                   else _render_query(branch, dialect, scope) for branch in query.branches)
    parts = []
    env = set(scope)
    for clause in clauses(query):
        if isinstance(clause, With):
            parts.append(("WITH DISTINCT " if clause.distinct else "WITH ") + _render_projection(clause.projections, dialect, frozenset(env)))
            env = {item.name for item in clause.projections}
        else:
            env.update(pattern_variables(clause.pattern))
            parts.append(("OPTIONAL MATCH " if clause.optional else "MATCH ") + render_pattern(clause.pattern, dialect))
        if clause.where is not None:
            parts.append("WHERE " + render_expression(clause.where, dialect, _scope=frozenset(env)))
    parts.append(("RETURN DISTINCT " if query.distinct else "RETURN ") + _render_projection(query.returns, dialect, frozenset(env)))
    if query.limit is not None:
        parts.append(f"LIMIT {query.limit}")
    return " ".join(parts)


def extract_seed_pattern(text: str) -> Pattern:
    """Derive the first node/single-edge prefix of the first ordinary MATCH.

    This does NOT preserve the source query's semantics. It is solely a seed
    structure for re-instantiation; callers must retain the original source.
    A longer path is truncated after its first edge. Unsupported atoms fail.
    The Lark lexer protects quoted strings/comments; no query regex splitting.
    """
    try:
        tokens = iter(_PARSER.lex(text))
        depth = 0
        previous = ""
        for token in tokens:
            value = str(token)
            if depth == 0 and token.type == "MATCH" and previous.upper() != "OPTIONAL":
                break
            depth += value in ("(", "[", "{")
            depth -= value in (")", "]", "}")
            previous = value
        else:
            raise QueryError("No ordinary MATCH found in the source")
        first = next(tokens)
        start = first.start_pos
        if first.type == "NAME":
            if str(next(tokens)) != "=":
                raise QueryError("Expected a named path assignment")
            first = next(tokens)
        if str(first) != "(":
            raise QueryError("The first MATCH must start with a named node")

        def finish_node(opening):
            balance = 1
            for current in tokens:
                if current.type == "LPAR":
                    balance += 1
                elif current.type == "RPAR":
                    balance -= 1
                if balance == 0:
                    return current.end_pos
            raise QueryError("Incomplete node pattern")

        end = finish_node(first)
        following = next(tokens, None)
        if following is None or str(following) not in ("-", "<-", "<"):
            result = _parse(text[start:end], "pattern")
        else:
            brackets = 0
            for current in tokens:
                if current.type == "LSQB":
                    brackets += 1
                elif current.type == "RSQB":
                    brackets -= 1
                elif current.type == "LPAR" and brackets == 0:
                    end = finish_node(current)
                    break
            else:
                raise QueryError("Incomplete edge pattern")
            result = _parse(text[start:end], "pattern")
        pattern_variables(result)
        return result
    except (UnexpectedInput, StopIteration) as exc:
        raise QueryError("Cannot derive a supported first MATCH pattern") from exc


def extract_seed_boolean(text: str, operator: str) -> tuple[int, int] | None:
    """Find an AND/OR subtree in a parsable WHERE, before re-binding its operands."""
    start = None
    depth = 0

    def contains(expression):
        if isinstance(expression, Boolean):
            return expression.operator == operator or any(contains(term) for term in expression.terms)
        return isinstance(expression, Negation) and contains(expression.expression)

    def matches(end):
        if start is None:
            return False
        try:
            return contains(_parse(text[start:end], "expr"))
        except QueryError:
            return False

    try:
        for token in _PARSER.lex(text):
            value = str(token).upper()
            if depth == 0 and token.type in ("MATCH", "RETURN", "NAME", "UNION", "OPTIONAL") and value in ("MATCH", "RETURN", "WITH", "UNWIND", "UNION", "OPTIONAL"):
                if matches(token.start_pos):
                    return start, token.start_pos
                start = None
            if depth == 0 and token.type == "WHERE":
                start = token.end_pos
            if token.type in ("LPAR", "LSQB", "LBRACE"):
                depth += 1
            elif token.type in ("RPAR", "RSQB", "RBRACE"):
                depth -= 1
        if matches(len(text)):
            return start, len(text)
    except UnexpectedInput:
        pass
    return None


def split_statements(text: str) -> tuple[str, ...]:
    """Split a Cypher script, leaving quoted semicolons and comments intact."""
    statements, part = [], []
    quote = None
    delimiters = []
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            part.append(char)
            if char == "\\" and quote != "`":
                i += 1
                if i < len(text):
                    part.append(text[i])
            elif char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    part.append(text[i + 1])
                    i += 1
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
            part.append(char)
        elif text.startswith("//", i) or (text.startswith("--", i) and not "".join(part).strip()):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            part.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise QueryError("Unterminated comment")
            i = end + 1
            part.append(" ")
        elif char in "([{":
            delimiters.append(char)
            part.append(char)
        elif char in ")]}":
            if not delimiters or delimiters.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                raise QueryError("Unbalanced query delimiters")
            part.append(char)
        elif char == ";" and not delimiters:
            if "".join(part).strip():
                statements.append("".join(part).strip())
            part = []
        else:
            part.append(char)
        i += 1
    if quote:
        raise QueryError("Unterminated quoted value")
    if delimiters:
        raise QueryError("Unbalanced query delimiters")
    if "".join(part).strip():
        statements.append("".join(part).strip())
    return tuple(statements)
