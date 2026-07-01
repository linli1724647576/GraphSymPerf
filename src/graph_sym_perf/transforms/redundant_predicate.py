"""Redundant boolean predicate injection."""

from __future__ import annotations

import re
from typing import Iterable

from graph_sym_perf.core import InjectionSite, QueryCase, QueryPair, TransformConfig
from graph_sym_perf.query_ir import select_expression, wrap_as_case_true


TRANSFORM_RULE = "where_and_true_or_expr"
GDSMITH_TRANSFORM_RULE = "gdsmith_ir_redundant_predicate"

SUPPORTED_GDSMITH_RULES = (
    "and_true_or",
    "and_expr_or_true",
    "or_false_and",
    "or_expr_and_false",
    "and_not_false_and",
    "and_case_true",
)

SKIP_UNSUPPORTED_SCOPE_START = "UNSUPPORTED_SCOPE_START"
SKIP_UNSAFE_OPTIONAL_MATCH_SITE = "UNSAFE_OPTIONAL_MATCH_SITE"
SKIP_CALL_NOT_SUPPORTED = "CALL_NOT_SUPPORTED"
SKIP_EXISTS_NOT_SUPPORTED = "EXISTS_NOT_SUPPORTED"
SKIP_PATTERN_COMPREHENSION_NOT_SUPPORTED = "PATTERN_COMPREHENSION_NOT_SUPPORTED"
SKIP_VARIABLE_LENGTH_PATH_NOT_SUPPORTED = "VARIABLE_LENGTH_PATH_NOT_SUPPORTED"
SKIP_WRITE_STATEMENT = "WRITE_STATEMENT"
SKIP_NO_MATCH_CLAUSE = "NO_MATCH_CLAUSE"
SKIP_NO_BOUND_NODE_VARIABLE = "NO_BOUND_NODE_VARIABLE"
SKIP_NO_ENABLED_TRANSFORM_RULE = "NO_ENABLED_TRANSFORM_RULE"
SKIP_EXPRESSION_GENERATION_FAILED = "EXPRESSION_GENERATION_FAILED"

_CLAUSE_RE = re.compile(
    r"\b(OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN|CALL|CREATE|MERGE|DELETE|SET|REMOVE)\b",
    re.IGNORECASE,
)
_NODE_VAR_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9_]*)\s*(?::|\{|\))")
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PROPERTY_TEMPLATE = r"\b{}\s*\.\s*([A-Za-z][A-Za-z0-9_]*)\b"
_WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP)\b", re.IGNORECASE)
_EXISTS_RE = re.compile(r"\bEXISTS\b", re.IGNORECASE)
_CALL_RE = re.compile(r"\bCALL\b", re.IGNORECASE)


