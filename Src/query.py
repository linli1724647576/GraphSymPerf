"""A deliberately small, structured Cypher fragment for reproducible tests.

Only the grammar below is accepted. Unsupported Cypher is rejected, rather than
silently rewritten. Cost placeholders and database contents belong to generator.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import replace

from lark import Lark, Transformer, UnexpectedInput
from lark.exceptions import VisitError


from ir import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count,
    Edge, Exists, Expression, ListExpr, Literal, Match, Negation, Node, Pattern, Property,
    Query, QueryError, Relationship, UnionAll, Variable, clauses,
    expression_variables, pattern_variables, validate_query,
    with_clauses,
)

_GRAMMAR = r"""
query: conditional_query ";"? | union_query ";"?
union_query: single_query ("UNION"i "ALL"i single_query)*
conditional_query: "CYPHER"i "25" "WHEN"i expr "THEN"i "{" single_query "}" "ELSE"i "{" single_query "}"
single_query: match_clause* return_clause limit_clause?
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
exists: "EXISTS"i "{" "MATCH"i pattern where_clause? "}"
count: "COUNT"i "{" "MATCH"i pattern where_clause? "}"
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
        return Exists(items[0], items[1] if len(items) == 2 else None)

    def count(self, items):
        return Count(items[0], items[1] if len(items) == 2 else None)

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

    def conditional_query(self, items):
        return ConditionalQuery(*items)

    def query(self, items):
        return items[0]


_PARSER = Lark(_GRAMMAR, parser="lalr", start=["query", "pattern", "expr"], maybe_placeholders=False)


def _parse(text: str, start: str):
    try:
        return _Build().transform(_PARSER.parse(text, start=start))
    except (UnexpectedInput, VisitError, SyntaxError, ValueError) as exc:
        raise QueryError(f"Unsupported Cypher {start}: {exc}") from exc


def parse_query(text: str) -> Query | UnionAll | ConditionalQuery:
    query = _parse(text, "query")
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


def render_expression(expression: Expression, dialect: str = "cypher") -> str:
    if isinstance(expression, Literal):
        return _literal(expression)
    if isinstance(expression, Variable):
        return _identifier(expression.name)
    if isinstance(expression, Property):
        return f"{_identifier(expression.variable)}.{_identifier(expression.key)}"
    if isinstance(expression, Comparison):
        left = render_expression(expression.left, dialect)
        if expression.operator in ("IS NULL", "IS NOT NULL"):
            return f"({left} {expression.operator})"
        if expression.operator not in ("=", "<>", "<", "<=", ">", ">="):
            raise QueryError("Unsupported comparison operator")
        return f"({left} {expression.operator} {render_expression(expression.right, dialect)})"
    if isinstance(expression, Boolean):
        if expression.operator not in ("AND", "OR") or len(expression.terms) < 2:
            raise QueryError("A Boolean expression requires AND/OR and at least two operands")
        return "(" + f" {expression.operator} ".join((render_expression(item, dialect) for item in expression.terms)) + ")"
    if isinstance(expression, Negation):
        return f"(NOT {render_expression(expression.expression, dialect)})"
    if isinstance(expression, (Exists, Count)):
        where = " WHERE " + render_expression(expression.where, dialect) if expression.where is not None else ""
        operator = "EXISTS" if isinstance(expression, Exists) else "COUNT"
        return f"{operator} {{ MATCH {render_pattern(expression.pattern, dialect)}{where} }}"
    if isinstance(expression, Call):
        if expression.name not in ("length", "coalesce", "range", "size", "nodes"):
            raise QueryError(f"Unsupported function: {expression.name}")
        return expression.name + "(" + ", ".join((render_expression(item, dialect) for item in expression.args)) + ")"
    if isinstance(expression, Case):
        return f"CASE WHEN {render_expression(expression.condition, dialect)} THEN {render_expression(expression.then, dialect)} ELSE {render_expression(expression.otherwise, dialect)} END"
    if isinstance(expression, ListExpr):
        return "[" + ", ".join((render_expression(item, dialect) for item in expression.items)) + "]"
    if isinstance(expression, Concat):
        return f"({render_expression(expression.left, dialect)} + {render_expression(expression.right, dialect)})"
    if isinstance(expression, All):
        return f"all({_identifier(expression.variable)} IN {render_expression(expression.items, dialect)} WHERE {render_expression(expression.predicate, dialect)})"
    raise QueryError("Unsupported expression")


def projection_names(query: Query | UnionAll | ConditionalQuery) -> tuple[str, ...]:
    """Historical Cypher result labels, for compatibility with existing archives."""
    if isinstance(query, UnionAll):
        return projection_names(query.branches[0])
    if isinstance(query, ConditionalQuery):
        return projection_names(query.then)
    return tuple(item.name if isinstance(item, Alias) else render_expression(item) for item in query.returns)


def render_query(query: Query | UnionAll | ConditionalQuery, dialect: str = "cypher") -> str:
    validate_query(query)
    if isinstance(query, ConditionalQuery):
        return f"CYPHER 25 WHEN {render_expression(query.condition, dialect)} THEN {{ {render_query(query.then, dialect)} }} ELSE {{ {render_query(query.otherwise, dialect)} }}"
    if isinstance(query, UnionAll):
        return " UNION ALL ".join((render_query(branch, dialect) for branch in query.branches))
    parts = []
    for clause in clauses(query):
        parts.append(("OPTIONAL MATCH " if clause.optional else "MATCH ") + render_pattern(clause.pattern, dialect))
        if clause.where is not None:
            parts.append("WHERE " + render_expression(clause.where, dialect))
    returns = tuple(render_expression(item.expression, dialect) + " AS " + item.name if isinstance(item, Alias) else render_expression(item, dialect) for item in query.returns)
    parts.append(("RETURN DISTINCT " if query.distinct else "RETURN ") + ", ".join(returns))
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
