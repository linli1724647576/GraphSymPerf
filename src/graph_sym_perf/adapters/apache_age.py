"""Apache AGE adapter over PostgreSQL cypher() wrappers."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph, parse_gdsmith_replay_graph
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class ApacheAgeAdapter(BaseAdapter):
    dependency_modules = ("psycopg",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._conn = None
        self._graph_name = None

    def connectivity_check(self) -> tuple[bool, str]:
        try:
            self._connect()
            with self._conn.cursor() as cursor:
                self._prepare_session(cursor)
                cursor.execute("SELECT 1")
                cursor.fetchone()
            if self.allow_create:
                self.ensure_schema()
            return True, "connected"
        except Exception as exc:
            return False, str(exc)

    def ensure_schema(self) -> None:
        self._connect()
        with self._conn.cursor() as cursor:
            self._prepare_session(cursor)
            cursor.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (self._graph_name,))
            exists = int(cursor.fetchone()[0]) > 0
            if not exists:
                cursor.execute("SELECT * FROM ag_catalog.create_graph(%s)", (self._graph_name,))
        self._conn.commit()

    def load_mini_ldbc(self, graph) -> None:
        self.ensure_schema()
        for node in graph.nodes:
            labels = (node.label,)
            self._run_wrapped(_create_node_cypher(labels, node.properties), "gdsmith_load_node")
        for relationship in graph.relationships:
            props = dict(relationship.properties)
            query = (
                f"MATCH (a:{relationship.start_label} {{gbp_id: {_literal(relationship.start_id)}}}), "
                f"(b:{relationship.end_label} {{gbp_id: {_literal(relationship.end_id)}}}) "
                f"CREATE (a)-[r:{relationship.relationship_type} {_map_literal(props)}]->(b)"
            )
            self._run_wrapped(query, "gdsmith_load_relationship")

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        self.ensure_schema()
        node_labels: dict[int, tuple[str, ...]] = {}
        for node in graph.nodes:
            labels = node.labels or ("GdsmithNode",)
            node_labels[node.node_id] = labels
            self._run_wrapped(_create_node_cypher(labels, node.properties), "gdsmith_load_node")
        for relationship in graph.relationships:
            start_labels = node_labels.get(relationship.start_id, ("GdsmithNode",))
            end_labels = node_labels.get(relationship.end_id, ("GdsmithNode",))
            query = (
                f"MATCH (a:{start_labels[0]} {{id: {_literal(relationship.start_id)}}}), "
                f"(b:{end_labels[0]} {{id: {_literal(relationship.end_id)}}}) "
                f"CREATE (a)-[r:{relationship.relationship_type} {_map_literal(relationship.properties)}]->(b)"
            )
            self._run_wrapped(query, "gdsmith_load_relationship")
        return graph

    def execute(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        self._connect()
        start = perf_counter()
        truncated = False
        cypher = _inline_parameters(query, parameters)
        sql, aliases = self._wrap_select(cypher)
        try:
            with self._conn.cursor() as cursor:
                self._prepare_session(cursor)
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                cursor.execute(sql)
                rows_raw = cursor.fetchall() if cursor.description else ()
                if max_rows is not None and len(rows_raw) > max_rows:
                    truncated = True
                    rows_raw = rows_raw[: max_rows + 1]
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise QueryExecutionError(f"Apache AGE query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.APACHE_AGE,
            query_kind=query_kind,
            rows=tuple(_row_from_tuple(aliases, row) for row in rows_raw),
            elapsed_ms=elapsed_ms,
            summary={
                "client": "psycopg",
                "wrapper": "cypher",
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            cypher = _inline_parameters(query, parameters)
            sql, _aliases = self._wrap_select(cypher)
            rows = self._explain_rows(f"EXPLAIN {sql}")
            return make_plan_record(TargetSystem.APACHE_AGE, query_kind, rows)
        except Exception as exc:
            if self._conn is not None:
                self._conn.rollback()
            return unavailable_plan(TargetSystem.APACHE_AGE, query_kind, str(exc))

    def profile(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        self._connect()
        start = perf_counter()
        try:
            cypher = _inline_parameters(query, parameters)
            sql, _aliases = self._wrap_select(cypher)
            with self._conn.cursor() as cursor:
                self._prepare_session(cursor)
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}")
                profile_rows = tuple(row[0] for row in cursor.fetchall())
            self._conn.commit()
            elapsed_ms = (perf_counter() - start) * 1000.0
            return ExecutionResult(
                system=TargetSystem.APACHE_AGE,
                query_kind=query_kind,
                rows=(),
                elapsed_ms=elapsed_ms,
                summary={
                    "client": "psycopg",
                    "wrapper": "cypher",
                    "truncated": False,
                    "row_cap": max_rows,
                    "timeout_seconds": timeout_seconds,
                    "profile": profile_rows,
                    "profile_available": True,
                    "profile_command": "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)",
                },
            )
        except Exception as exc:
            self._conn.rollback()
            result = self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)
            return ExecutionResult(
                system=result.system,
                query_kind=result.query_kind,
                rows=result.rows,
                elapsed_ms=result.elapsed_ms,
                summary={
                    **dict(result.summary),
                    "profile": None,
                    "profile_available": False,
                    "profile_command": "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)",
                    "profile_error": str(exc),
                },
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def _run_wrapped(self, cypher: str, query_kind: str) -> ExecutionResult:
        query = cypher if re.search(r"\bRETURN\b", cypher, flags=re.IGNORECASE) else f"{cypher} RETURN 1 AS ok"
        return self.execute(query, {}, query_kind)

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        if _has_multi_label_node(query):
            return False
        if _has_variable_label_conflict(query):
            return False
        if _has_distinct_order_by_projection_mismatch(query):
            return False
        return True

    def _explain_rows(self, sql: str) -> tuple[str, ...]:
        self._connect()
        with self._conn.cursor() as cursor:
            self._prepare_session(cursor)
            cursor.execute(sql)
            rows = tuple(row[0] for row in cursor.fetchall())
        self._conn.commit()
        return rows

    def _wrap_select(self, cypher: str) -> tuple[str, tuple[str, ...]]:
        aliases = _return_aliases(cypher)
        if not aliases:
            aliases = ("result",)
        escaped = cypher.replace("$$", "$ graph_sym_perf_dollar $")
        columns = ", ".join(f"{_sql_identifier(alias)} agtype" for alias in aliases)
        sql = f"SELECT * FROM ag_catalog.cypher({_sql_literal(self._graph_name)}, $${escaped}$$) AS ({columns})"
        return sql, aliases

    def _connect(self) -> None:
        if self._conn is not None:
            return
        try:
            import psycopg

            uri = self.env_value("GRAPHSYMPERF_APACHE_AGE_URI")
            database = self.optional_env_value("database")
            if database:
                uri = _uri_with_db(uri, database)
            self._graph_name = self.env_value("GRAPHSYMPERF_APACHE_AGE_GRAPH", "graph_sym_perf")
            self._conn = psycopg.connect(uri)
        except Exception as exc:
            raise AdapterError(f"cannot connect to Apache AGE: {exc}") from exc

    def _prepare_session(self, cursor: Any) -> None:
        cursor.execute("LOAD 'age'")
        cursor.execute('SET search_path = ag_catalog, "$user", public')


def _row_from_tuple(aliases: tuple[str, ...], row: tuple[Any, ...]) -> Mapping[str, Any]:
    return {aliases[index]: _age_value(value) for index, value in enumerate(row)}


def _age_value(value: Any) -> Any:
    converted = to_jsonable(value)
    if isinstance(converted, str):
        stripped = converted.strip()
        if stripped and stripped[0] in "[{\"" and stripped[-1] in "]}\"":
            try:
                return json.loads(stripped)
            except Exception:
                return stripped
    return converted


def _return_aliases(query: str) -> tuple[str, ...]:
    return_body = _return_body(query)
    if not return_body:
        return ()
    aliases: list[str] = []
    for index, item in enumerate(_split_top_level_commas(return_body), start=1):
        explicit = re.search(r"\bAS\s+([A-Za-z]\w*)\s*$", item, flags=re.IGNORECASE)
        aliases.append(explicit.group(1) if explicit else f"col{index}")
    return tuple(aliases)


def _return_body(query: str) -> str:
    match = None
    for candidate in re.finditer(r"\bRETURN\b", query, flags=re.IGNORECASE):
        match = candidate
    if match is None:
        return ""
    tail = query[match.end() :]
    stop = _first_top_level_keyword(tail, ("ORDER BY", "SKIP", "LIMIT", "UNION"))
    return tail[:stop].strip() if stop is not None else tail.strip()


def _first_top_level_keyword(text: str, keywords: tuple[str, ...]) -> int | None:
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            upper = text[index:].upper()
            for keyword in keywords:
                if upper.startswith(keyword):
                    return index
        index += 1
    return None


def _split_top_level_commas(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    parts.append(text[start:].strip())
    return tuple(part for part in parts if part)


def _create_node_cypher(labels: tuple[str, ...], properties: Mapping[str, Any]) -> str:
    label_text = f":{labels[0]}" if labels else ":GdsmithNode"
    return f"CREATE (n{label_text} {_map_literal(properties)})"


def _has_multi_label_node(query: str) -> bool:
    return bool(re.search(r"\([^)]*:\s*[A-Za-z]\w*\s*:\s*[A-Za-z]\w*", query))


def _has_variable_label_conflict(query: str) -> bool:
    labels_by_variable: dict[str, set[str | None]] = {}
    for match in re.finditer(r"\(\s*([A-Za-z]\w*)\s*((?::\s*[A-Za-z]\w*)*)", query):
        variable = match.group(1)
        if variable.upper() in {"WHERE", "RETURN", "MATCH", "OPTIONAL", "WITH"}:
            continue
        labels = set(re.findall(r":\s*([A-Za-z]\w*)", match.group(2))) or {None}
        labels_by_variable.setdefault(variable, set()).update(labels)
        if len(labels_by_variable[variable]) > 1:
            return True
    return False


def _has_distinct_order_by_projection_mismatch(query: str) -> bool:
    return_body = _return_body(query)
    if not return_body:
        return False
    if not return_body.upper().lstrip().startswith("DISTINCT "):
        return False
    order_body = _order_by_body(query)
    if not order_body:
        return False
    projected = {
        _canonical_expression(item.removeprefix("DISTINCT").strip())
        for item in _split_top_level_commas(return_body)
    }
    order_expressions = {
        _canonical_expression(re.sub(r"\s+(ASC|DESC)\s*$", "", item, flags=re.IGNORECASE))
        for item in _split_top_level_commas(order_body)
    }
    return bool(order_expressions - projected)


def _order_by_body(query: str) -> str:
    match = None
    for candidate in re.finditer(r"\bORDER\s+BY\b", query, flags=re.IGNORECASE):
        match = candidate
    if match is None:
        return ""
    tail = query[match.end() :]
    stop = _first_top_level_keyword(tail, ("SKIP", "LIMIT", "UNION"))
    return tail[:stop].strip() if stop is not None else tail.strip()


def _canonical_expression(text: str) -> str:
    expr = re.sub(r"\bAS\s+[A-Za-z]\w*\s*$", "", text.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", expr).strip().lower()


def _map_literal(values: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{key}: {_literal(value)}" for key, value in values.items()) + "}"


def _inline_parameters(query: str, parameters: Mapping[str, Any]) -> str:
    rendered = query
    for name, value in sorted(parameters.items(), key=lambda item: len(item[0]), reverse=True):
        rendered = re.sub(rf"\${re.escape(str(name))}\b", _literal(value), rendered)
    return rendered


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _sql_literal(value: str | None) -> str:
    if value is None or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
        raise AdapterError(f"unsafe Apache AGE graph name: {value!r}")
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
        value = "col"
    return '"' + value.replace('"', '""') + '"'


def _uri_with_db(uri: str | None, database: str) -> str:
    if uri is None:
        return ""
    parsed = urlparse(uri)
    return urlunparse(parsed._replace(path="/" + database))
