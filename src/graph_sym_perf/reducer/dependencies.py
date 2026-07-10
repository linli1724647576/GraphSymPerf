"""Conservative variable dependency checks for Cypher clause trimming."""

from __future__ import annotations

import re


_VARIABLE = re.compile(r"\(([A-Za-z_]\w*)|\[([A-Za-z_]\w*)")


def clause_variables(clause: str) -> frozenset[str]:
    return frozenset(value for groups in _VARIABLE.findall(clause) for value in groups if value)


def dependency_safe_removal(clauses: tuple[str, ...], remove_index: int) -> bool:
    """Only remove a clause when it defines no variable used by a later clause."""
    removed = clause_variables(clauses[remove_index])
    later = frozenset().union(*(clause_variables(clause) for clause in clauses[remove_index + 1 :]))
    return not bool(removed & later)
