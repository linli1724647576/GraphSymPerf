#!/usr/bin/env python3
"""Independently validate the finite IN/disjunction Memgraph candidate."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


QUERY_OR_COUNT = (
    "MATCH (n:Item) "
    "WHERE n.key = 42 OR n.key = 43 "
    "RETURN count(*) AS c"
)
QUERY_IN_COUNT = (
    "MATCH (n:Item) "
    "WHERE n.key IN [42, 43] "
    "RETURN count(*) AS c"
)
QUERY_OR_IDS = (
    "MATCH (n:Item) "
    "WHERE n.key = 42 OR n.key = 43 "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_IN_IDS = (
    "MATCH (n:Item) "
    "WHERE n.key IN [42, 43] "
    "RETURN n.id AS id ORDER BY id"
)


def run(session: Any, text: str, parameters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def set_up(session: Any, count: int) -> None:
    run(session, "MATCH (n) DETACH DELETE n")
    try:
        run(session, "DROP INDEX ON :Item(key)")
    except Exception:
        pass
    run(session, "CREATE INDEX ON :Item(key)")
    for start in range(0, count, 5_000):
        end = min(count, start + 5_000)
        run(
            session,
            """
            UNWIND range($start, $end - 1) AS i
            CREATE (:Item {
              id: i,
              key: i % 1000,
              nullable: CASE WHEN i % 7 = 0 THEN null ELSE i % 1000 END
            })
            """,
            {"start": start, "end": end},
        )


def time_pair(
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
    a_values = [item["a_ms"] for item in timings]
    b_values = [item["b_ms"] for item in timings]
    median_a = statistics.median(a_values)
    median_b = statistics.median(b_values)
    return {
        "equivalent": True,
        "rows": rows_a,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "ratio_a_over_b": median_a / max(median_b, 1e-9),
        "delta_ms": median_a - median_b,
        "timings": timings,
    }


def explain(session: Any, query: str) -> list[dict[str, Any]]:
    rows, _ = run(session, f"EXPLAIN {query}")
    return rows


def validate_scale(session: Any, count: int, repeats: int, seed: int) -> dict[str, Any]:
    set_up(session, count)
    ids_or, _ = run(session, QUERY_OR_IDS)
    ids_in, _ = run(session, QUERY_IN_IDS)
    indexed = time_pair(
        session,
        QUERY_OR_COUNT,
        QUERY_IN_COUNT,
        repeats=repeats,
        seed=f"{seed}:{count}:indexed",
    )
    aa_control = time_pair(
        session,
        QUERY_OR_COUNT,
        QUERY_OR_COUNT,
        repeats=repeats,
        seed=f"{seed}:{count}:aa",
    )
    indexed_plans = {
        "or": explain(session, QUERY_OR_COUNT),
        "in": explain(session, QUERY_IN_COUNT),
    }
    result: dict[str, Any] = {
        "nodes": count,
        "expected_matches": 2 * (count // 1000),
        "exact_ids_equal": ids_or == ids_in,
        "matched_ids": len(ids_or),
        "indexed": indexed,
        "aa_control": aa_control,
        "indexed_plans": indexed_plans,
    }
    if count == max(SCALES):
        run(session, "DROP INDEX ON :Item(key)")
        result["without_index"] = time_pair(
            session,
            QUERY_OR_COUNT,
            QUERY_IN_COUNT,
            repeats=repeats,
            seed=f"{seed}:{count}:no-index",
        )
        result["without_index_plans"] = {
            "or": explain(session, QUERY_OR_COUNT),
            "in": explain(session, QUERY_IN_COUNT),
        }
    return result


SCALES: tuple[int, ...] = ()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--scales", default="20000,100000,200000,500000")
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    global SCALES
    SCALES = tuple(int(item) for item in args.scales.split(","))
    if not SCALES or any(item <= 0 for item in SCALES) or args.repeats <= 0:
        parser.error("scales and repeats must be positive")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            results = [
                validate_scale(session, count, args.repeats, args.seed)
                for count in SCALES
            ]
        payload = {
            "schema_version": 1,
            "purpose": "fresh-process validation of finite IN/disjunction candidate",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "query_or": QUERY_OR_COUNT,
            "query_in": QUERY_IN_COUNT,
            "scales": list(SCALES),
            "repeats_per_order": args.repeats,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for item in results:
            indexed = item["indexed"]
            print(
                f"{item['nodes']} nodes: exact={item['exact_ids_equal']} "
                f"OR={indexed['median_a_ms']:.3f}ms "
                f"IN={indexed['median_b_ms']:.3f}ms "
                f"ratio={indexed['ratio_a_over_b']:.2f}"
            )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
