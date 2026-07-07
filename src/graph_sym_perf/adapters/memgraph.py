"""Memgraph adapter using the Neo4j Bolt driver."""

from __future__ import annotations

from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin, _has_bare_property_predicate, _has_boolean_property_operand, _is_timeout_like
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class MemgraphAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("neo4j",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._driver = None

    def connectivity_check(self) -> tuple[bool, str]:
        try:
            self._connect()
            self.execute("RETURN 1 AS ok", {}, "connectivity")
            return True, "connected"
        except Exception as exc:
            return False, str(exc)

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
        try:
            with self._driver.session() as session:
                result = session.run(_query_with_timeout(query, timeout_seconds), dict(parameters))
                rows, summary, truncated = _collect_rows(result, max_rows)
        except Exception as exc:
            raise QueryExecutionError(f"Memgraph query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.MEMGRAPH,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "neo4j",
                "summary": _summary(summary),
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            result = self.execute(f"EXPLAIN {query}", parameters, f"{query_kind}_explain")
            return make_plan_record(TargetSystem.MEMGRAPH, query_kind, result.rows)
        except Exception as exc:
            return unavailable_plan(TargetSystem.MEMGRAPH, query_kind, str(exc))

    def profile(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        try:
            return self.execute(
                f"PROFILE {query}",
                parameters,
                query_kind,
                max_rows=max_rows,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            if _is_timeout_like(exc):
                raise
            return self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)

    def schema_statements(self) -> tuple[str, ...]:
        return (
            "CREATE INDEX ON :Person(gbp_dataset)",
            "CREATE INDEX ON :Post(gbp_dataset)",
            "CREATE INDEX ON :Forum(gbp_dataset)",
            "CREATE INDEX ON :Tag(gbp_dataset)",
        )

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
        self._driver = None

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        upper = query.upper()
        if " STARTS WITH " in upper or " ENDS WITH " in upper:
            return False
        if _has_bare_property_predicate(query) or _has_boolean_property_operand(query):
            return False
        return True

    def _connect(self) -> None:
        if self._driver is not None:
            return
        try:
            from neo4j import GraphDatabase

            host = self.env_value("GRAPHSYMPERF_MEMGRAPH_HOST")
            port = int(self.env_value("GRAPHSYMPERF_MEMGRAPH_PORT", "7687"))
            username = self.optional_env_value("username", "")
            password = self.optional_env_value("password", "")
            auth = (username, password) if username or password else None
            self._driver = GraphDatabase.driver(f"bolt://{host}:{port}", auth=auth)
            self._driver.verify_connectivity()
        except Exception as exc:
            raise AdapterError(f"cannot connect to Memgraph: {exc}") from exc


def _query_with_timeout(query: str, timeout_seconds: float | None) -> Any:
    if timeout_seconds is None:
        return query
    from neo4j import Query

    return Query(query, timeout=timeout_seconds)


def _collect_rows(result: Any, max_rows: int | None) -> tuple[tuple[Mapping[str, Any], ...], Any, bool]:
    rows: list[Mapping[str, Any]] = []
    truncated = False
    limit = max_rows + 1 if max_rows is not None else None
    for record in result:
        rows.append(to_jsonable(record.data()))
        if limit is not None and len(rows) >= limit:
            truncated = True
            break
    summary = None if truncated else result.consume()
    return tuple(rows), summary, truncated


def _summary(summary: Any) -> Mapping[str, Any]:
    if summary is None:
        return {}
    return {
        "server": to_jsonable(getattr(summary, "server", None)),
        "database": to_jsonable(getattr(summary, "database", None)),
        "result_available_after": getattr(summary, "result_available_after", None),
        "result_consumed_after": getattr(summary, "result_consumed_after", None),
    }
