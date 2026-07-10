"""FalkorDB adapter using the Python FalkorDB client."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin, _has_bare_property_predicate, _has_boolean_property_operand, _query_execution_error
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, TargetSystem, to_jsonable
from graph_sym_perf.plan import make_plan_record, unavailable_plan

_logger = logging.getLogger(__name__)


class FalkorDBAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("falkordb",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._db = None
        self._graph = None

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
            raw_result = _call_with_timeout(
                lambda: _run_graph_query(self._graph, query, parameters),
                timeout_seconds=timeout_seconds,
                label=f"FalkorDB {query_kind}",
            )
            rows = _falkor_rows(raw_result)
            if max_rows is not None and len(rows) > max_rows:
                truncated = True
                rows = rows[: max_rows + 1]
        except Exception as exc:
            raise _query_execution_error(f"FalkorDB query failed: {exc}", exc) from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.FALKORDB,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "falkordb",
                "statistics": _falkor_statistics(raw_result),
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            self._connect()
            explained_query = _query_with_params(query, parameters)
            explain = getattr(self._graph, "explain", None)
            if callable(explain):
                raw_plan = _falkor_plan_payload(explain(explained_query))
            else:
                raw_plan = _falkor_plan_payload(_run_graph_query(self._graph, f"EXPLAIN {explained_query}", {}))
            return make_plan_record(TargetSystem.FALKORDB, query_kind, raw_plan)
        except Exception as exc:
            return unavailable_plan(TargetSystem.FALKORDB, query_kind, str(exc))

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
            self._connect()
            start = perf_counter()
            profiled_query = _query_with_params(query, parameters)
            profile = getattr(self._graph, "profile", None)
            if callable(profile):
                raw_profile = _call_with_timeout(
                    lambda: profile(profiled_query),
                    timeout_seconds=timeout_seconds,
                    label=f"FalkorDB profile {query_kind}",
                )
            else:
                raw_profile = _call_with_timeout(
                    lambda: _run_graph_query(self._graph, f"PROFILE {profiled_query}", {}),
                    timeout_seconds=timeout_seconds,
                    label=f"FalkorDB profile {query_kind}",
                )
            profile_payload = _falkor_plan_payload(raw_profile)
            elapsed_ms = (perf_counter() - start) * 1000.0
            return ExecutionResult(
                system=TargetSystem.FALKORDB,
                query_kind=query_kind,
                rows=_falkor_rows(raw_profile),
                elapsed_ms=elapsed_ms,
                summary={
                    "client": "falkordb",
                    "profile": profile_payload,
                    "statistics": _falkor_statistics(raw_profile),
                    "truncated": False,
                    "row_cap": max_rows,
                    "timeout_seconds": timeout_seconds,
                },
            )
        except Exception as exc:
            _logger.warning("FalkorDB profile failed for %s (%s), falling back to execute: %s", query_kind, type(exc).__name__, exc)
            return self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)

    def schema_statements(self) -> tuple[str, ...]:
        return (
            "CREATE INDEX ON :Person(gbp_dataset)",
            "CREATE INDEX ON :Post(gbp_dataset)",
            "CREATE INDEX ON :Forum(gbp_dataset)",
            "CREATE INDEX ON :Tag(gbp_dataset)",
        )

    def close(self) -> None:
        self._graph = None
        self._db = None

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        return {
            "host": (self.env_value("GRAPHSYMPERF_FALKORDB_HOST", "127.0.0.1") or "").lower(),
            "port": int(self.env_value("GRAPHSYMPERF_FALKORDB_PORT", "6379")),
            "graph": self.env_value("GRAPHSYMPERF_FALKORDB_GRAPH", "graph_sym_perf"),
        }

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        upper = query.upper()
        if " STARTS WITH " in upper or " ENDS WITH " in upper:
            return False
        if _has_bare_property_predicate(query) or _has_boolean_property_operand(query):
            return False
        return True

    def _connect(self) -> None:
        if self._graph is not None:
            return
        try:
            from falkordb import FalkorDB

            host = self.env_value("GRAPHSYMPERF_FALKORDB_HOST", "127.0.0.1")
            port = int(self.env_value("GRAPHSYMPERF_FALKORDB_PORT", "6379"))
            graph_name = self.env_value("GRAPHSYMPERF_FALKORDB_GRAPH", "graph_sym_perf")
            username = self.optional_env_value("username")
            password = self.optional_env_value("password")
            kwargs: dict[str, Any] = {"host": host, "port": port}
            if username:
                kwargs["username"] = username
            if password:
                kwargs["password"] = password
            self._db = FalkorDB(**kwargs)
            self._graph = self._db.select_graph(graph_name)
            self._cache_server_version(_falkordb_server_version(self._db))
        except Exception as exc:
            raise AdapterError(f"cannot connect to FalkorDB: {exc}") from exc


def _call_with_timeout(fn, *, timeout_seconds: float | None, label: str = "query") -> Any:
    """Run *fn* in a thread, raising TimeoutError if it exceeds *timeout_seconds*."""
    if timeout_seconds is None or timeout_seconds <= 0:
        return fn()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="falkordb-timeout") as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            _logger.warning("%s exceeded timeout %.3fs", label, timeout_seconds)
            raise TimeoutError(f"{label} exceeded timeout {timeout_seconds:.3f}s") from None


def _run_graph_query(graph: Any, query: str, parameters: Mapping[str, Any]) -> Any:
    try:
        return graph.query(query, dict(parameters))
    except TypeError:
        return graph.query(_query_with_params(query, parameters))


def _falkordb_server_version(database: Any) -> str | None:
    try:
        modules = database.connection.execute_command("MODULE", "LIST")
    except Exception:
        return None
    for raw_module in modules or ():
        if isinstance(raw_module, Mapping):
            module = {str(key).lower(): value for key, value in raw_module.items()}
        elif isinstance(raw_module, list | tuple):
            module = {str(raw_module[index]).lower(): raw_module[index + 1] for index in range(0, len(raw_module) - 1, 2)}
        else:
            continue
        name = str(module.get("name") or module.get("b'name'") or "").lower()
        if "graph" in name or "falkor" in name:
            return str(module.get("ver") or module.get("version") or module.get("b'ver'") or "") or None
    return None


def _query_with_params(query: str, parameters: Mapping[str, Any]) -> str:
    if not parameters:
        return query
    assignments = " ".join(f"{name}={_literal(value)}" for name, value in sorted(parameters.items()))
    return f"CYPHER {assignments} {query}"


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, Mapping):
        items = ", ".join(f"{key}: {_literal(item)}" for key, item in value.items())
        return "{" + items + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _falkor_rows(result: Any) -> tuple[Mapping[str, Any], ...]:
    if result is None:
        return ()
    if isinstance(result, list | tuple):
        if result and all(isinstance(item, str) for item in result):
            return tuple({"operator": item} for item in result)
        return tuple(_row_mapping(item) for item in result)
    result_set = getattr(result, "result_set", None)
    if result_set is None:
        return ({"row": to_jsonable(result)},)
    headers = _falkor_headers(result)
    rows: list[Mapping[str, Any]] = []
    for item in result_set:
        if isinstance(item, Mapping):
            rows.append(to_jsonable(item))
        elif headers and isinstance(item, list | tuple):
            rows.append({headers[index]: to_jsonable(value) for index, value in enumerate(item)})
        else:
            rows.append({"row": to_jsonable(item)})
    return tuple(rows)


def _row_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return to_jsonable(item)
    if isinstance(item, list | tuple):
        return {"row": tuple(to_jsonable(value) for value in item)}
    return {"row": to_jsonable(item)}


def _falkor_headers(result: Any) -> tuple[str, ...]:
    header = getattr(result, "header", None)
    if not header:
        return ()
    names: list[str] = []
    for item in header:
        name = getattr(item, "name", None) or getattr(item, "property", None) or str(item)
        names.append(_clean_header_name(name))
    return tuple(names)


def _clean_header_name(value: str) -> str:
    match = re.search(r"'([^']+)'", value)
    return match.group(1) if match else value


def _falkor_statistics(result: Any) -> Any:
    for attr in ("statistics", "stats"):
        value = getattr(result, attr, None)
        if value is not None:
            return to_jsonable(value)
    return None


def _falkor_plan_payload(result: Any) -> Any:
    plan = getattr(result, "plan", None)
    if plan is not None:
        return tuple(str(line) for line in plan)
    structured = getattr(result, "structured_plan", None)
    if structured is not None:
        return str(structured)
    return to_jsonable(result)
