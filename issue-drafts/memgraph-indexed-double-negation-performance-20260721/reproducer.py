#!/usr/bin/env python3
"""Reproduce the indexed double-negation performance regression."""

from __future__ import annotations

import argparse
import random
import statistics
import time
from typing import Any

from neo4j import GraphDatabase, Query


QUERY_A = "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c"
QUERY_B = "MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN count(*) AS c"
IDS_A = "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id"
IDS_B = "MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN n.id AS id ORDER BY id"


def run(session: Any, text: str, parameters: dict[str, Any] | None = None):
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000


def setup(session: Any, node_count: int) -> None:
    run(session, "MATCH (n) DETACH DELETE n")
    try:
        run(session, "DROP INDEX ON :Item(key)")
    except Exception:
        pass
    run(session, "CREATE INDEX ON :Item(key)")
    for start in range(0, node_count, 5_000):
        end = min(node_count, start + 5_000)
        run(
            session,
            """
            UNWIND range($start, $end - 1) AS i
            CREATE (:Item {id: i, key: i % 1000})
            """,
            {"start": start, "end": end},
        )
    run(
        session,
        "UNWIND range($start, $start + 99) AS i CREATE (:Item {id: i})",
        {"start": node_count},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--nodes", type=int, default=500_000)
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            setup(session, args.nodes)
            ids_a, _ = run(session, IDS_A)
            ids_b, _ = run(session, IDS_B)
            if ids_a != ids_b:
                raise RuntimeError("the two queries returned different node IDs")

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
        print(f"Matching IDs equal: {ids_a == ids_b} ({len(ids_a)} rows)")
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
