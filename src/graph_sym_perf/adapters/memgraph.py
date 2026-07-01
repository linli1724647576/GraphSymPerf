"""Memgraph adapter using GQLAlchemy."""

from __future__ import annotations

from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class MemgraphAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("gqlalchemy",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._client = None

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
        truncated = False
        try:
            rows_list = []
            limit = max_rows + 1 if max_rows is not None else None
            for row in self._client.execute_and_fetch(query, dict(parameters)):
                rows_list.append(to_jsonable(row))
                if limit is not None and len(rows_list) >= limit:
                    truncated = True
                    break
            rows = tuple(rows_list)
        except Exception as exc:
            raise QueryExecutionError(f"Memgraph query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.MEMGRAPH,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "gqlalchemy",
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
        except Exception:
            return self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)

    def schema_statements(self) -> tuple[str, ...]:
        return (
            "CREATE INDEX ON :Person(gbp_dataset)",
            "CREATE INDEX ON :Post(gbp_dataset)",
            "CREATE INDEX ON :Forum(gbp_dataset)",
            "CREATE INDEX ON :Tag(gbp_dataset)",
        )

    def close(self) -> None:
        self._client = None

    def _connect(self) -> None:
        if self._client is not None:
            return
        try:
            from gqlalchemy import Memgraph

            host = self.env_value("GRAPHSYMPERF_MEMGRAPH_HOST")
            port = int(self.env_value("GRAPHSYMPERF_MEMGRAPH_PORT", "7687"))
            username = self.optional_env_value("username", "")
            password = self.optional_env_value("password", "")
            kwargs = {"host": host, "port": port}
            if username or password:
                kwargs.update({"username": username, "password": password})
            self._client = Memgraph(**kwargs)
        except Exception as exc:
            raise AdapterError(f"cannot connect to Memgraph: {exc}") from exc
