"""Conservative query-purity gate used before automatic validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from graph_sym_perf.cypher.parser import parse_query


@dataclass(frozen=True)
class PurityEvidence:
    safe: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PurityContext:
    """Externally verified ordering facts used by the conservative purity gate."""

    unique_order_keys: tuple[str, ...] = ()
    collect_input_order: tuple[str, ...] = ()
    expected_final_clause: str = "RETURN"


_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b", re.IGNORECASE)
_VOLATILE = re.compile(
    r"\b(rand|random|randomuuid|datetime|localdatetime|date|time|localtime|timestamp)\s*\(",
    re.IGNORECASE,
)
_INTERNAL_ID = re.compile(r"\b(id|elementid)\s*\(", re.IGNORECASE)
_NON_FINITE = re.compile(
    r"\b(?:nan|inf|infinity)\b|\btofloat\s*\(\s*['\"](?:nan|[+-]?(?:inf|infinity))['\"]\s*\)|"
    r"(?:^|[^\w.])(?:[+-]?\d+(?:\.\d+)?)\s*/\s*[+-]?0+(?:\.0+)?(?:[^\w.]|$)",
    re.IGNORECASE,
)


def assess_query_purity(
    query: str,
    *,
    volatile_functions: tuple[str, ...] = (),
    context: PurityContext | None = None,
) -> PurityEvidence:
    upper = query.upper()
    reasons: list[str] = []
    clauses = parse_query(query).clauses
    if not clauses or query[: clauses[0].start].strip():
        reasons.append("unparsed_clause_prefix")
    if any(clause.keyword not in {"MATCH", "OPTIONAL MATCH", "WITH", "UNWIND", "RETURN"} for clause in clauses):
        reasons.append("unsupported_clause")
    final_clause = clauses[-1] if clauses else None
    if context is not None and (
        final_clause is None or final_clause.keyword != context.expected_final_clause.upper()
    ):
        reasons.append("final_clause_mismatch")
    if _WRITE.search(query):
        reasons.append("write_clause")
    if _VOLATILE.search(query) or any(re.search(rf"\b{re.escape(name)}\s*\(", query, re.IGNORECASE) for name in volatile_functions):
        reasons.append("volatile_function")
    if _INTERNAL_ID.search(query):
        reasons.append("internal_id_function")
    if re.search(r"\bCALL\b", query, re.IGNORECASE):
        reasons.append("procedural_call")
    if _NON_FINITE.search(query):
        reasons.append("non_finite_float")
    if re.search(r"\b(LIMIT|SKIP)\b", upper):
        final_order = _order_keys(final_clause.text) if final_clause is not None else ()
        if (
            context is None
            or not context.unique_order_keys
            or final_clause is None
            or final_clause.keyword != context.expected_final_clause.upper()
            or final_order != context.unique_order_keys
        ):
            reasons.append("unordered_limit_or_skip")
    for collect in re.finditer(r"\bcollect\s*\(", query, re.IGNORECASE):
        collect_clause_index = next(
            (index for index, clause in enumerate(clauses) if clause.start <= collect.start() < clause.end),
            -1,
        )
        input_clause = clauses[collect_clause_index - 1] if collect_clause_index > 0 else None
        input_order = _order_keys(input_clause.text) if input_clause is not None else ()
        if (
            context is None
            or collect_clause_index != len(clauses) - 1
            or final_clause is None
            or final_clause.keyword != context.expected_final_clause.upper()
            or input_clause is None
            or input_clause.keyword != "WITH"
            or not context.unique_order_keys
            or input_order != context.collect_input_order
            or input_order != context.unique_order_keys
        ):
            reasons.append("unstable_collect")
            break
    return PurityEvidence(not reasons, tuple(reasons))


def _order_keys(clause_text: str) -> tuple[str, ...]:
    match = re.search(
        r"\bORDER\s+BY\b(?P<body>.*?)(?=\bLIMIT\b|\bSKIP\b|$)",
        clause_text,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ()
    keys = []
    for raw in match.group("body").split(","):
        key = re.sub(r"\s+(?:ASC|DESC)\s*$", "", raw.strip(), flags=re.IGNORECASE)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", key):
            return ()
        keys.append(key)
    return tuple(keys)
