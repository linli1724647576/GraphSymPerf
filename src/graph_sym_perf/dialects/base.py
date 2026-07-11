"""Typed, fail-closed compilation boundary for Cypher dialects."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from graph_sym_perf.generation.corpus import GraphCorpus, PortableQuery


class DecisionKind(str, Enum):
    ACCEPT = "accept"
    REWRITE = "rewrite"
    REJECT = "reject"


class ReasonCode(str, Enum):
    NATIVE_CYPHER_SUPPORTED = "NATIVE_CYPHER_SUPPORTED"
    AGENSGRAPH_SUPPORTED = "AGENSGRAPH_SUPPORTED"
    APACHE_AGE_SUPPORTED = "APACHE_AGE_SUPPORTED"
    MULTI_LABEL_UNSUPPORTED = "MULTI_LABEL_UNSUPPORTED"
    VARIABLE_LABEL_CONFLICT = "VARIABLE_LABEL_CONFLICT"
    DISTINCT_ORDER_BY_SCOPE = "DISTINCT_ORDER_BY_SCOPE"
    RETURN_STAR_SCOPE_UNKNOWN = "RETURN_STAR_SCOPE_UNKNOWN"
    AGE_SUBQUERY_UNSUPPORTED = "AGE_SUBQUERY_UNSUPPORTED"
    PROJECTION_SCHEMA_UNSUPPORTED = "PROJECTION_SCHEMA_UNSUPPORTED"
    PARAMETER_RENDER_UNSUPPORTED = "PARAMETER_RENDER_UNSUPPORTED"
    PROOF_REQUIRED = "PROOF_REQUIRED"


@dataclass(frozen=True)
class DialectCapabilities:
    name: str
    multiple_node_labels: bool
    native_parameters: bool
    requires_explicit_result_schema: bool
    requires_graph_path: bool
    generation_backend: str = "gdsmith"


@dataclass(frozen=True)
class ResultColumn:
    name: str
    postgres_type: str = "agtype"


@dataclass(frozen=True)
class ExecutionEnvelope:
    statement: str
    protocol: str
    bind_values: tuple[object, ...] = ()
    dollar_quote_tag: str | None = None


@dataclass(frozen=True)
class CompiledQuery:
    cypher_core: str
    execution_envelope: ExecutionEnvelope
    result_schema: tuple[ResultColumn, ...]
    parameters: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    provenance: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True)
class CompatibilityDecision:
    kind: DecisionKind
    reason_code: ReasonCode
    details: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    rewritten_ast: object | None = None
    compiled_query: CompiledQuery | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if self.kind is DecisionKind.REWRITE and self.rewritten_ast is None:
            raise ValueError("REWRITE requires a proof-backed rewritten AST")
        if self.kind is not DecisionKind.REWRITE and self.rewritten_ast is not None:
            raise ValueError("only REWRITE may carry a rewritten AST")
        if self.kind is DecisionKind.REJECT and self.compiled_query is not None:
            raise ValueError("REJECT cannot carry executable output")

    @property
    def rewritten_query(self) -> str | None:
        if self.kind is not DecisionKind.REWRITE or self.compiled_query is None:
            return None
        return self.compiled_query.cypher_core


class DialectAdapter(Protocol):
    capabilities: DialectCapabilities

    def check(self, query: PortableQuery) -> CompatibilityDecision:
        ...

    def load_graph(self, corpus: GraphCorpus) -> object:
        ...


@dataclass(frozen=True)
class Projection:
    columns: tuple[ResultColumn, ...]
    is_star: bool = False


def accept(reason: ReasonCode, compiled: CompiledQuery) -> CompatibilityDecision:
    return CompatibilityDecision(DecisionKind.ACCEPT, reason, compiled_query=compiled)


def reject(reason: ReasonCode, **details: object) -> CompatibilityDecision:
    return CompatibilityDecision(DecisionKind.REJECT, reason, details=details)


def render_typed_parameters(query: str, parameters: Mapping[str, Any]) -> str:
    """Render only real Cypher parameter tokens, never text inside literals/comments."""

    normalized: dict[str, Any] = {}
    for raw_name, value in parameters.items():
        name = str(raw_name)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None:
            raise ValueError(f"unsupported Cypher parameter name: {name!r}")
        normalized[name] = value

    output: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(query):
        char = query[index]
        following = query[index + 1] if index + 1 < len(query) else ""
        if line_comment:
            output.append(char)
            index += 1
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            output.append(char)
            if char == "*" and following == "/":
                output.append(following)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote is not None:
            output.append(char)
            if char == "\\" and following:
                output.append(following)
                index += 2
                continue
            if char == quote:
                if following == quote and quote in {"'", '"'}:
                    output.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "/" and following == "/":
            output.extend((char, following))
            index += 2
            line_comment = True
            continue
        if char == "/" and following == "*":
            output.extend((char, following))
            index += 2
            block_comment = True
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "$" and following and (following.isalpha() or following == "_"):
            end = index + 2
            while end < len(query) and (query[end].isalnum() or query[end] == "_"):
                end += 1
            name = query[index + 1 : end]
            if name not in normalized:
                raise ValueError(f"missing Cypher parameter binding: {name}")
            output.append(render_cypher_literal(normalized[name]))
            index = end
            continue
        output.append(char)
        index += 1
    if quote is not None or block_comment:
        raise ValueError("unterminated Cypher literal or comment")
    return "".join(output)


def render_cypher_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are outside the portable Cypher literal grammar")
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(render_cypher_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        items: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) is None:
                raise ValueError(f"unsupported Cypher map key: {key!r}")
            items.append(f"{key}: {render_cypher_literal(item)}")
        return "{" + ", ".join(items) + "}"
    raise TypeError(f"unsupported Cypher parameter type: {type(value).__name__}")


def dynamic_dollar_quote(cypher: str) -> str:
    digest = sha256(cypher.encode("utf-8")).hexdigest()
    for width in range(12, len(digest) + 1, 4):
        tag = f"$gsym_{digest[:width]}$"
        if tag not in cypher:
            return tag
    raise ValueError("cannot construct a collision-free PostgreSQL dollar quote tag")


def parse_projection(query: str, *, allow_star_scope: bool) -> Projection:
    return_start = _last_top_level_keyword(query, "RETURN")
    if return_start is None:
        return Projection((ResultColumn("result"),))
    body_start = return_start + len("RETURN")
    body_end = len(query)
    for keyword in ("ORDER BY", "SKIP", "LIMIT", "UNION"):
        position = _first_top_level_keyword(query, keyword, start=body_start)
        if position is not None:
            body_end = min(body_end, position)
    body = query[body_start:body_end].strip()
    if not body:
        raise ValueError("RETURN has no projection")
    if body.upper().startswith("DISTINCT "):
        body = body[len("DISTINCT ") :].lstrip()
    parts = _split_top_level_commas(body)
    columns: list[ResultColumn] = []
    used: set[str] = set()
    saw_star = False
    for index, item in enumerate(parts, start=1):
        if _code_only(item).strip() == "*":
            if not allow_star_scope:
                raise LookupError("RETURN * scope is not provable")
            scope = _provable_return_star_scope(query[:return_start])
            if not scope:
                raise LookupError("RETURN * scope is empty or unknown")
            for name in sorted(scope):
                if name in used:
                    raise ValueError(f"duplicate result column: {name}")
                used.add(name)
                columns.append(ResultColumn(name))
            saw_star = True
            continue
        explicit = re.search(r"\bAS\s+([A-Za-z][A-Za-z0-9_]*)\s*$", item, re.IGNORECASE)
        if explicit is not None:
            name = explicit.group(1)
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", item.strip()):
            name = item.strip()
        else:
            name = f"col{index}"
        if name in used:
            raise ValueError(f"duplicate result column: {name}")
        used.add(name)
        columns.append(ResultColumn(name))
    return Projection(tuple(columns), is_star=saw_star)


def has_multi_label_node(query: str) -> bool:
    identifier = r"(?:[A-Za-z]\w*|`(?:``|[^`])+`)"
    return bool(
        re.search(
            rf"\([^)]*:\s*{identifier}\s*:\s*{identifier}",
            _code_only(query, preserve_backticks=True),
        )
    )


def has_variable_label_conflict(query: str) -> bool:
    identifier = r"(?:[A-Za-z]\w*|`(?:``|[^`])+`)"

    def canonical(value: str) -> str:
        value = value.strip()
        return value[1:-1].replace("``", "`") if value.startswith("`") else value

    labels_by_variable: dict[str, set[str | None]] = {}
    code = _code_only(query, preserve_backticks=True)
    for match in re.finditer(rf"\(\s*({identifier})\s*((?::\s*{identifier})*)", code):
        variable = canonical(match.group(1))
        labels = {canonical(value) for value in re.findall(rf":\s*({identifier})", match.group(2))} or {None}
        labels_by_variable.setdefault(variable, set()).update(labels)
        if len(labels_by_variable[variable]) > 1:
            return True
    return False


def has_distinct_order_by_projection_mismatch(query: str) -> bool:
    return_start = _last_top_level_keyword(query, "RETURN")
    order_start = _last_top_level_keyword(query, "ORDER BY")
    if return_start is None or order_start is None or order_start < return_start:
        return False
    return_body = query[return_start + len("RETURN") : order_start].strip()
    if not return_body.upper().startswith("DISTINCT "):
        return False
    projected: set[str] = set()
    for item in _split_top_level_commas(return_body[len("DISTINCT ") :]):
        alias = re.search(r"\bAS\s+([A-Za-z]\w*)\s*$", item, re.IGNORECASE)
        projected.add(_canonical_expression(alias.group(1) if alias else item))
    end = len(query)
    for keyword in ("SKIP", "LIMIT", "UNION"):
        position = _first_top_level_keyword(query, keyword, start=order_start + len("ORDER BY"))
        if position is not None:
            end = min(end, position)
    ordered = {
        _canonical_expression(re.sub(r"\s+(ASC|DESC)\s*$", "", item, flags=re.IGNORECASE))
        for item in _split_top_level_commas(query[order_start + len("ORDER BY") : end])
    }
    return bool(ordered - projected)


def has_top_level_keyword(query: str, keyword: str) -> bool:
    return _first_top_level_keyword(query, keyword, start=0) is not None


def _provable_return_star_scope(prefix: str) -> set[str]:
    from graph_sym_perf.cypher.ast import Clause, NodePattern
    from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query

    if has_top_level_keyword(prefix, "CALL"):
        raise LookupError("subquery scope is unsupported")
    scope: set[str] = set()
    try:
        clauses = parse_query(prefix).clauses
        for clause in clauses:
            if clause.keyword in {"MATCH", "OPTIONAL MATCH"}:
                text = clause.text
                if clause.keyword == "OPTIONAL MATCH":
                    text = re.sub(r"^\s*OPTIONAL\s+MATCH", "MATCH", text, count=1, flags=re.I)
                parsed = parse_match_clause(Clause("MATCH", clause.start, clause.end, text))
                pattern = parsed.pattern
                if isinstance(pattern, NodePattern):
                    scope.add(pattern.variable)
                else:
                    scope.update(pattern.variables)
                continue
            if clause.keyword == "UNWIND":
                alias = re.search(r"\bAS\s+([A-Za-z][A-Za-z0-9_]*)\s*$", clause.text, re.I | re.S)
                if alias is None:
                    raise LookupError("UNWIND scope is outside the provable fragment")
                scope.add(alias.group(1))
                continue
            if clause.keyword == "WITH":
                with_body = re.sub(r"^\s*WITH\b", "", clause.text, count=1, flags=re.I).strip()
                narrowed: set[str] = set()
                for item in _split_top_level_commas(with_body):
                    alias = re.search(r"\bAS\s+([A-Za-z][A-Za-z0-9_]*)\s*$", item, re.I)
                    if alias:
                        narrowed.add(alias.group(1))
                    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", item.strip()):
                        narrowed.add(item.strip())
                    else:
                        raise LookupError("WITH scope is outside the provable fragment")
                scope = narrowed
                continue
            raise LookupError(f"{clause.keyword} scope is outside the provable fragment")
    except CypherParseError as exc:
        raise LookupError(f"RETURN * scope parse failed: {exc.code}") from exc
    return scope


def _split_top_level_commas(text: str) -> tuple[str, ...]:
    positions = _scan_top_level(text, comma=True)
    parts: list[str] = []
    start = 0
    for position in positions:
        part = text[start:position].strip()
        if not part:
            raise ValueError("empty projection item")
        parts.append(part)
        start = position + 1
    last = text[start:].strip()
    if not last:
        raise ValueError("empty projection item")
    parts.append(last)
    return tuple(parts)


def _last_top_level_keyword(text: str, keyword: str) -> int | None:
    positions = _keyword_positions(text, keyword)
    return positions[-1] if positions else None


def _first_top_level_keyword(text: str, keyword: str, *, start: int) -> int | None:
    return next((position for position in _keyword_positions(text, keyword) if position >= start), None)


def _keyword_positions(text: str, keyword: str) -> tuple[int, ...]:
    code = _code_only(text, preserve_depth=True)
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9_])", re.I)
    depths = _depths(code)
    return tuple(match.start() for match in pattern.finditer(code) if depths[match.start()] == 0)


def _scan_top_level(text: str, *, comma: bool) -> tuple[int, ...]:
    code = _code_only(text, preserve_depth=True)
    depths = _depths(code)
    return tuple(index for index, char in enumerate(code) if comma and char == "," and depths[index] == 0)


def _depths(text: str) -> tuple[int, ...]:
    depth = 0
    values: list[int] = []
    for char in text:
        values.append(depth)
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
    return tuple(values)


def _code_only(text: str, *, preserve_depth: bool = False, preserve_backticks: bool = False) -> str:
    chars = list(text)
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(chars):
        char = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            else:
                chars[index] = " "
            index += 1
            continue
        if block_comment:
            chars[index] = " "
            if char == "*" and following == "/":
                chars[index + 1] = " "
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote is not None:
            chars[index] = " "
            if char == "\\" and following:
                chars[index + 1] = " "
                index += 2
                continue
            if char == quote:
                if following == quote and quote in {"'", '"'}:
                    chars[index + 1] = " "
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "/" and following == "/":
            chars[index] = chars[index + 1] = " "
            index += 2
            line_comment = True
            continue
        if char == "/" and following == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            block_comment = True
            continue
        if char == "`" and preserve_backticks:
            index += 1
            while index < len(chars):
                if chars[index] == "`":
                    if index + 1 < len(chars) and chars[index + 1] == "`":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char in {"'", '"', "`"}:
            chars[index] = " "
            quote = char
        index += 1
    return "".join(chars)


def _canonical_expression(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


__all__ = [
    "CompatibilityDecision",
    "CompiledQuery",
    "DecisionKind",
    "DialectAdapter",
    "DialectCapabilities",
    "ExecutionEnvelope",
    "PortableQuery",
    "ReasonCode",
    "ResultColumn",
    "accept",
    "dynamic_dollar_quote",
    "has_distinct_order_by_projection_mismatch",
    "has_multi_label_node",
    "has_top_level_keyword",
    "has_variable_label_conflict",
    "parse_projection",
    "reject",
    "render_cypher_literal",
    "render_typed_parameters",
]
