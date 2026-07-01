"""Kuzu embedded database adapter."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from graph_sym_perf.adapters.base import BaseAdapter
from graph_sym_perf.core import AdapterError, ExecutionResult, PlanRecord, QueryExecutionError, TargetSystem, to_jsonable
from graph_sym_perf.datasets import MiniLdbcGraph, MiniLdbcNode, MiniLdbcRelationship
from graph_sym_perf.plan import make_plan_record, unavailable_plan


class KuzuAdapter(BaseAdapter):
    dependency_modules = ("kuzu",)

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
        for query in _kuzu_schema_statements():
            self.execute(query, {}, "schema")

    def load_mini_ldbc(self, graph: MiniLdbcGraph) -> None:
        if self._dataset_present(graph.dataset_id):
            return
        for node in graph.nodes:
            self._create_node(node)
        for relationship in graph.relationships:
            self._create_relationship(relationship)

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
            result = self._conn.execute(query, dict(parameters))
            rows = _kuzu_rows(result)
            if max_rows is not None and len(rows) > max_rows:
                truncated = True
                rows = rows[: max_rows + 1]
        except Exception as exc:
            raise QueryExecutionError(f"Kuzu query failed: {exc}") from exc
        elapsed_ms = (perf_counter() - start) * 1000.0
        return ExecutionResult(
            system=TargetSystem.KUZU,
            query_kind=query_kind,
            rows=rows,
            elapsed_ms=elapsed_ms,
            summary={
                "client": "kuzu",
                "truncated": truncated,
                "row_cap": max_rows,
                "timeout_seconds": timeout_seconds,
            },
        )

    def explain(self, query: str, parameters: Mapping[str, Any], query_kind: str) -> PlanRecord:
        try:
            result = self.execute(f"EXPLAIN {query}", parameters, f"{query_kind}_explain")
            return make_plan_record(TargetSystem.KUZU, query_kind, result.rows)
        except Exception as exc:
            return unavailable_plan(TargetSystem.KUZU, query_kind, str(exc))

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

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._db = None

    def _connect(self) -> None:
        if self._conn is not None:
            return
        try:
            import kuzu

            path = Path(self.env_value("GRAPHSYMPERF_KUZU_DATABASE_PATH") or "")
            if not path.exists() and not self.allow_create:
                raise AdapterError(
                    "Kuzu database path does not exist; pass --load with GRAPHSYMPERF_ALLOW_LOAD=1 "
                    "to create a dedicated test database"
                )
            self._db = kuzu.Database(str(path))
            self._conn = kuzu.Connection(self._db)
        except Exception as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError(f"cannot connect to Kuzu: {exc}") from exc

    def _dataset_present(self, dataset_id: str) -> bool:
        try:
            result = self.execute(
                "MATCH (p:Person) WHERE p.gbp_dataset = $dataset_id RETURN count(*) AS count",
                {"dataset_id": dataset_id},
                "dataset_check",
            )
            if not result.rows:
                return False
            count = next(iter(result.rows[0].values()))
            return int(count) > 0
        except Exception:
            return False

    def _create_node(self, node: MiniLdbcNode) -> None:
        props = ", ".join(f"{key}: ${key}" for key in node.properties)
        self.execute(f"CREATE (n:{node.label} {{{props}}})", dict(node.properties), "load_node")

    def _create_relationship(self, relationship: MiniLdbcRelationship) -> None:
        props = ", ".join(f"{key}: ${key}" for key in relationship.properties)
        query = f"""
MATCH (a:{relationship.start_label}), (b:{relationship.end_label})
WHERE a.gbp_id = $start_id AND b.gbp_id = $end_id AND a.gbp_dataset = $dataset_id AND b.gbp_dataset = $dataset_id
CREATE (a)-[r:{relationship.relationship_type} {{{props}}}]->(b)
"""
        parameters = dict(relationship.properties)
        parameters.update(
            {
                "start_id": relationship.start_id,
                "end_id": relationship.end_id,
                "dataset_id": relationship.properties["gbp_dataset"],
            }
        )
        self.execute(_compact(query), parameters, "load_relationship")


def _kuzu_schema_statements() -> tuple[str, ...]:
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


def _kuzu_rows(result: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(result, list):
        if not result:
            return ()
        result = result[-1]
    if result is None:
        return ()
    try:
        rows = result.rows_as_dict().get_all()
    except Exception:
        try:
            rows = result.get_all()
        except Exception:
            return ()
    if isinstance(rows, dict):
        keys = list(rows)
        length = len(next(iter(rows.values()), []))
        return tuple({key: to_jsonable(rows[key][index]) for key in keys} for index in range(length))
    if rows and isinstance(rows[0], dict):
        return tuple(to_jsonable(row) for row in rows)
    return tuple({"row": to_jsonable(row)} for row in rows)


def _compact(query: str) -> str:
    return "\n".join(line.rstrip() for line in query.strip().splitlines())
