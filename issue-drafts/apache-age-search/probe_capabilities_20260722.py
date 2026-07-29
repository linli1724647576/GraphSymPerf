#!/usr/bin/env python3
"""Probe Apache AGE Cypher surfaces before rule-family screening."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import psycopg


GRAPH = "age_capabilities"


PROBES = {
    "inline_property": "MATCH (n:N {id: 3}) RETURN count(*) AS value",
    "where_property": "MATCH (n:N) WHERE n.id = 3 RETURN count(*) AS value",
    "singleton_in": "MATCH (n:N) WHERE n.grp IN [3] RETURN count(*) AS value",
    "or_to_in": "MATCH (n:N) WHERE n.grp = 2 OR n.grp = 3 RETURN count(*) AS value",
    "arithmetic_identity": "MATCH (n:N) WHERE n.grp + 0 = 3 RETURN count(*) AS value",
    "with_passthrough": "MATCH (n:N) WITH n RETURN count(*) AS value",
    "with_distinct": "MATCH (n:N) WITH DISTINCT n.grp AS g RETURN count(*) AS value",
    "union_all": (
        "MATCH (n:N) WHERE n.grp < 3 RETURN count(*) AS value "
        "UNION ALL MATCH (n:N) WHERE n.grp >= 3 RETURN count(*) AS value"
    ),
    "undirected_relationship": "MATCH (:A)-[r:R]-(:B) RETURN count(*) AS value",
    "directed_union": (
        "MATCH (:A)-[:R]->(:B) RETURN count(*) AS value "
        "UNION ALL MATCH (:A)<-[:R]-(:B) RETURN count(*) AS value"
    ),
    "chain_pattern": "MATCH (:A)-[:R]->(:B)-[:S]->(:C) RETURN count(*) AS value",
    "chain_split": (
        "MATCH (a:A)-[:R]->(b:B) MATCH (b)-[:S]->(c:C) "
        "RETURN count(*) AS value"
    ),
    "case_expression": (
        "MATCH (n:N) RETURN sum(CASE WHEN n.grp < 3 THEN 1 ELSE 0 END) AS value"
    ),
    "coalesce": "MATCH (n:N) RETURN sum(coalesce(n.missing, 1)) AS value",
    "minimum": "MATCH (n:N) RETURN min(n.id) AS value",
    "sort_limit": "MATCH (n:N) RETURN n.id AS value ORDER BY value LIMIT 1",
    "count_relationship": "MATCH (:A)-[r:R]->(:B) RETURN count(r) AS value",
    "sum_one": "MATCH (n:N) RETURN sum(1) AS value",
    "relationship_reversal": "MATCH (b:B)<-[:R]-(a:A) RETURN count(*) AS value",
    "optional_match": (
        "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m) RETURN count(*) AS value"
    ),
    "is_not_null": "MATCH (n:N) WHERE n.id IS NOT NULL RETURN count(*) AS value",
    "exists_property": "MATCH (n:N) WHERE exists(n.id) RETURN count(*) AS value",
    "relationship_type": (
        "MATCH (:A)-[r]->(:B) WHERE type(r) = 'R' RETURN count(*) AS value"
    ),
    "label_function": "MATCH (n) WHERE label(n) = 'N' RETURN count(*) AS value",
}


def sql_for(query: str) -> str:
    tag = "$age_probe$"
    return (
        f"SELECT * FROM ag_catalog.cypher('{GRAPH}', {tag}{query}{tag}) "
        'AS ("value" agtype)'
    )


def run(cursor: Any, query: str) -> tuple[list[list[str]], float]:
    started = time.perf_counter()
    cursor.execute(sql_for(query))
    rows = [[str(value) for value in row] for row in cursor.fetchall()]
    return rows, (time.perf_counter() - started) * 1000.0


def setup(cursor: Any) -> None:
    cursor.execute(
        "SELECT ag_catalog.drop_graph(%s, true) "
        "WHERE EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s)",
        (GRAPH, GRAPH),
    )
    cursor.execute("SELECT ag_catalog.create_graph(%s)", (GRAPH,))
    run(
        cursor,
        "UNWIND range(1, 100) AS i "
        "CREATE (:N {id: i, grp: i % 7}) RETURN count(*) AS value",
    )
    run(
        cursor,
        "UNWIND range(1, 30) AS i "
        "CREATE (:A {id: i})-[:R {id: i}]->(:B {id: i}) "
        "RETURN count(*) AS value",
    )
    run(
        cursor,
        "UNWIND range(1, 20) AS i "
        "CREATE (:A {id: 1000 + i})-[:R {id: 1000 + i}]->"
        "(:B {id: 1000 + i})-[:S {id: 1000 + i}]->(:C {id: i}) "
        "RETURN count(*) AS value",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("AGE_PASSWORD"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set AGE_PASSWORD or pass --password")

    records: list[dict[str, Any]] = []
    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=args.password,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("LOAD 'age'")
            cursor.execute('SET search_path = ag_catalog, "$user", public')
            cursor.execute("SELECT version()")
            postgres_version = cursor.fetchone()[0]
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'age'")
            age_version = cursor.fetchone()[0]
            setup(cursor)
            for probe_id, query in PROBES.items():
                record: dict[str, Any] = {
                    "probe_id": probe_id,
                    "query": query,
                    "postgres_version": postgres_version,
                    "age_version": age_version,
                }
                try:
                    rows, elapsed_ms = run(cursor, query)
                    record.update(status="supported", rows=rows, elapsed_ms=elapsed_ms)
                except Exception as exc:
                    record.update(status="unsupported", error=str(exc))
                records.append(record)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "supported": sum(record["status"] == "supported" for record in records),
                "total": len(records),
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
