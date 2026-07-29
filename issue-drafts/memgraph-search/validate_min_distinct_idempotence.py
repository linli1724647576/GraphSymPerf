#!/usr/bin/env python3
"""Validate redundant DISTINCT inside MIN and MAX aggregates on Memgraph."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


PAIRS = {
    "min": (
        "MATCH (n:Item) RETURN min(n.value) AS value",
        "MATCH (n:Item) RETURN min(DISTINCT n.value) AS value",
    ),
    "max": (
        "MATCH (n:Item) RETURN max(n.value) AS value",
        "MATCH (n:Item) RETURN max(DISTINCT n.value) AS value",
    ),
}


def run(
    session: Any,
    text: str,
    parameters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def setup(session: Any, node_count: int) -> dict[str, int]:
    run(session, "MATCH (n) DETACH DELETE n")
    for start in range(0, node_count, 5_000):
        end = min(node_count, start + 5_000)
        result = session.run(
            Query(
                "UNWIND range($start, $end - 1) AS i "
                "CREATE (:Item {id: i, value: i})",
                timeout=30,
            ),
            {"start": start, "end": end},
        )
        result.consume()
    run(
        session,
        "UNWIND range(1, 100) AS i CREATE (:Item {id: $node_count + i})",
        {"node_count": node_count},
    )
    return {"nodes_with_value": node_count, "nodes_without_value": 100}


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
    for _ in range(5):
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
    a_values = [item["a_ms"] for item in timings]
    b_values = [item["b_ms"] for item in timings]
    median_a = statistics.median(a_values)
    median_b = statistics.median(b_values)
    return {
        "equivalent": True,
        "rows": rows_a,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "ratio_b_over_a": median_b / max(median_a, 1e-9),
        "delta_ms": median_b - median_a,
        "timings": timings,
    }


def validate_scale(
    session: Any,
    count: int,
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    graph = setup(session, count)
    results: dict[str, Any] = {}
    for name, (query_a, query_b) in PAIRS.items():
        results[name] = {
            "query_a": query_a,
            "query_b": query_b,
            "ab": measure(
                session,
                query_a,
                query_b,
                repeats=repeats,
                seed=f"{seed}:{count}:{name}:ab",
            ),
            "aa_control": measure(
                session,
                query_a,
                query_a,
                repeats=repeats,
                seed=f"{seed}:{count}:{name}:aa",
            ),
            "bb_control": measure(
                session,
                query_b,
                query_b,
                repeats=repeats,
                seed=f"{seed}:{count}:{name}:bb",
            ),
            "plan_a": run(session, f"EXPLAIN {query_a}")[0],
            "plan_b": run(session, f"EXPLAIN {query_b}")[0],
        }
    return {"graph": graph, "aggregates": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--scales", default="50000,250000,500000")
    parser.add_argument("--repeats", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scales = tuple(int(item) for item in args.scales.split(","))
    if not scales or any(item <= 0 for item in scales) or args.repeats <= 0:
        parser.error("scales and repeats must be positive")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            results = [
                validate_scale(session, count, args.repeats, args.seed)
                for count in scales
            ]
        payload = {
            "schema_version": 1,
            "purpose": "fresh-process performance validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "scales": list(scales),
            "repeats_per_order": args.repeats,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for result in results:
            print(f"{result['graph']['nodes_with_value']} nodes")
            for name, aggregate in result["aggregates"].items():
                ab = aggregate["ab"]
                print(
                    f"  {name}: equal={ab['equivalent']} "
                    f"plain={ab['median_a_ms']:.3f}ms "
                    f"distinct={ab['median_b_ms']:.3f}ms "
                    f"ratio={ab['ratio_b_over_a']:.2f} "
                    f"delta={ab['delta_ms']:.3f}ms"
                )
        print(f"wrote {args.output}")
        largest = results[-1]["aggregates"]["min"]["ab"]
        red = (
            largest["equivalent"]
            and largest["ratio_b_over_a"] >= 1.5
            and largest["delta_ms"] >= 25.0
        )
        return 1 if red else 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
