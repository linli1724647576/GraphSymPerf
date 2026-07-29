#!/usr/bin/env python3
"""Validate static subscript property access against indexed dot access."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


QUERY_A = "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c"
QUERY_B = "MATCH (n:Item) WHERE n['key'] = 42 RETURN count(*) AS c"
ROWS_A = "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id"
ROWS_B = "MATCH (n:Item) WHERE n['key'] = 42 RETURN n.id AS id ORDER BY id"


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


def best_effort(session: Any, statement: str) -> None:
    try:
        run(session, statement)
    except Exception:
        pass


def setup(session: Any, node_count: int) -> dict[str, int]:
    run(session, "MATCH (n) DETACH DELETE n")
    best_effort(session, "DROP INDEX ON :Item(key)")
    run(session, "CREATE INDEX ON :Item(key)")
    for start in range(0, node_count, 5_000):
        end = min(node_count, start + 5_000)
        run(
            session,
            "UNWIND range($start, $end - 1) AS i "
            "CREATE (:Item {id: i, key: i % 1000})",
            {"start": start, "end": end},
        )
    run(
        session,
        "UNWIND range($start, $start + 99) AS i CREATE (:Item {id: i})",
        {"start": node_count},
    )
    return {"nodes_with_key": node_count, "nodes_without_key": 100}


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
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "ratio_b_over_a": median_b / max(median_a, 1e-9),
        "delta_ms": median_b - median_a,
        "timings": timings,
    }


def validate_scale(
    session: Any,
    count: int,
    largest: int,
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    graph = setup(session, count)
    rows_a, _ = run(session, ROWS_A)
    rows_b, _ = run(session, ROWS_B)
    result: dict[str, Any] = {
        "graph": graph,
        "exact_rows_equal": rows_a == rows_b,
        "matched_rows": len(rows_a),
        "indexed": measure(
            session,
            QUERY_A,
            QUERY_B,
            repeats=repeats,
            seed=f"{seed}:{count}:ab",
        ),
        "aa_control": measure(
            session,
            QUERY_A,
            QUERY_A,
            repeats=repeats,
            seed=f"{seed}:{count}:aa",
        ),
        "bb_control": measure(
            session,
            QUERY_B,
            QUERY_B,
            repeats=repeats,
            seed=f"{seed}:{count}:bb",
        ),
        "plan_a": run(session, f"EXPLAIN {QUERY_A}")[0],
        "plan_b": run(session, f"EXPLAIN {QUERY_B}")[0],
    }
    if count == largest:
        run(session, "DROP INDEX ON :Item(key)")
        result["without_index"] = measure(
            session,
            QUERY_A,
            QUERY_B,
            repeats=repeats,
            seed=f"{seed}:{count}:without-index",
        )
        result["without_index_plan_a"] = run(session, f"EXPLAIN {QUERY_A}")[0]
        result["without_index_plan_b"] = run(session, f"EXPLAIN {QUERY_B}")[0]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--scales", default="20000,100000,500000")
    parser.add_argument("--repeats", type=int, default=10)
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
                validate_scale(session, count, max(scales), args.repeats, args.seed)
                for count in scales
            ]
        payload = {
            "schema_version": 1,
            "purpose": "fresh-process performance validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "query_a": QUERY_A,
            "query_b": QUERY_B,
            "scales": list(scales),
            "repeats_per_order": args.repeats,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for result in results:
            indexed = result["indexed"]
            print(
                f"{result['graph']['nodes_with_key']} nodes: "
                f"exact={result['exact_rows_equal']} "
                f"dot={indexed['median_a_ms']:.3f}ms "
                f"subscript={indexed['median_b_ms']:.3f}ms "
                f"ratio={indexed['ratio_b_over_a']:.2f} "
                f"delta={indexed['delta_ms']:.3f}ms"
            )
        print(f"wrote {args.output}")
        largest = results[-1]["indexed"]
        red = (
            results[-1]["exact_rows_equal"]
            and largest["ratio_b_over_a"] >= 5.0
            and largest["delta_ms"] >= 10.0
        )
        return 1 if red else 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
