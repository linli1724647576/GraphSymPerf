"""LadybugDB embedded database adapter.

LadybugDB is the active community fork of Kuzu (archived 2025-10),
maintained by Arun Sharma under the MIT license.  It shares the same
Python API (``Database`` / ``Connection`` / ``execute``) and openCypher
compatibility.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter, normalized_file_endpoint
from graph_sym_perf.adapters.common_cypher import _compact, _query_execution_error
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, TargetSystem, to_typed_value as to_jsonable
from graph_sym_perf.gdsmith.replay import GdsmithReplayGraph, parse_gdsmith_replay_graph
from graph_sym_perf.plan import make_plan_record, unavailable_plan

_logger = logging.getLogger(__name__)


class LadybugDBAdapter(BaseAdapter):
    """Adapter for the LadybugDB embedded graph database (pip install ladybug)."""

    dependency_modules = ("ladybug",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._db = None
        self._conn = None

    def connectivity_check(self) -> tuple[bool, str]:
        try:
            self._connect()
            self.execute("RETURN 1 AS ok", {}, "connectivity")
            return True, "connected"
        except Exception as exc:
            return False, str(exc)

    def ensure_schema(self) -> None:
        for query in _ladybugdb_schema_statements():
            self.execute(query, {}, "schema")

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> GdsmithReplayGraph:
        graph = parse_gdsmith_replay_graph(statements)
        self._ensure_gdsmith_schema(graph)
        node_label_by_id = {node.node_id: _primary_label(node.labels) for node in graph.nodes}
        for node in graph.nodes:
            self._create_gdsmith_node(_primary_label(node.labels), node.properties)
        for relationship in graph.relationships:
            start_label = node_label_by_id.get(relationship.start_id)
            end_label = node_label_by_id.get(relationship.end_id)
            if start_label is None or end_label is None:
                continue
            self._create_gdsmith_relationship(
                relationship.relationship_type,
                start_label,
                end_label,
                relationship.start_id,
                relationship.end_id,
                relationship.properties,
            )
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
        try:
            result = _call_with_timeout(
                lambda: self._conn.execute(query, dict(parameters)),
                timeout_seconds=timeout_seconds,
                label=f"LadybugDB {query_kind}",
            )
            rows, truncated = _ladybugdb_rows_bounded(result, max_rows=max_rows)
        except Exception as exc:
            raise _query_execution_error(f"LadybugDB query failed: {exc}", exc) from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.LADYBUGDB,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "ladybugdb",
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            result = self.execute(f"EXPLAIN {query}", parameters, f"{query_kind}_explain")
            return make_plan_record(TargetSystem.LADYBUGDB, query_kind, result.rows)
        except Exception as exc:
            return unavailable_plan(TargetSystem.LADYBUGDB, query_kind, str(exc))

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
            _logger.warning("LadybugDB profile failed for %s (%s), falling back to execute: %s", query_kind, type(exc).__name__, exc)
            return self.execute(query, parameters, query_kind, max_rows=max_rows, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._db = None

    def _fingerprint_endpoint_identity(self) -> Mapping[str, object]:
        return normalized_file_endpoint(self.env_value("GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH"))

    def _probe_health_epoch_identity(self) -> tuple[str, object] | None:
        self._connect()
        return ("embedded_host_process", os.getpid())

    # ── private helpers ──────────────────────────────────────────────

    def _connect(self) -> None:
        if self._conn is not None:
            return
        try:
            raw_path = self.env_value("GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH")
            if not raw_path:
                raise AdapterError("LadybugDB requires GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH")

            import ladybug

            path = Path(raw_path).expanduser()
            if not path.exists() and not self.allow_create:
                raise AdapterError(
                    "LadybugDB database path does not exist; pass --load "
                    "with GRAPHSYMPERF_ALLOW_LOAD=1 to create a dedicated test database"
                )
            self._db = ladybug.Database(str(path))
            self._conn = ladybug.Connection(self._db)
            self._cache_server_version(getattr(ladybug, "__version__", None))
        except Exception as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError(f"cannot connect to LadybugDB: {exc}") from exc

    def _ensure_gdsmith_schema(self, graph: GdsmithReplayGraph) -> None:
        # Single pass over nodes: build label-by-id map and property map together.
        node_props_by_label: dict[str, dict[str, str]] = {}
        node_label_by_id: dict[int, str] = {}
        for node in graph.nodes:
            label = _primary_label(node.labels)
            node_label_by_id[node.node_id] = label
            props = node_props_by_label.setdefault(label, {"id": "INT64"})
            for key, value in node.properties.items():
                props.setdefault(key, _ladybugdb_type(value))
        for label, props in node_props_by_label.items():
            fields = ["id INT64 PRIMARY KEY"]
            fields.extend(f"{key} {value}" for key, value in props.items() if key != "id")
            self.execute(f"CREATE NODE TABLE IF NOT EXISTS {label}({', '.join(fields)})", {}, "gdsmith_schema")

        rel_props_by_type: dict[str, dict[str, str]] = {}
        rel_pairs_by_type: dict[str, set[tuple[str, str]]] = {}
        for relationship in graph.relationships:
            start_label = node_label_by_id.get(relationship.start_id)
            end_label = node_label_by_id.get(relationship.end_id)
            if start_label is None or end_label is None:
                continue
            props = rel_props_by_type.setdefault(relationship.relationship_type, {})
            for key, value in relationship.properties.items():
                props.setdefault(key, _ladybugdb_type(value))
            pairs = rel_pairs_by_type.setdefault(relationship.relationship_type, set())
            pairs.add((start_label, end_label))
        for relationship_type, pairs in rel_pairs_by_type.items():
            pair_fields = [f"FROM {start} TO {end}" for start, end in pairs]
            prop_fields = [f"{key} {value}" for key, value in rel_props_by_type.get(relationship_type, {}).items()]
            self.execute(
                f"CREATE REL TABLE IF NOT EXISTS {relationship_type}({', '.join(pair_fields + prop_fields)})",
                {},
                "gdsmith_schema",
            )

    def _create_gdsmith_node(self, label: str, properties: Mapping[str, Any]) -> None:
        props = ", ".join(f"{key}: ${key}" for key in properties)
        self.execute(f"CREATE (n:{label} {{{props}}})", dict(properties), "gdsmith_load_node")

    def _create_gdsmith_relationship(
        self,
        relationship_type: str,
        start_label: str,
        end_label: str,
        start_id: int,
        end_id: int,
        properties: Mapping[str, Any],
    ) -> None:
        props = ", ".join(f"{key}: ${key}" for key in properties)
        query = f"""