class TransformSkipError(ValueError):
    """Raised when a query is deliberately excluded from safe transformation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inject_redundant_predicate(
    query_case: QueryCase,
    transform_config: TransformConfig | None = None,
) -> QueryPair:
    config = transform_config or TransformConfig()
    if query_case.source == "gdsmith" or not query_case.base_predicate:
        return _inject_gdsmith_redundant_predicate(query_case, config)

    original = query_case.query
    needle = f"WHERE {query_case.base_predicate}"
    replacement = f"WHERE ({query_case.base_predicate}) AND (true OR ({query_case.safe_expression}))"
    if needle not in original:
        raise ValueError(f"query case {query_case.case_id} does not contain its base predicate")
    transformed = original.replace(needle, replacement, 1)
    return QueryPair(
        case_id=query_case.case_id,
        original_query=original,
        transformed_query=transformed,
        parameters=query_case.parameters,
        transform_rule=TRANSFORM_RULE,
        proof_obligation="true OR expr is logically true under Cypher boolean semantics when expr is true, false, or null.",
        transform_metadata={"source": query_case.source, "rule": TRANSFORM_RULE},
    )


def analyze_gdsmith_injection_site(query: str) -> InjectionSite:
    original = query.rstrip().rstrip(";")
    _reject_unsafe_scope(original)
    match = _first_non_optional_match_clause(original)
    if match is None:
        raise TransformSkipError(SKIP_NO_MATCH_CLAUSE, "query does not contain a supported non-optional MATCH clause")
    start, end = match
    clause = original[start:end]
    variables = _node_variables(clause)
    if not variables:
        raise TransformSkipError(SKIP_NO_BOUND_NODE_VARIABLE, "MATCH clause does not expose a bound node variable")
    where_match = _WHERE_RE.search(clause)
    base_predicate = clause[where_match.end() :].strip() if where_match else "true"
    return InjectionSite(
        clause_kind="MATCH",
        start=start,
        end=end,
        has_where=where_match is not None,
        bound_variables=variables,
        selected_variable=variables[0],
        base_predicate=base_predicate,
    )


def render_gdsmith_transform(
    original_query: str,
    injection_site: InjectionSite,
    *,
    rule: str,
    expression: str,
) -> str:
    original = original_query.rstrip().rstrip(";")
    clause = original[injection_site.start : injection_site.end]
    predicate = _render_rule(rule, injection_site.base_predicate, expression)
    where_match = _WHERE_RE.search(clause)
    if where_match is None:
        transformed_clause = f"{clause.rstrip()} WHERE {predicate}"
    else:
        before_where = clause[: where_match.start()].rstrip()
        transformed_clause = f"{before_where} WHERE {predicate}"
    suffix = original[injection_site.end :]
    separator = " " if suffix else ""
    return original[: injection_site.start] + transformed_clause.rstrip() + separator + suffix.lstrip()


def _inject_gdsmith_redundant_predicate(query_case: QueryCase, config: TransformConfig) -> QueryPair:
    original = query_case.query.rstrip().rstrip(";")
    site = analyze_gdsmith_injection_site(original)
    if site.selected_variable is None:
        raise TransformSkipError(SKIP_NO_BOUND_NODE_VARIABLE, "safe injection site has no selected variable")
    rules = _enabled_rules(config.enabled_rules)
    case_index = _case_index(query_case.case_id)
    rule = rules[case_index % len(rules)]
    property_names = _property_names(original, site.selected_variable)
    try:
        expression = select_expression(
            site.selected_variable,
            property_names=property_names,
            index=case_index // len(rules),
            max_complexity=config.max_expr_complexity,
        )
    except ValueError as exc:
        raise TransformSkipError(SKIP_EXPRESSION_GENERATION_FAILED, str(exc)) from exc

    rendered_expr = expression.rendered_cypher
    expression_record = expression
    if rule == "and_case_true":
        expression_record = wrap_as_case_true(expression)
        rendered_expr = expression.rendered_cypher

    transformed = render_gdsmith_transform(original, site, rule=rule, expression=rendered_expr)
    return QueryPair(
        case_id=query_case.case_id,
        original_query=original,
        transformed_query=transformed,
        parameters=query_case.parameters,
        transform_rule=rule,
        proof_obligation=_proof_obligation(rule, expression.rendered_cypher),
        expression_record=expression_record,
        injection_site=site,
        transform_metadata={
            "source": "gdsmith",
            "rule": rule,
            "rule_family": "conservative_tautology",
            "base_predicate": site.base_predicate,
            "expression_family": expression_record.family,
            "expression_id": expression_record.expr_id,
            "referenced_variables": expression_record.referenced_variables,
            "selected_variable": site.selected_variable,
            "safe_only": config.gdsmith_safe_only,
        },
    )


def _reject_unsafe_scope(query: str) -> None:
    stripped = query.lstrip()
    upper = stripped.upper()
    if upper.startswith("WITH "):
        raise TransformSkipError(SKIP_UNSUPPORTED_SCOPE_START, "query starts with WITH and may rebind scope")
    if upper.startswith("OPTIONAL MATCH"):
        raise TransformSkipError(SKIP_UNSAFE_OPTIONAL_MATCH_SITE, "query starts with OPTIONAL MATCH")
    if _CALL_RE.search(query):
        raise TransformSkipError(SKIP_CALL_NOT_SUPPORTED, "CALL and subquery scopes are not supported")
    if _EXISTS_RE.search(query):
        raise TransformSkipError(SKIP_EXISTS_NOT_SUPPORTED, "EXISTS predicates are not supported")
    if _WRITE_RE.search(query):
        raise TransformSkipError(SKIP_WRITE_STATEMENT, "write statements are not transformed")
    if _has_pattern_comprehension(query):
        raise TransformSkipError(SKIP_PATTERN_COMPREHENSION_NOT_SUPPORTED, "pattern comprehension is not supported")
    if _has_variable_length_path(query):
        raise TransformSkipError(SKIP_VARIABLE_LENGTH_PATH_NOT_SUPPORTED, "variable-length paths are not supported")


def _first_non_optional_match_clause(query: str) -> tuple[int, int] | None:
    matches = [
        match
        for match in _CLAUSE_RE.finditer(query)
        if not _inside_string(query, match.start()) and _is_clause_keyword(query, match)
    ]
    for index, match in enumerate(matches):
        keyword = " ".join(match.group(1).upper().split())
        if keyword != "MATCH":
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(query)
        return match.start(), end
    return None


def _is_clause_keyword(query: str, match: re.Match[str]) -> bool:
    keyword = " ".join(match.group(1).upper().split())
    if keyword != "WITH":
        return True

    prefix = query[: match.start()].rstrip().upper()
    return not (prefix.endswith("STARTS") or prefix.endswith("ENDS"))


def _node_variables(clause: str) -> tuple[str, ...]:
    variables: list[str] = []
    for match in _NODE_VAR_RE.finditer(clause):
        variable = match.group(1)
        if variable not in variables:
            variables.append(variable)
    return tuple(variables)


def _property_names(query: str, variable: str) -> tuple[str, ...]:
    pattern = re.compile(_PROPERTY_TEMPLATE.format(re.escape(variable)))
    properties: list[str] = []
    for match in pattern.finditer(query):
        prop = match.group(1)
        if prop not in properties:
            properties.append(prop)
    return tuple(properties)


def _enabled_rules(raw_rules: Iterable[str]) -> tuple[str, ...]:
    rules = tuple(rule for rule in raw_rules if rule in SUPPORTED_GDSMITH_RULES)
    if not rules:
        raise TransformSkipError(SKIP_NO_ENABLED_TRANSFORM_RULE, "no supported GDsmith transform rule is enabled")
    return rules


def _case_index(case_id: str) -> int:
    match = re.search(r"(\d+)$", case_id)
    if not match:
        return 0
    return max(int(match.group(1)) - 1, 0)


def _render_rule(rule: str, base_predicate: str, expression: str) -> str:
    base = base_predicate.strip() or "true"
    expr = expression.strip()
    if rule == "and_true_or":
        return f"({base}) AND (true OR ({expr}))"
    if rule == "and_expr_or_true":
        return f"({base}) AND (({expr}) OR true)"
    if rule == "or_false_and":
        return f"({base}) OR (false AND ({expr}))"
    if rule == "or_expr_and_false":
        return f"({base}) OR (({expr}) AND false)"
    if rule == "and_not_false_and":
        return f"({base}) AND NOT(false AND ({expr}))"
    if rule == "and_case_true":
        return f"({base}) AND CASE WHEN ({expr}) THEN true ELSE true END"
    raise TransformSkipError(SKIP_NO_ENABLED_TRANSFORM_RULE, f"unsupported transform rule: {rule}")


def _proof_obligation(rule: str, expression: str) -> str:
    return (
        f"Rule {rule} inserts a tautological wrapper around a deterministic, side-effect-free expression "
        f"after a non-optional MATCH binding point. The expression is: {expression}."
    )


def _has_pattern_comprehension(query: str) -> bool:
    for match in re.finditer(r"\[[^\]]*\|[^\]]*\]", query, re.DOTALL):
        snippet = match.group(0)
        if "(" in snippet and ")" in snippet:
            return True
    return False


def _has_variable_length_path(query: str) -> bool:
    return bool(re.search(r"\[[^\]]*\*", query))


def _inside_string(text: str, position: int) -> bool:
    quote: str | None = None
    escaped = False
    for char in text[:position]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is None and char in {"'", '"'}:
            quote = char
            continue
        if quote == char:
            quote = None
    return quote is not None
