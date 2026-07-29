#!/usr/bin/env python3
"""Screen relationship-type pattern syntax against WHERE type(r)."""

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
    "RETURN a.id AS a, b.id AS b ORDER BY a, b"
)
FILTERED_ROWS = (
    "MATCH (a:A)-[r]->(b:B) "
    "WHERE type(r) = 'LINK' "
    "RETURN a.id AS a, b.id AS b ORDER BY a, b"
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
    edge_rows, _ = run(session, "MATCH ()-[r]->() RETURN count(r) AS c")
    return {
        "nodes_a": nodes,
        "nodes_b": nodes,
        "link_edges": nodes,
        "noise_edges": nodes * noise_per_node,
        "relationships": edge_rows[0]["c"],
    }


def measure(session: Any, repeats: int, seed: int) -> dict[str, Any]:
    typed_rows, _ = run(session, TYPED_ROWS)
    filtered_rows, _ = run(session, FILTERED_ROWS)
    if typed_rows != filtered_rows:
        return {
            "equivalent": False,
            "typed_rows": typed_rows,
            "filtered_rows": filtered_rows,
        }
    for _ in range(3):
        run(session, TYPED)
        run(session, FILTERED)
    orders = ["AB", "BA"] * repeats
    random.Random(seed).shuffle(orders)
    timings: list[dict[str, Any]] = []
    for order in orders:
        item: dict[str, Any] = {"order": order}
        for side in order:
            _, elapsed = run(session, TYPED if side == "A" else FILTERED)
            item[f"{side.lower()}_ms"] = elapsed
        timings.append(item)
    typed = statistics.median(item["a_ms"] for item in timings)
    filtered = statistics.median(item["b_ms"] for item in timings)
    return {
        "equivalent": True,
        "matched_rows": len(typed_rows),
        "median_typed_ms": typed,
        "median_filtered_ms": filtered,
        "ratio_filtered_over_typed": filtered / max(typed, 1e-9),
        "delta_ms": filtered - typed,
        "timings": timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--noise-per-node", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.nodes <= 0 or args.noise_per_node < 0 or args.repeats <= 0:
        parser.error("nodes and repeats must be positive; noise cannot be negative")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            graph = set_up(session, args.nodes, args.noise_per_node)
            timing = measure(session, args.repeats, args.seed)
            typed_plan, _ = run(session, f"EXPLAIN {TYPED}")
            filtered_plan, _ = run(session, f"EXPLAIN {FILTERED}")
        payload = {
            "schema_version": 1,
            "purpose": "targeted discovery screen; not submit-ready validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "graph": graph,
            "typed_query": TYPED,
            "filtered_query": FILTERED,
            "timing": timing,
            "typed_plan": typed_plan,
            "filtered_plan": filtered_plan,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"exact={timing['equivalent']} matches={timing.get('matched_rows')} "
            f"typed={timing.get('median_typed_ms', 0):.3f}ms "
            f"filtered={timing.get('median_filtered_ms', 0):.3f}ms "
            f"ratio={timing.get('ratio_filtered_over_typed', 0):.2f}"
        )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
