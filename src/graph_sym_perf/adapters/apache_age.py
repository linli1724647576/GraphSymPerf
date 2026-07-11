"""Apache AGE adapter over PostgreSQL cypher() wrappers."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from graph_sym_perf.adapters.base import BaseAdapter, normalized_uri_endpoint
from graph_sym_perf.adapters.common_cypher import _query_execution_error
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, TargetSystem, to_typed_value as to_jsonable
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.base import (
    DecisionKind,
    has_distinct_order_by_projection_mismatch as _dialect_distinct_order_mismatch,
    has_multi_label_node as _dialect_has_multi_label_node,
    has_variable_label_conflict as _dialect_variable_label_conflict,
    parse_projection,
    render_cypher_literal,
)
from graph_sym_perf.generation.corpus import PortableQuery
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

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        for node in graph.nodes:
            _single_node_label(node.labels)
        self.ensure_schema()
        node_labels: dict[int, tuple[str, ...]] = {}
        for node in graph.nodes:
            labels = (_single_node_label(node.labels),)
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
        compiled = self._compile(query, parameters)
        sql = compiled.execution_envelope.statement
        aliases = tuple(column.name for column in compiled.result_schema)
        try:
            with self._conn.cursor() as cursor:
                self._prepare_session(cursor)
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                cursor.execute(sql)
                if not cursor.description:
                    rows_raw = ()
                elif max_rows is None:
                    rows_raw = cursor.fetchall()
                else:
                    rows_raw = tuple(cursor.fetchmany(max_rows + 1))
                    truncated = len(rows_raw) > max_rows
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise _query_execution_error(f"Apache AGE query failed: {exc}", exc) from exc
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
            compiled = self._compile(query, parameters)
            sql = compiled.execution_envelope.statement
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
            compiled = self._compile(query, parameters)
            sql = compiled.execution_envelope.statement
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

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        endpoint = dict(
            normalized_uri_endpoint(
                self.env_value("GRAPHSYMPERF_APACHE_AGE_URI"),
                default_port=5432,
                database=self.optional_env_value("database"),
            )
        )
        endpoint["graph"] = self.env_value("GRAPHSYMPERF_APACHE_AGE_GRAPH", "graph_sym_perf")
        return endpoint

    def _probe_health_epoch_identity(self) -> tuple[str, object] | None:
        self._connect()
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT pg_postmaster_start_time()")
            row = cursor.fetchone()
        return ("pg_postmaster_start_time", row[0]) if row and row[0] not in (None, "") else None

    def _run_wrapped(self, cypher: str, query_kind: str) -> ExecutionResult:
        query = cypher if re.search(r"\bRETURN\b", cypher, flags=re.IGNORECASE) else f"{cypher} RETURN 1 AS ok"
        return self.execute(query, {}, query_kind)

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        graph_name = getattr(self, "_graph_name", None) or "graph_sym_perf"
        return ApacheAgeDialect(graph_name=graph_name).check(PortableQuery(query)).kind is not DecisionKind.REJECT

    def _explain_rows(self, sql: str) -> tuple[str, ...]:
        self._connect()
        with self._conn.cursor() as cursor:
            self._prepare_session(cursor)
            cursor.execute(sql)
            rows = tuple(row[0] for row in cursor.fetchall())
        self._conn.commit()
        return rows

    def _wrap_select(self, cypher: str) -> tuple[str, tuple[str, ...]]:
        compiled = self._compile(cypher, {})
        aliases = tuple(column.name for column in compiled.result_schema)
        return compiled.execution_envelope.statement, aliases

    def _compile(self, query: str, parameters: Mapping[str, Any]):
        dialect = ApacheAgeDialect(graph_name=self._graph_name or "graph_sym_perf")
        decision = dialect.check(PortableQuery(query, parameters=parameters))
        if decision.kind is DecisionKind.REJECT or decision.compiled_query is None:
            raise AdapterError(f"Apache AGE dialect rejected query: {decision.reason_code.value}")
        return decision.compiled_query

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
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT version()")
                postgres_row = cursor.fetchone()
                cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'age'")
                age_row = cursor.fetchone()
            if postgres_row and age_row:
                self._cache_server_version(f"{postgres_row[0]}|age:{age_row[0]}")
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
    projection = parse_projection(query, allow_star_scope=True)
    return tuple(column.name for column in projection.columns)


def _create_node_cypher(labels: tuple[str, ...], properties: Mapping[str, Any]) -> str:
    label_text = f":{_single_node_label(labels)}"
    return f"CREATE (n{label_text} {_map_literal(properties)})"


def _has_multi_label_node(query: str) -> bool:
    return _dialect_has_multi_label_node(query)


def _has_variable_label_conflict(query: str) -> bool:
    return _dialect_variable_label_conflict(query)


def _has_distinct_order_by_projection_mismatch(query: str) -> bool:
    return _dialect_distinct_order_mismatch(query)


def _map_literal(values: Mapping[str, Any]) -> str:
    return render_cypher_literal(values)


def _literal(value: Any) -> str:
    return render_cypher_literal(value)


def _single_node_label(labels: tuple[str, ...]) -> str:
    effective = labels or ("GdsmithNode",)
    if len(effective) != 1:
        raise AdapterError("MULTI_LABEL_UNSUPPORTED: Apache AGE uses the single-label AgensGraph proxy subset")
    label = effective[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label) is None:
        raise AdapterError(f"unsafe Apache AGE label: {label!r}")
    return label


def _uri_with_db(uri: str | None, database: str) -> str:
    if uri is None:
        return ""
    parsed = urlparse(uri)
    return urlunparse(parsed._replace(path="/" + database))
