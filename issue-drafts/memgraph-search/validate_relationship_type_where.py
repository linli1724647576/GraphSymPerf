#!/usr/bin/env python3
"""Validate the relationship type(r) filtering candidate on fresh graphs."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


TYPED = "MATCH (a:A)-[r:LINK]->(b:B) RETURN count(r) AS c"
FILTERED = (
    "MATCH (a:A)-[r]->(b:B) "
    "WHERE type(r) = 'LINK' "
    "RETURN count(r) AS c"
)
TYPED_ROWS = (
    "MATCH (a:A)-[r:LINK]->(b:B) "
    "RETURN a.id AS a, b.id AS b, type(r) AS t ORDER BY a, b, t"
)
FILTERED_ROWS = (
    "MATCH (a:A)-[r]->(b:B) "
    "WHERE type(r) = 'LINK' "
    "RETURN a.id AS a, b.id AS b, type(r) AS t ORDER BY a, b, t"
)


def run(session: Any, text: str, parameters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def set_up(session: Any, nodes: int, noise_per_node: int) -> dict[str, int]:
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
        if noise_per_node:
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
    return {
        "nodes_a": nodes,
        "nodes_b": nodes,
        "link_edges": nodes,
        "noise_edges": nodes * noise_per_node,
    }


def measure(
    session: Any,
    query_a: str,
    query_b: str,
    *,
    repeats: int,
    seed: str,
) -> dict[str, Any]:
    rows_a, _ = run(session, query_a)
    rows_b, _ = run(session, query_b)
    if rows_a != rows_b:
        return {"equivalent": False, "rows_a": rows_a, "rows_b": rows_b}
    for _ in range(3):
        run(session, query_a)
        run(session, query_b)
    orders = ["AB", "BA"] * repeats
    random.Random(seed).shuffle(orders)
    timings: list[dict[str, Any]] = []
    for order in orders:
        item: dict[str, Any] = {"order": order}
        for side in order:
            _, elapsed = run(session, query_a if side == "A" else query_b)
            item[f"{side.lower()}_ms"] = elapsed
        timings.append(item)
    a = statistics.median(item["a_ms"] for item in timings)
    b = statistics.median(item["b_ms"] for item in timings)
    return {
        "equivalent": True,
        "median_a_ms": a,
        "median_b_ms": b,
        "ratio_b_over_a": b / max(a, 1e-9),
        "delta_ms": b - a,
        "timings": timings,
    }


def validate_level(
    session: Any,
    nodes: int,
    noise: int,
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    graph = set_up(session, nodes, noise)
    typed_rows, _ = run(session, TYPED_ROWS)
    filtered_rows, _ = run(session, FILTERED_ROWS)
    typed_plan, _ = run(session, f"EXPLAIN {TYPED}")
    filtered_plan, _ = run(session, f"EXPLAIN {FILTERED}")
    return {
        "noise_per_node": noise,
        "graph": graph,
        "exact_rows_equal": typed_rows == filtered_rows,
        "matched_rows": len(typed_rows),
        "comparison": measure(
            session,
            TYPED,
            FILTERED,
            repeats=repeats,
            seed=f"{seed}:{noise}:comparison",
        ),
        "aa_control": measure(
            session,
            TYPED,
            TYPED,
            repeats=repeats,
            seed=f"{seed}:{noise}:aa",
        ),
        "typed_plan": typed_plan,
        "filtered_plan": filtered_plan,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--noise-levels", default="0,5,20,50")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    levels = tuple(int(item) for item in args.noise_levels.split(","))
    if args.nodes <= 0 or args.repeats <= 0 or any(item < 0 for item in levels):
        parser.error("nodes and repeats must be positive; noise cannot be negative")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            results = [
                validate_level(session, args.nodes, noise, args.repeats, args.seed)
                for noise in levels
            ]
        payload = {
            "schema_version": 1,
            "purpose": "fresh-process validation of relationship type WHERE rule",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "nodes_per_label": args.nodes,
            "typed_query": TYPED,
            "filtered_query": FILTERED,
            "noise_levels": list(levels),
            "repeats_per_order": args.repeats,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for item in results:
            timing = item["comparison"]
            print(
                f"noise={item['noise_per_node']}: exact={item['exact_rows_equal']} "
                f"typed={timing['median_a_ms']:.3f}ms "
                f"filtered={timing['median_b_ms']:.3f}ms "
                f"ratio={timing['ratio_b_over_a']:.2f}"
            )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
