"""AgensGraph adapter over PostgreSQL."""

from __future__ import annotations

import re
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
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

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> None:
        self.ensure_schema()
        for statement in statements:
            self.execute(statement, {}, "gdsmith_load")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

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
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _identifier(value: str | None) -> str:
    if not value or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
        raise AdapterError(f"unsafe AgensGraph graph name: {value!r}")
    return value


def _uri_with_db(uri: str | None, database: str) -> str:
    if uri is None or "/" in uri.rsplit("@", 1)[-1]:
        return uri or ""
    return uri.rstrip("/") + "/" + database
