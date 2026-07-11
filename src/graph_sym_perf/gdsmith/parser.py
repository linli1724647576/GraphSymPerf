"""Parse GDsmith logs into graph setup statements and query records."""

from __future__ import annotations

import re
from dataclasses import dataclass


_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH)\b",
    re.IGNORECASE,
)
_READ_START = re.compile(r"^(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND)\b", re.IGNORECASE)
_SCHEMA_START = re.compile(
    r"^CREATE\s+(?:INDEX|PROPERTY\s+INDEX|VLABEL|ELABEL)\b",
    re.IGNORECASE,
)
_GRAPH_NODE_CREATE = re.compile(r"^CREATE\s*\(", re.IGNORECASE)
_GRAPH_RELATIONSHIP_MERGE = re.compile(r"^MATCH\b.*\bMERGE\s*\(", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class GdsmithStatementRecord:
    index: int
    statement: str
    kind: str
    accepted: bool
    skip_reason: str | None = None


@dataclass(frozen=True)
class GdsmithParsedLog:
    graph_statements: tuple[str, ...]
    query_records: tuple[GdsmithStatementRecord, ...]

    @property
    def accepted_queries(self) -> tuple[GdsmithStatementRecord, ...]:
        return tuple(record for record in self.query_records if record.accepted)


def parse_gdsmith_log(log_text: str, *, max_cases: int | None = None) -> GdsmithParsedLog:
    statements = _split_logged_statements(log_text)
    graph_statements: list[str] = []
    query_records: list[GdsmithStatementRecord] = []
    accepted_count = 0

    for index, statement in enumerate(statements, start=1):
        kind, accepted, skip_reason = _classify_statement(statement)
        if kind == "graph":
            graph_statements.append(statement)
            continue
        if accepted and max_cases is not None and accepted_count >= max_cases:
            accepted = False
            skip_reason = "MAX_CASES_REACHED"
        if accepted:
            accepted_count += 1
        query_records.append(
            GdsmithStatementRecord(
                index=index,
                statement=statement,
                kind=kind,
                accepted=accepted,
                skip_reason=skip_reason,
            )
        )
    return GdsmithParsedLog(graph_statements=tuple(graph_statements), query_records=tuple(query_records))


def _split_logged_statements(log_text: str) -> tuple[str, ...]:
    statements: list[str] = []
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        if not line.endswith(";"):
            continue
        statement = line[:-1].strip()
        if statement:
            statements.append(statement)
    return tuple(statements)


def _classify_statement(statement: str) -> tuple[str, bool, str | None]:
    normalized = statement.strip()
    if not normalized:
        return "skipped", False, "EMPTY_STATEMENT"

    has_write = bool(_WRITE_KEYWORDS.search(normalized))
    if _SCHEMA_START.match(normalized):
        return "skipped", False, "SCHEMA_SETUP_NOT_REPLAYED"
    if _GRAPH_NODE_CREATE.match(normalized) or _GRAPH_RELATIONSHIP_MERGE.match(normalized):
        return "graph", False, "GRAPH_SETUP"
    if has_write:
        return "skipped", False, "WRITE_STATEMENT"
    if not _READ_START.match(normalized):
        return "skipped", False, "UNSUPPORTED_START_CLAUSE"
    if not re.search(r"\bRETURN\b", normalized, flags=re.IGNORECASE):
        return "skipped", False, "NO_RETURN_CLAUSE"
    if re.search(r"\bCALL\b", normalized, flags=re.IGNORECASE):
        return "skipped", False, "CALL_NOT_SUPPORTED"
    return "query", True, None
