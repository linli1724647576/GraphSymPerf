#!/usr/bin/env python3
"""Reproduce the exact zero-hop path slowdown."""

from __future__ import annotations

import argparse
import random
import statistics
import time
from typing import Any

from neo4j import GraphDatabase, Query


QUERY_A = "MATCH (n:Item) RETURN count(*) AS c"
QUERY_B = "MATCH (n:Item)-[:UNUSED*0..0]->(m:Item) RETURN count(*) AS c"
ROWS_A = "MATCH (n:Item) RETURN n.id AS n, n.id AS m"
ROWS_B = (
    "MATCH (n:Item)-[:UNUSED*0..0]->(m:Item) "
    "RETURN n.id AS n, m.id AS m"
)


def run(session: Any, text: str, parameters: dict[str, Any] | None = None):
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def setup(session: Any, node_count: int) -> None:
    run(session, "MATCH (n) DETACH DELETE n")
    for start in range(0, node_count, 5_000):
        end = min(node_count, start + 5_000)
        run(
            session,
            "UNWIND range($start, $end - 1) AS i CREATE (:Item {id: i})",
            {"start": start, "end": end},
        )
    run(session, "UNWIND range(1, 100) AS i CREATE (:Other {id: i})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--nodes", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            setup(session, args.nodes)
            rows_a, _ = run(session, ROWS_A)
            rows_b, _ = run(session, ROWS_B)
            canonical_a = sorted((row["n"], row["m"]) for row in rows_a)
            canonical_b = sorted((row["n"], row["m"]) for row in rows_b)
            if canonical_a != canonical_b:
                raise RuntimeError("the two queries returned different endpoints")
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
        print(f"Endpoint rows equal: {canonical_a == canonical_b} ({len(canonical_a)} rows)")
        print(f"Node match: {statistics.median(a_times):.3f} ms")
        print(f"Zero-hop path: {statistics.median(b_times):.3f} ms")
        print("Node-match plan:")
        for row in plan_a:
            print(row["QUERY PLAN"])
        print("Zero-hop plan:")
        for row in plan_b:
            print(row["QUERY PLAN"])
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
