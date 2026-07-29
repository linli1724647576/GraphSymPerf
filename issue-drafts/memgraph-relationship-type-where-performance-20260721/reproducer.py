#!/usr/bin/env python3
"""Reproduce the WHERE type(r) relationship-expansion slowdown."""

from __future__ import annotations

import argparse
import random
import statistics
import time
from typing import Any

from neo4j import GraphDatabase, Query


QUERY_A = "MATCH (a:A)-[r:LINK]->(b:B) RETURN count(r) AS c"
QUERY_B = (
    "MATCH (a:A)-[r]->(b:B) "
    "WHERE type(r) = 'LINK' "
    "RETURN count(r) AS c"
)
ROWS_A = (
    "MATCH (a:A)-[r:LINK]->(b:B) "
    "RETURN a.id AS a, b.id AS b, type(r) AS t ORDER BY a, b, t"
)
ROWS_B = (
    "MATCH (a:A)-[r]->(b:B) "
    "WHERE type(r) = 'LINK' "
    "RETURN a.id AS a, b.id AS b, type(r) AS t ORDER BY a, b, t"
)


def run(session: Any, text: str, parameters: dict[str, Any] | None = None):
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000


def setup(session: Any, nodes: int, noise_per_node: int) -> None:
    run(session, "MATCH (n) DETACH DELETE n")
    for statement in ("DROP INDEX ON :A(id)", "DROP INDEX ON :B(id)"):
        try:
            run(session, statement)
        except Exception:
            pass
    run(session, "CREATE INDEX ON :A(id)")
    run(session, "CREATE INDEX ON :B(id)")
    for label in ("A", "B"):
        for start in range(0, nodes, 5_000):
            end = min(nodes, start + 5_000)
            run(
                session,
                f"UNWIND range($start, $end - 1) AS i CREATE (:{label} {{id: i}})",
                {"start": start, "end": end},
            )
    for start in range(0, nodes, 1_000):
        end = min(nodes, start + 1_000)
        run(
            session,
            """
            MATCH (a:A)
            WHERE a.id >= $start AND a.id < $end
            MATCH (b:B {id: a.id})
            CREATE (a)-[:LINK]->(b)
            """,
            {"start": start, "end": end},
        )
        run(
            session,
            """
            MATCH (a:A)
            WHERE a.id >= $start AND a.id < $end
            UNWIND range(1, $noise) AS offset
            MATCH (b:B {id: (a.id + offset) % $nodes})
            CREATE (a)-[:NOISE]->(b)
            """,
            {
                "start": start,
                "end": end,
                "noise": noise_per_node,
                "nodes": nodes,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--noise-per-node", type=int, default=50)
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            setup(session, args.nodes, args.noise_per_node)
            rows_a, _ = run(session, ROWS_A)
            rows_b, _ = run(session, ROWS_B)
            if rows_a != rows_b:
                raise RuntimeError("the two queries returned different relationships")

            for _ in range(3):
                run(session, QUERY_A)
                run(session, QUERY_B)

            orders = ["AB", "BA"] * args.repeats
            random.Random(20260721).shuffle(orders)
            a_times: list[float] = []
            b_times: list[float] = []
            for order in orders:
                for side in order:
                    _, elapsed = run(session, QUERY_A if side == "A" else QUERY_B)
                    (a_times if side == "A" else b_times).append(elapsed)

            plan_a, _ = run(session, f"EXPLAIN {QUERY_A}")
            plan_b, _ = run(session, f"EXPLAIN {QUERY_B}")

        print(f"Memgraph version: {version[0]['version']}")
        print(f"Matching relationships equal: {rows_a == rows_b} ({len(rows_a)} rows)")
        print(f"Query A: {statistics.median(a_times):.3f} ms")
        print(f"Query B: {statistics.median(b_times):.3f} ms")
        print("Query A plan:")
        for row in plan_a:
            print(row["QUERY PLAN"])
        print("Query B plan:")
        for row in plan_b:
            print(row["QUERY PLAN"])
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
