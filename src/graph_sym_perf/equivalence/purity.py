"""Conservative query-purity gate used before automatic validation."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PurityEvidence:
    safe: bool
    reason_codes: tuple[str, ...]


_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD\s+CSV)\b", re.IGNORECASE)
_VOLATILE = re.compile(r"\b(rand|random|datetime|date|time|timestamp|id)\s*\(", re.IGNORECASE)


def assess_query_purity(query: str, *, volatile_functions: tuple[str, ...] = ()) -> PurityEvidence:
    upper = query.upper()
    reasons: list[str] = []
    if _WRITE.search(query):
        reasons.append("write_clause")
    if _VOLATILE.search(query) or any(re.search(rf"\b{re.escape(name)}\s*\(", query, re.IGNORECASE) for name in volatile_functions):
        reasons.append("volatile_function")
    if re.search(r"\b(LIMIT|SKIP)\b", upper) and "ORDER BY" not in upper:
        reasons.append("unordered_limit_or_skip")
    if re.search(r"\bcollect\s*\(", query, re.IGNORECASE) and "ORDER BY" not in upper:
        reasons.append("unstable_collect")
    return PurityEvidence(not reasons, tuple(reasons))
