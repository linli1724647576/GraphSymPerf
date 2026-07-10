"""Conservative query-purity gate used before automatic validation."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PurityEvidence:
    safe: bool
    reason_codes: tuple[str, ...]


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


def assess_query_purity(query: str, *, volatile_functions: tuple[str, ...] = ()) -> PurityEvidence:
    upper = query.upper()
    reasons: list[str] = []
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
    if re.search(r"\b(LIMIT|SKIP)\b", upper) and "ORDER BY" not in upper:
        reasons.append("unordered_limit_or_skip")
    for collect in re.finditer(r"\bcollect\s*\(", query, re.IGNORECASE):
        order_positions = tuple(match.start() for match in re.finditer(r"\bORDER\s+BY\b", query, re.IGNORECASE))
        prior_orders = tuple(position for position in order_positions if position < collect.start())
        last_order = max(prior_orders, default=-1)
        invalidating_source = re.search(
            r"\b(?:OPTIONAL\s+MATCH|MATCH|UNWIND|CALL)\b",
            query[last_order + 1 : collect.start()],
            re.IGNORECASE,
        )
        if last_order < 0 or invalidating_source is not None:
            reasons.append("unstable_collect")
            break
    return PurityEvidence(not reasons, tuple(reasons))
