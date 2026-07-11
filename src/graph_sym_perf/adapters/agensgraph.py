"""AgensGraph adapter over PostgreSQL."""

from __future__ import annotations

import re
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter, normalized_uri_endpoint
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin, _query_execution_error
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, TargetSystem, to_typed_value as to_jsonable
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.base import DecisionKind
from graph_sym_perf.generation.corpus import PortableQuery
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

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        for node in graph.nodes:
            self._single_node_label(node)
        self.ensure_schema()
        self._ensure_labels(
            vertex_labels=tuple(sorted({self._single_node_label(node) for node in graph.nodes})),
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
        cypher = self._compile(query, parameters)
        truncated = False
        try:
            with self._conn.cursor() as cursor:
                if timeout_seconds is not None:
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                self._set_graph_path(cursor)
                cursor.execute(cypher)
                aliases = tuple(item.name for item in (cursor.description or ()))
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
            raise _query_execution_error(f"AgensGraph query failed: {exc}", exc) from exc
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
            cypher = self._compile(query, parameters)
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
        cypher = self._compile(query, parameters)
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

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        endpoint = dict(
            normalized_uri_endpoint(
                self.env_value("GRAPHSYMPERF_AGENSGRAPH_URI"),
                default_port=5432,
                database=self.optional_env_value("database"),
            )
        )
        endpoint["graph"] = self.env_value("GRAPHSYMPERF_AGENSGRAPH_GRAPH", "graph_sym_perf")
        return endpoint

    def _probe_health_epoch_identity(self) -> tuple[str, object] | None:
        self._connect()
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT pg_postmaster_start_time()")
            row = cursor.fetchone()
        return ("pg_postmaster_start_time", row[0]) if row and row[0] not in (None, "") else None

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        return AgensGraphDialect().check(PortableQuery(query)).kind is not DecisionKind.REJECT

    def graph_identifier_normalizer(self, value: str) -> str:
        """AgensGraph folds unquoted PostgreSQL identifiers to lowercase."""

        return value.lower()

    def _merge_gdsmith_node(self, node: GdsmithReplayNode) -> None:
        label = self._single_node_label(node)
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

    def _single_node_label(self, node: GdsmithReplayNode) -> str:
        labels = node.labels or ("GdsmithNode",)
        if len(labels) != 1:
            raise AdapterError("MULTI_LABEL_UNSUPPORTED: AgensGraph vertices require exactly one label")
        return _identifier(labels[0])

    def _compile(self, query: str, parameters: Mapping[str, Any]) -> str:
        decision = AgensGraphDialect().check(PortableQuery(query, parameters=parameters))
        if decision.kind is DecisionKind.REJECT or decision.compiled_query is None:
            raise AdapterError(f"AgensGraph dialect rejected query: {decision.reason_code.value}")
        return decision.compiled_query.cypher_core

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
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT version()")
                row = cursor.fetchone()
            self._cache_server_version(row[0] if row else None)
        except Exception as exc:
            raise AdapterError(f"cannot connect to AgensGraph: {exc}") from exc

    def _set_graph_path(self, cursor: Any) -> None:
        cursor.execute(f"SET graph_path = {_identifier(self._graph_name)}")


def _identifier(value: str | None) -> str:
    if not value or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
        raise AdapterError(f"unsafe AgensGraph graph name: {value!r}")
    return value


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())


def _uri_with_db(uri: str | None, database: str) -> str:
    if uri is None or "/" in uri.rsplit("@", 1)[-1]:
        return uri or ""
    return uri.rstrip("/") + "/" + database
