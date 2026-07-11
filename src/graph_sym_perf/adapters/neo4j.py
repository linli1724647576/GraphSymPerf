"""Neo4j adapter using the official Python driver."""

from __future__ import annotations

import re
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter, normalized_uri_endpoint
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin, _query_execution_error
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryTimeoutError, TargetSystem, to_typed_value as to_jsonable
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class Neo4jAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("neo4j",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._driver = None
        self._database = None

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
            with self._session() as session:
                result = session.run(_query_with_timeout(query, timeout_seconds), dict(parameters))
                rows, summary, truncated = _collect_rows(result, max_rows)
        except Exception as exc:
            raise _query_execution_error(f"Neo4j query failed: {exc}", exc) from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary=_neo4j_summary(summary, truncated=truncated, row_cap=max_rows),
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        self._connect()
        try:
            with self._session() as session:
                result = session.run(f"EXPLAIN {query}", dict(parameters))
                summary = result.consume()
            return make_plan_record(TargetSystem.NEO4J, query_kind, _neo4j_plan(summary.plan))
        except Exception as exc:
            return unavailable_plan(TargetSystem.NEO4J, query_kind, str(exc))

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
            with self._session() as session:
                result = session.run(_query_with_timeout(f"PROFILE {query}", timeout_seconds), dict(parameters))
                rows, summary, truncated = _collect_rows(result, max_rows)
        except Exception as exc:
            classified = _query_execution_error(f"Neo4j profile failed: {exc}", exc)
            if isinstance(classified, QueryTimeoutError):
                raise classified from exc
            return self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.NEO4J,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary=_neo4j_summary(summary, truncated=truncated, row_cap=max_rows),
        )

    def schema_statements(self) -> tuple[str, ...]:
        return (
            "CREATE INDEX gbp_person_dataset IF NOT EXISTS FOR (n:Person) ON (n.gbp_dataset)",
            "CREATE INDEX gbp_post_dataset IF NOT EXISTS FOR (n:Post) ON (n.gbp_dataset)",
            "CREATE INDEX gbp_forum_dataset IF NOT EXISTS FOR (n:Forum) ON (n.gbp_dataset)",
            "CREATE INDEX gbp_tag_dataset IF NOT EXISTS FOR (n:Tag) ON (n.gbp_dataset)",
        )

    def reset_schema(self) -> None:
        indexes = self.execute(
            "SHOW INDEXES YIELD name RETURN name",
            {},
            "gdsmith_schema_inspect",
            max_rows=1_000,
        )
        for row in indexes.rows:
            name = row.get("name") if isinstance(row, Mapping) else None
            if isinstance(name, str) and _is_gdsmith_index_name(name):
                self.execute(
                    f"DROP INDEX `{name}` IF EXISTS",
                    {},
                    "gdsmith_schema_reset",
                )

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        return normalized_uri_endpoint(
            self.env_value("GRAPHSYMPERF_NEO4J_URI"),
            default_port=7687,
            database=self.optional_env_value("database"),
        )

    def _probe_health_epoch_identity(self) -> tuple[str, object] | None:
        self._connect()
        with self._session() as session:
            record = session.run(
                "CALL dbms.queryJmx('java.lang:type=Runtime') YIELD attributes "
                "RETURN attributes.StartTime.value AS start_time"
            ).single()
        value = record.get("start_time") if record is not None else None
        return ("jvm_runtime_start_time", value) if value not in (None, "") else None

    def _connect(self) -> None:
        if self._driver is not None:
            return
        try:
            from neo4j import GraphDatabase

            uri = self.env_value("GRAPHSYMPERF_NEO4J_URI")
            user = self.env_value("GRAPHSYMPERF_NEO4J_USER")
            password = self.env_value("GRAPHSYMPERF_NEO4J_PASSWORD")
            self._database = self.optional_env_value("database")
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            self._driver.verify_connectivity()
            info = self._driver.get_server_info()
            self._cache_server_version(getattr(info, "agent", None))
        except Exception as exc:
            raise AdapterError(f"cannot connect to Neo4j: {exc}") from exc

    def _session(self):
        if self._database:
            return self._driver.session(database=self._database)
        return self._driver.session()


def _query_with_timeout(query: str, timeout_seconds: float | None) -> Any:
    if timeout_seconds is None:
        return query
    from neo4j import Query

    return Query(query, timeout=timeout_seconds)


def _is_gdsmith_index_name(name: str) -> bool:
    return re.fullmatch(r"i\d+", name) is not None


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


def _neo4j_summary(summary: Any, *, truncated: bool = False, row_cap: int | None = None) -> dict[str, Any]:
    if summary is None:
        return {
            "server": None,
            "database": None,
            "result_available_after": None,
            "result_consumed_after": None,
            "plan": None,
            "profile": None,
            "truncated": truncated,
            "row_cap": row_cap,
        }
    return {
        "server": to_jsonable(getattr(summary, "server", None)),
        "database": to_jsonable(getattr(summary, "database", None)),
        "result_available_after": getattr(summary, "result_available_after", None),
        "result_consumed_after": getattr(summary, "result_consumed_after", None),
        "plan": _neo4j_plan(getattr(summary, "plan", None)),
        "profile": _neo4j_plan(getattr(summary, "profile", None)),
        "truncated": truncated,
        "row_cap": row_cap,
    }


def _neo4j_plan(plan: Any) -> Any:
    if plan is None:
        return None
    if isinstance(plan, dict):
        args = plan.get("args", {}) or {}
        return {
            "operator_type": plan.get("operatorType") or plan.get("operator_type"),
            "identifiers": tuple(plan.get("identifiers", ()) or ()),
            "arguments": to_jsonable(args),
            "string_representation": args.get("string-representation"),
            "children": tuple(_neo4j_plan(child) for child in (plan.get("children", ()) or ())),
        }
    arguments = to_jsonable(getattr(plan, "arguments", {}) or {})
    for key, value in _neo4j_profile_metrics(plan).items():
        arguments[key] = value
    return {
        "operator_type": getattr(plan, "operator_type", None),
        "identifiers": tuple(getattr(plan, "identifiers", ()) or ()),
        "arguments": arguments,
        "children": tuple(_neo4j_plan(child) for child in (getattr(plan, "children", None) or ())),
    }


def _neo4j_profile_metrics(plan: Any) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for attr, key in (
        ("db_hits", "DbHits"),
        ("records", "Rows"),
        ("page_cache_hits", "PageCacheHits"),
        ("page_cache_misses", "PageCacheMisses"),
        ("time", "Time"),
    ):
        value = getattr(plan, attr, None)
        if value is not None:
            metrics[key] = value
    return metrics
