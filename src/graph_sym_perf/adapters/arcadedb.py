"""ArcadeDB HTTP/OpenCypher adapter."""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.adapters.common_cypher import CypherLoaderMixin
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class ArcadeDBAdapter(CypherLoaderMixin, BaseAdapter):
    dependency_modules = ("requests",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._session = None
        self._base_url = None
        self._database = None

    def connectivity_check(self) -> tuple[bool, str]:
        try:
            self._connect()
            response = self._request("GET", "/api/v1/ready")
            if response.status_code >= 400:
                return False, response.text
            if self.allow_create:
                self.ensure_schema()
            self.execute("RETURN 1 AS ok", {}, "connectivity")
            return True, "connected"
        except Exception as exc:
            return False, str(exc)

    def ensure_schema(self) -> None:
        self._connect()
        exists = self._request("GET", f"/api/v1/exists/{self._database}")
        if exists.status_code == 200:
            try:
                payload = exists.json()
            except Exception:
                payload = None
            if isinstance(payload, Mapping) and bool(payload.get("result")):
                return
        response = self._request(
            "POST",
            "/api/v1/server",
            json={"command": f"create database {self._database}"},
        )
        if response.status_code not in (200, 201, 409):
            raise AdapterError(f"cannot create ArcadeDB database {self._database!r}: {response.text}")

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
            payload = self._command_payload(query) if _requires_command_endpoint(query) else self._query(query, parameters)
            rows = _arcade_rows(payload)
            if max_rows is not None and len(rows) > max_rows:
                truncated = True
                rows = rows[: max_rows + 1]
        except Exception as exc:
            raise QueryExecutionError(f"ArcadeDB query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.ARCADEDB,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "http",
                "statistics": _arcade_statistics(payload),
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            payload = self._query(f"EXPLAIN {query}", parameters)
            return make_plan_record(TargetSystem.ARCADEDB, query_kind, _arcade_plan_payload(payload))
        except Exception as exc:
            return unavailable_plan(TargetSystem.ARCADEDB, query_kind, str(exc))

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
            payload = self._query(f"PROFILE {query}", parameters)
            elapsed_ms = (perf_counter() - start) * 1000.0
            return ExecutionResult(
                system=TargetSystem.ARCADEDB,
                query_kind=query_kind,
                rows=_arcade_rows(payload),
                elapsed_ms=elapsed_ms,
                summary={
                    "client": "http",
                    "profile": _arcade_plan_payload(payload),
                    "statistics": _arcade_statistics(payload),
                    "truncated": False,
                    "row_cap": max_rows,
                    "timeout_seconds": timeout_seconds,
                    "profile_available": True,
                    "profile_command": "PROFILE",
                },
            )
        except Exception as exc:
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
                    "profile_command": "PROFILE",
                    "profile_error": str(exc),
                },
            )

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None

    def gdsmith_query_is_replayable(self, query: str) -> bool:
        upper = query.upper()
        if "MATCH" not in upper:
            return False
        if any(keyword in upper for keyword in (" CALL ", " EXISTS ", " UNION ")):
            return False
        if re.search(r"\[\s*\]", query):
            return False
        return True

    def _query(self, query: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/query/{self._database}",
            json={"language": "opencypher", "command": query, "params": dict(parameters)},
        )
        return self._payload_or_error(response)

    def _command(self, query: str, query_kind: str) -> ExecutionResult:
        start = perf_counter()
        payload = self._command_payload(query)
        return ExecutionResult(
            system=TargetSystem.ARCADEDB,
            query_kind=query_kind,
            rows=_arcade_rows(payload),
            elapsed_ms=(perf_counter() - start) * 1000.0,
            summary={"client": "http", "statistics": _arcade_statistics(payload)},
        )

    def _command_payload(self, query: str) -> Mapping[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/command/{self._database}",
            json={"language": "opencypher", "command": query},
        )
        return self._payload_or_error(response)

    def _connect(self) -> None:
        if self._session is not None:
            return
        try:
            import requests

            self._base_url = (self.env_value("GRAPHSYMPERF_ARCADEDB_HTTP_URL") or "").rstrip("/")
            self._database = self.env_value("GRAPHSYMPERF_ARCADEDB_DATABASE", "graph_sym_perf")
            username = self.env_value("GRAPHSYMPERF_ARCADEDB_USER", "root")
            password = self.env_value("GRAPHSYMPERF_ARCADEDB_PASSWORD", "")
            self._session = requests.Session()
            self._session.auth = (username, password)
            self._session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        except Exception as exc:
            raise AdapterError(f"cannot configure ArcadeDB HTTP client: {exc}") from exc

    def _request(self, method: str, path: str, **kwargs):
        self._connect()
        timeout = kwargs.pop("timeout", 30)
        return self._session.request(method, f"{self._base_url}{path}", timeout=timeout, **kwargs)

    def _payload_or_error(self, response) -> Mapping[str, Any]:
        if response.status_code >= 400:
            raise QueryExecutionError(f"HTTP {response.status_code}: {response.text}")
        try:
            payload = response.json()
        except Exception as exc:
            raise QueryExecutionError(f"ArcadeDB returned non-JSON response: {response.text}") from exc
        if isinstance(payload, Mapping) and payload.get("error"):
            raise QueryExecutionError(str(payload.get("error")))
        return payload if isinstance(payload, Mapping) else {"result": payload}


def _arcade_rows(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    result = payload.get("result", ())
    if result is None:
        return ()
    if isinstance(result, Mapping):
        return (to_jsonable(result),)
    if isinstance(result, list | tuple):
        rows: list[Mapping[str, Any]] = []
        for item in result:
            if isinstance(item, Mapping):
                rows.append(to_jsonable(item))
            elif isinstance(item, list | tuple):
                rows.append({f"col{index + 1}": to_jsonable(value) for index, value in enumerate(item)})
            else:
                rows.append({"value": to_jsonable(item)})
        return tuple(rows)
    return ({"value": to_jsonable(result)},)


def _arcade_statistics(payload: Mapping[str, Any]) -> Any:
    return to_jsonable(payload.get("statistics") or payload.get("stats") or payload.get("metrics"))


def _arcade_plan_payload(payload: Mapping[str, Any]) -> Any:
    for key in ("plan", "profile", "explain"):
        value = payload.get(key)
        if value:
            return to_jsonable(value)
    result = payload.get("result")
    if isinstance(result, list | tuple) and result:
        return tuple(_plan_line(item) for item in result)
    if isinstance(result, Mapping):
        return to_jsonable(result)
    return to_jsonable(payload)


def _plan_line(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return " | ".join(f"{key}: {value}" for key, value in item.items())
    return str(item)


def _create_node_cypher(labels: tuple[str, ...], properties: Mapping[str, Any]) -> str:
    label_text = "".join(f":{label}" for label in labels) or ":GdsmithNode"
    return f"CREATE (n{label_text} {_map_literal(properties)})"


def _map_literal(values: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{key}: {_literal(value)}" for key, value in values.items()) + "}"


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _requires_command_endpoint(query: str) -> bool:
    stripped = query.lstrip().upper()
    if stripped.startswith(("MERGE ", "CREATE ", "DELETE ", "DETACH ", "SET ", "REMOVE ", "DROP ")):
        return True
    return any(token in stripped for token in (" MERGE ", "\nMERGE ", " SET ", "\nSET ", " CREATE ", "\nCREATE "))