MATCH (a:{start_label}), (b:{end_label})
WHERE a.id = $start_id AND b.id = $end_id
CREATE (a)-[r:{relationship_type} {{{props}}}]->(b)
"""
        parameters = dict(properties)
        parameters.update({"start_id": start_id, "end_id": end_id})
        self.execute(_compact(query), parameters, "gdsmith_load_relationship")


# ── module-level helpers ────────────────────────────────────────────


def _ladybugdb_schema_statements() -> tuple[str, ...]:
    return (
        "CREATE NODE TABLE IF NOT EXISTS Person(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, first_name STRING, last_name STRING, age INT64, city STRING, language STRING, creation_day INT64)",
        "CREATE NODE TABLE IF NOT EXISTS Post(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, content STRING, length INT64, creation_day INT64)",
        "CREATE NODE TABLE IF NOT EXISTS Forum(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, title STRING, creation_day INT64)",
        "CREATE NODE TABLE IF NOT EXISTS Tag(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, name STRING)",
        "CREATE NODE TABLE IF NOT EXISTS Place(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, name STRING, kind STRING)",
        "CREATE NODE TABLE IF NOT EXISTS Organisation(gbp_id STRING PRIMARY KEY, gbp_dataset STRING, name STRING, kind STRING)",
        "CREATE REL TABLE IF NOT EXISTS KNOWS(FROM Person TO Person, gbp_dataset STRING, since INT64)",
        "CREATE REL TABLE IF NOT EXISTS LIKES(FROM Person TO Post, gbp_dataset STRING, creation_day INT64)",
        "CREATE REL TABLE IF NOT EXISTS HAS_CREATOR(FROM Post TO Person, gbp_dataset STRING)",
        "CREATE REL TABLE IF NOT EXISTS HAS_MEMBER(FROM Forum TO Person, gbp_dataset STRING, join_day INT64)",
        "CREATE REL TABLE IF NOT EXISTS CONTAINER_OF(FROM Forum TO Post, gbp_dataset STRING)",
        "CREATE REL TABLE IF NOT EXISTS HAS_TAG(FROM Post TO Tag, gbp_dataset STRING)",
        "CREATE REL TABLE IF NOT EXISTS IS_LOCATED_IN(FROM Person TO Place, gbp_dataset STRING)",
        "CREATE REL TABLE IF NOT EXISTS WORKS_AT(FROM Person TO Organisation, gbp_dataset STRING, work_from INT64)",
    )


def _call_with_timeout(fn, *, timeout_seconds: float | None, label: str = "query") -> Any:
    """Run *fn* in a thread, raising TimeoutError if it exceeds *timeout_seconds*."""
    if timeout_seconds is None or timeout_seconds <= 0:
        return fn()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ladybugdb-timeout") as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            _logger.warning("%s exceeded timeout %.3fs", label, timeout_seconds)
            raise TimeoutError(f"{label} exceeded timeout {timeout_seconds:.3f}s") from None


def _ladybugdb_rows(result: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(result, list):
        if not result:
            return ()
        result = result[-1]
    if result is None:
        return ()
    try:
        rows = result.rows_as_dict().get_all()
    except Exception as exc:
        _logger.debug("LadybugDB rows_as_dict() failed (%s), trying get_all()", exc)
        try:
            rows = result.get_all()
        except Exception as exc2:
            _logger.warning("LadybugDB row extraction failed: get_all() error: %s", exc2)
            return ()
    if isinstance(rows, dict):
        keys = list(rows)
        length = len(next(iter(rows.values()), []))
        return tuple({key: to_jsonable(rows[key][index]) for key in keys} for index in range(length))
    if rows and isinstance(rows[0], dict):
        return tuple(to_jsonable(row) for row in rows)
    return tuple({"row": to_jsonable(row)} for row in rows)


def _ladybugdb_rows_bounded(
    result: Any,
    *,
    max_rows: int | None,
) -> tuple[tuple[Mapping[str, Any], ...], bool]:
    if max_rows is None:
        return _ladybugdb_rows(result), False
    if isinstance(result, list):
        result = result[-1] if result else None
    if result is None:
        return (), False
    has_next = getattr(result, "has_next", None)
    get_next = getattr(result, "get_next", None)
    get_names = getattr(result, "get_column_names", None)
    if not callable(has_next) or not callable(get_next) or not callable(get_names):
        raise RuntimeError("LadybugDB bounded row streaming is unavailable")
    names = tuple(str(name) for name in get_names())
    rows: list[Mapping[str, Any]] = []
    while len(rows) <= max_rows and has_next():
        raw = get_next()
        if isinstance(raw, Mapping):
            rows.append(to_jsonable(raw))
        elif isinstance(raw, list | tuple):
            rows.append({names[index] if index < len(names) else f"col{index + 1}": to_jsonable(value) for index, value in enumerate(raw)})
        else:
            rows.append({"row": to_jsonable(raw)})
    return tuple(rows), len(rows) > max_rows


def _primary_label(labels: tuple[str, ...]) -> str:
    return labels[0] if labels else "GdsmithNode"


def _ladybugdb_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"
