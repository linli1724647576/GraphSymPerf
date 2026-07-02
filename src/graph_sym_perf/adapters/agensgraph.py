"""AgensGraph adapter over PostgreSQL."""

from __future__ import annotations

import re
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.datasets import MiniLdbcGraph
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph, GdsmithReplayNode, parse_gdsmith_replay_graph
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class AgensGraphAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("psycopg",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._conn = None
        self._graph_name = None

    def connectivity_check(self) -> tuple[bool, str]:
        try:
            self._connect()
            with self._conn.cursor() as cursor:
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
            cursor.execute(f"CREATE GRAPH IF NOT EXISTS {_identifier(self._graph_name)}")
            cursor.execute(f"SET graph_path = {_identifier(self._graph_name)}")
        self._conn.commit()

    def reset_schema(self) -> None:
        self._connect()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(f"DROP GRAPH IF EXISTS {_identifier(self._graph_name)} CASCADE")
            self._conn.commit()
            self.ensure_schema()
        except Exception as exc:
            self._conn.rollback()
            raise AdapterError(f"cannot reset AgensGraph graph: {exc}") from exc

    def load_mini_ldbc(self, graph: MiniLdbcGraph) -> None:
        self.ensure_schema()
        self._ensure_labels(
            vertex_labels=tuple(sorted({node.label for node in graph.nodes})),
            edge_labels=tuple(sorted({relationship.relationship_type for relationship in graph.relationships})),
        )
        CypherLoaderMixin.load_mini_ldbc(self, graph)

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        self.ensure_schema()
        self._ensure_labels(
            vertex_labels=tuple(sorted({self._primary_node_label(node) for node in graph.nodes})),
            edge_labels=tuple(sorted({relationship.relationship_type for relationship in graph.relationships})),
        )
        for node in graph.nodes:
            self._merge_gdsmith_node(node)
        for relationship in graph.relationships:
            self._merge_gdsmith_relationship(relationship)
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
        cypher = _inline_parameters(query, parameters)
        truncated = False
        try:
            with self._conn.cursor() as cursor:
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                self._set_graph_path(cursor)
                cursor.execute(cypher)
                aliases = tuple(item.name for item in (cursor.description or ()))
                rows_raw = cursor.fetchall() if cursor.description else ()
                if max_rows is not None and len(rows_raw) > max_rows:
                    truncated = True
                    rows_raw = rows_raw[: max_rows + 1]
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise QueryExecutionError(f"AgensGraph query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.AGENSGRAPH,
            query_kind=query_kind,
            rows=tuple(
                {aliases[index]: to_jsonable(value) for index, value in enumerate(row)}
                for row in rows_raw
            ),
            elapsed_ms=elapsed_ms,
            summary={
                "client": "psycopg",
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            cypher = _inline_parameters(query, parameters)
            sql = f"EXPLAIN {cypher}"
            self._connect()
            with self._conn.cursor() as cursor:
                self._set_graph_path(cursor)
                cursor.execute(sql)
                rows = tuple(row[0] for row in cursor.fetchall())
            self._conn.commit()
            return make_plan_record(TargetSystem.AGENSGRAPH, query_kind, rows)
        except Exception as exc:
            if self._conn is not None:
                self._conn.rollback()
            return unavailable_plan(TargetSystem.AGENSGRAPH, query_kind, str(exc))

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
        cypher = _inline_parameters(query, parameters)
        try:
            with self._conn.cursor() as cursor:
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                self._set_graph_path(cursor)
                cursor.execute(f"EXPLAIN ANALYZE {cypher}")
                profile_rows = tuple(row[0] for row in cursor.fetchall())
            self._conn.commit()
            elapsed_ms = (perf_counter() - start) * 1000.0
            return ExecutionResult(
                system=TargetSystem.AGENSGRAPH,
                query_kind=query_kind,
                rows=(),
                elapsed_ms=elapsed_ms,
                summary={
                    "client": "psycopg",
                    "truncated": False,
                    "row_cap": max_rows,
                    "timeout_seconds": timeout_seconds,
                    "profile": profile_rows,
                    "profile_available": True,
                    "profile_command": "EXPLAIN ANALYZE",
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
                    "profile_command": "EXPLAIN ANALYZE",
                    "profile_error": str(exc),
                },
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        if _has_multi_label_node(query):
            return False
        if _has_variable_label_conflict(query):
            return False
        if _has_distinct_order_by_projection_mismatch(query):
            return False
        return True

    def _merge_gdsmith_node(self, node: GdsmithReplayNode) -> None:
        label = self._primary_node_label(node)
        query = f"""
MERGE (n:{label} {{id: $id}})
SET n += $properties
"""
        properties = dict(node.properties)
        properties.setdefault("id", node.node_id)
        self.execute(
            _compact(query),
            {"id": node.node_id, "properties": properties},
            "gdsmith_load_node",
        )

    def _ensure_labels(self, *, vertex_labels: tuple[str, ...], edge_labels: tuple[str, ...]) -> None:
        self._connect()
        try:
            with self._conn.cursor() as cursor:
                self._set_graph_path(cursor)
                for label in vertex_labels:
                    cursor.execute(f"CREATE VLABEL IF NOT EXISTS {_identifier(label)}")
                for label in edge_labels:
                    cursor.execute(f"CREATE ELABEL IF NOT EXISTS {_identifier(label)}")
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise AdapterError(f"cannot prepare AgensGraph labels: {exc}") from exc

    def _primary_node_label(self, node: GdsmithReplayNode) -> str:
        return _identifier((node.labels or ("GdsmithNode",))[0])

    def _connect(self) -> None:
        if self._conn is not None:
            return
        try:
            import psycopg

            uri = self.env_value("GRAPHSYMPERF_AGENSGRAPH_URI")
            database = self.optional_env_value("database")
            if database:
                uri = _uri_with_db(uri, database)
            self._graph_name = self.env_value("GRAPHSYMPERF_AGENSGRAPH_GRAPH", "graph_sym_perf")
            self._conn = psycopg.connect(uri)
        except Exception as exc:
            raise AdapterError(f"cannot connect to AgensGraph: {exc}") from exc

    def _set_graph_path(self, cursor: Any) -> None:
        cursor.execute(f"SET graph_path = {_identifier(self._graph_name)}")


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
    if isinstance(value, Mapping):
        return _map_literal(value)
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _map_literal(values: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{_property_key(key)}: {_literal(value)}" for key, value in values.items()) + "}"


def _identifier(value: str | None) -> str:
    if not value or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
        raise AdapterError(f"unsafe AgensGraph graph name: {value!r}")
    return value


def _property_key(value: str) -> str:
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", str(value)):
        raise AdapterError(f"unsafe AgensGraph property key: {value!r}")
    return str(value)


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())


def _uri_with_db(uri: str | None, database: str) -> str:
    if uri is None or "/" in uri.rsplit("@", 1)[-1]:
        return uri or ""
    return uri.rstrip("/") + "/" + database


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


def _return_body(query: str) -> str:
    match = None
    for candidate in re.finditer(r"\bRETURN\b", query, flags=re.IGNORECASE):
        match = candidate
    if match is None:
        return ""
    tail = query[match.end() :]
    stop = _first_top_level_keyword(tail, ("ORDER BY", "SKIP", "LIMIT", "UNION"))
    return tail[:stop].strip() if stop is not None else tail.strip()


def _order_by_body(query: str) -> str:
    match = None
    for candidate in re.finditer(r"\bORDER\s+BY\b", query, flags=re.IGNORECASE):
        match = candidate
    if match is None:
        return ""
    tail = query[match.end() :]
    stop = _first_top_level_keyword(tail, ("SKIP", "LIMIT", "UNION"))
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


def _canonical_expression(text: str) -> str:
    expr = re.sub(r"\bAS\s+[A-Za-z]\w*\s*$", "", text.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", expr).strip().lower()
