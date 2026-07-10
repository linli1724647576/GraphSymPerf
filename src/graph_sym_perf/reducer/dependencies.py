"""Conservative Cypher scope and observable-output dependency analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass

from graph_sym_perf.cypher.ast import PatternList
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query, split_top_level_commas


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_KEYWORDS = frozenset(
    {
        "ALL", "AND", "AS", "ASC", "BY", "CASE", "COUNT", "DESC", "DISTINCT",
        "ELSE", "END", "FALSE", "IN", "IS", "LIMIT", "MATCH", "NOT", "NULL",
        "OPTIONAL", "OR", "ORDER", "RETURN", "SKIP", "THEN", "TRUE", "UNWIND",
        "WHEN", "WHERE", "WITH", "XOR",
    }
)
_BARRIER_RE = re.compile(r"\b(ORDER\s+BY|LIMIT|SKIP)\b", re.I)
_AGGREGATE_RE = re.compile(r"\b(COUNT|COLLECT|SUM|AVG|MIN|MAX)\s*\(", re.I)


@dataclass(frozen=True)
class ClauseDependency:
    index: int
    keyword: str
    text: str
    definitions: frozenset[str]
    uses: frozenset[str]
    incoming_scope: frozenset[str]
    outgoing_scope: frozenset[str]
    barriers: tuple[str, ...] = ()
    projection_schema: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryDependencyGraph:
    query: str
    clauses: tuple[ClauseDependency, ...]
    output_schema: tuple[str, ...]
    valid: bool
    reason_codes: tuple[str, ...]
    aggregate_sensitive: bool
    ast_node_count: int


def analyze_query(query: str) -> QueryDependencyGraph:
    """Build a fail-closed scope graph for the supported read-only Cypher fragment."""

    reasons: list[str] = []
    dependencies: list[ClauseDependency] = []
    scope: frozenset[str] = frozenset()
    output_schema: tuple[str, ...] = ()
    aggregate_sensitive = False
    try:
        parsed = parse_query(query)
    except CypherParseError as exc:
        return QueryDependencyGraph(query, (), (), False, (f"parse:{exc.code}",), False, 0)
    if not parsed.clauses or query[: parsed.clauses[0].start].strip():
        return QueryDependencyGraph(query, (), (), False, ("parse:missing_or_prefixed_clause",), False, 0)

    for index, clause in enumerate(parsed.clauses):
        incoming = scope
        definitions: frozenset[str] = frozenset()
        uses: frozenset[str] = frozenset()
        barriers = _barriers(clause.text)
        projection_schema: tuple[str, ...] = ()
        try:
            if clause.keyword in {"MATCH", "OPTIONAL MATCH"}:
                if clause.keyword != "MATCH":
                    raise CypherParseError("OPTIONAL_MATCH_FAIL_CLOSED", "optional match reduction is not proven")
                match = parse_match_clause(clause)
                variables = _pattern_variables(match.pattern)
                definitions = variables - incoming
                pattern_uses = variables & incoming
                property_uses = _pattern_property_uses(match.pattern)
                where_uses = _expression_variables(match.where_region.text) if match.where_region else frozenset()
                uses = pattern_uses | property_uses | where_uses
                scope = incoming | variables
                missing = (property_uses | where_uses) - scope
                if missing:
                    reasons.append(f"clause_{index}:unbound:{','.join(sorted(missing))}")
            elif clause.keyword == "WITH":
                projection, suffixes = _projection_parts(clause.text, "WITH")
                items = _projection_items(projection)
                aggregate_sensitive = aggregate_sensitive or bool(_AGGREGATE_RE.search(projection))
                projection_schema = tuple(item[1] for item in items)
                uses = frozenset().union(*(_expression_variables(item[0]) for item in items))
                missing = uses - incoming
                if missing:
                    reasons.append(f"clause_{index}:unbound:{','.join(sorted(missing))}")
                scope = frozenset(name for _, name in items if _is_identifier(name))
                suffix_uses = frozenset().union(*(_expression_variables(text) for _, text in suffixes))
                missing_suffix = suffix_uses - scope
                if missing_suffix:
                    reasons.append(f"clause_{index}:unbound_after_with:{','.join(sorted(missing_suffix))}")
                uses |= suffix_uses
                definitions = scope - incoming
            elif clause.keyword == "RETURN":
                projection, suffixes = _projection_parts(clause.text, "RETURN")
                items = _projection_items(projection)
                projection_schema = tuple(item[1] for item in items)
                output_schema = projection_schema
                aggregate_sensitive = aggregate_sensitive or bool(_AGGREGATE_RE.search(projection))
                projection_uses = frozenset().union(*(_expression_variables(item[0]) for item in items))
                alias_scope = frozenset(name for _, name in items if _is_identifier(name))
                suffix_uses = frozenset().union(*(_expression_variables(text) for _, text in suffixes))
                missing = projection_uses - incoming
                missing_suffix = suffix_uses - (incoming | alias_scope)
                if missing or missing_suffix:
                    unbound = missing | missing_suffix
                    reasons.append(f"clause_{index}:unbound:{','.join(sorted(unbound))}")
                uses = projection_uses | (suffix_uses & incoming)
                scope = incoming
            elif clause.keyword == "UNWIND":
                body = clause.text[len("UNWIND") :].strip()
                match = re.fullmatch(r"(?P<expression>.+?)\s+AS\s+(?P<alias>[A-Za-z_]\w*)", body, re.I | re.S)
                if match is None:
                    raise CypherParseError("UNPROVEN_UNWIND", "UNWIND must have a static alias")
                uses = _expression_variables(match.group("expression"))
                missing = uses - incoming
                if missing:
                    reasons.append(f"clause_{index}:unbound:{','.join(sorted(missing))}")
                definitions = frozenset({match.group("alias")})
                scope = incoming | definitions
            else:
                reasons.append(f"clause_{index}:unsupported:{clause.keyword.lower().replace(' ', '_')}")
        except CypherParseError as exc:
            reasons.append(f"clause_{index}:parse:{exc.code}")

        dependencies.append(
            ClauseDependency(
                index=index,
                keyword=clause.keyword,
                text=clause.text.strip(),
                definitions=definitions,
                uses=uses,
                incoming_scope=incoming,
                outgoing_scope=scope,
                barriers=barriers,
                projection_schema=projection_schema,
            )
        )

    if not dependencies or dependencies[-1].keyword != "RETURN":
        reasons.append("missing_final_return")
    ast_nodes = sum(1 + len(item.definitions) + len(item.uses) + len(item.projection_schema) for item in dependencies)
    return QueryDependencyGraph(
        query=query,
        clauses=tuple(dependencies),
        output_schema=output_schema,
        valid=not reasons,
        reason_codes=tuple(reasons),
        aggregate_sensitive=aggregate_sensitive,
        ast_node_count=ast_nodes,
    )


def dependency_safe_removal(clauses: tuple[str, ...], remove_index: int) -> bool:
    """Return true only when removing the clause leaves a valid, schema-identical query."""

    if remove_index < 0 or remove_index >= len(clauses):
        return False
    original = analyze_query(" ".join(clauses))
    candidate = analyze_query(" ".join(clauses[:remove_index] + clauses[remove_index + 1 :]))
    remaining = original.clauses[:remove_index] + original.clauses[remove_index + 1 :]
    binding_roles_preserved = len(remaining) == len(candidate.clauses) and all(
        before.definitions == after.definitions and before.uses <= after.uses
        for before, after in zip(remaining, candidate.clauses, strict=True)
    )
    return bool(
        original.valid
        and candidate.valid
        and binding_roles_preserved
        and original.output_schema == candidate.output_schema
        and not original.aggregate_sensitive
    )


def dependency_preserved(original: str, candidate: str) -> bool:
    original_graph = analyze_query(original)
    candidate_graph = analyze_query(candidate)
    return bool(
        original_graph.valid
        and candidate_graph.valid
        and original_graph.output_schema == candidate_graph.output_schema
        and not original_graph.aggregate_sensitive
    )


def _pattern_variables(pattern: object) -> frozenset[str]:
    if isinstance(pattern, PatternList):
        return pattern.variables
    variables = getattr(pattern, "variables", None)
    if variables is not None:
        return frozenset(variables)
    variable = getattr(pattern, "variable", None)
    return frozenset({variable}) if variable else frozenset()


def _pattern_property_uses(pattern: object) -> frozenset[str]:
    if isinstance(pattern, PatternList):
        return frozenset().union(*(_pattern_property_uses(atom) for atom in pattern.atoms))
    expressions: list[str] = [value for _key, value in getattr(pattern, "properties", ())]
    for component_name in ("left", "relationship", "right"):
        component = getattr(pattern, component_name, None)
        expressions.extend(value for _key, value in getattr(component, "properties", ()))
    return frozenset().union(*(_expression_variables(value) for value in expressions))


def _barriers(text: str) -> tuple[str, ...]:
    return tuple(" ".join(match.group(1).upper().split()) for match in _BARRIER_RE.finditer(_mask_strings(text)))


def _projection_parts(text: str, keyword: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    body = text[len(keyword) :].strip()
    starts = _top_level_suffixes(body)
    if not starts:
        return body, ()
    projection = body[: starts[0][0]].strip()
    suffixes: list[tuple[str, str]] = []
    for index, (start, name) in enumerate(starts):
        value_start = start + len(name)
        value_end = starts[index + 1][0] if index + 1 < len(starts) else len(body)
        suffixes.append((name, body[value_start:value_end].strip()))
    return projection, tuple(suffixes)


def _top_level_suffixes(text: str) -> tuple[tuple[int, str], ...]:
    names = ("WHERE", "ORDER BY", "SKIP", "LIMIT")
    masked = _mask_nested(text)
    found: list[tuple[int, str]] = []
    for name in names:
        for match in re.finditer(rf"\b{re.escape(name).replace(r'\ ', r'\s+')}\b", masked, re.I):
            found.append((match.start(), name))
    return tuple(sorted(found))


def _projection_items(text: str) -> tuple[tuple[str, str], ...]:
    if not text.strip() or text.strip() == "*":
        raise CypherParseError("UNPROVEN_PROJECTION", "projection must be explicit")
    items: list[tuple[str, str]] = []
    for raw in split_top_level_commas(text):
        match = re.fullmatch(r"(?P<expression>.+?)\s+AS\s+(?P<alias>[A-Za-z_]\w*)", raw.strip(), re.I | re.S)
        if match:
            items.append((match.group("expression").strip(), match.group("alias")))
        else:
            expression = raw.strip()
            items.append((expression, expression))
    return tuple(items)


def _expression_variables(expression: str) -> frozenset[str]:
    masked = _mask_strings(expression)
    result: set[str] = set()
    for match in _IDENTIFIER.finditer(masked):
        name = match.group(0)
        upper = name.upper()
        previous = masked[match.start() - 1] if match.start() else ""
        rest = masked[match.end() :]
        next_nonspace = rest.lstrip()[:1]
        if upper in _KEYWORDS or previous in {".", ":", "$"} or next_nonspace == "(":
            continue
        result.add(name)
    return frozenset(result)


def _mask_strings(text: str) -> str:
    chars = list(text)
    quote: str | None = None
    index = 0
    while index < len(chars):
        char = chars[index]
        if quote:
            chars[index] = " "
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            chars[index] = " "
        index += 1
    return "".join(chars)


def _mask_nested(text: str) -> str:
    masked = list(_mask_strings(text))
    depth = 0
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
            masked[index] = " "
        elif char in ")]}":
            masked[index] = " "
            depth = max(0, depth - 1)
        elif depth:
            masked[index] = " "
    return "".join(masked)


def _is_identifier(value: str) -> bool:
    return _IDENTIFIER.fullmatch(value) is not None
