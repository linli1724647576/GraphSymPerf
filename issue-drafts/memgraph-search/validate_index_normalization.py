#!/usr/bin/env python3
"""Validate index-sensitive predicate-normalization candidates on Memgraph."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


BASE = "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c"
BASE_IDS = "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id"
CANDIDATES = {
    "indexed_double_negation_predicate": {
        "query": "MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN count(*) AS c",
        "ids": "MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN n.id AS id ORDER BY id",
        "proof_boundary": "Kleene double negation over a pure finite-integer equality",
    },
    "negated_inequality_normalization": {
        "query": "MATCH (n:Item) WHERE NOT (n.key <> 42) RETURN count(*) AS c",
        "ids": "MATCH (n:Item) WHERE NOT (n.key <> 42) RETURN n.id AS id ORDER BY id",
        "proof_boundary": "finite integer equality; null remains rejected",
    },
    "integer_add_zero_identity": {
        "query": "MATCH (n:Item) WHERE n.key + 0 = 42 RETURN count(*) AS c",
        "ids": "MATCH (n:Item) WHERE n.key + 0 = 42 RETURN n.id AS id ORDER BY id",
        "proof_boundary": "finite integer property; adding zero preserves value and null",
    },
    "case_equality_selection": {
        "query": (
            "MATCH (n:Item) "
            "WHERE CASE WHEN n.key = 42 THEN true ELSE false END "
            "RETURN count(*) AS c"
        ),
        "ids": (
            "MATCH (n:Item) "
            "WHERE CASE WHEN n.key = 42 THEN true ELSE false END "
            "RETURN n.id AS id ORDER BY id"
        ),
        "proof_boundary": "WHERE selection only; null and false both reject",
    },
}


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
            CREATE (:Item {id: i, key: i % 1000})
            """,
            {"start": start, "end": end},
        )
    run(
        session,
        "UNWIND range($start, $start + 99) AS i CREATE (:Item {id: i})",
        {"start": count},
    )


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
    a = [item["a_ms"] for item in timings]
    b = [item["b_ms"] for item in timings]
    median_a = statistics.median(a)
    median_b = statistics.median(b)
    return {
        "equivalent": True,
        "rows": rows_a,
        "median_base_ms": median_a,
        "median_transformed_ms": median_b,
        "ratio_transformed_over_base": median_b / max(median_a, 1e-9),
        "delta_ms": median_b - median_a,
        "timings": timings,
    }


def plan(session: Any, query: str) -> list[dict[str, Any]]:
    rows, _ = run(session, f"EXPLAIN {query}")
    return rows


def validate_scale(
    session: Any,
    count: int,
    largest: int,
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    set_up(session, count)
    base_ids, _ = run(session, BASE_IDS)
    result: dict[str, Any] = {
        "nodes_with_key": count,
        "nodes_without_key": 100,
        "aa_control": measure(
            session,
            BASE,
            BASE,
            repeats=repeats,
            seed=f"{seed}:{count}:aa",
        ),
        "base_plan": plan(session, BASE),
        "candidates": {},
    }
    for rule_id, candidate in CANDIDATES.items():
        transformed_ids, _ = run(session, str(candidate["ids"]))
        result["candidates"][rule_id] = {
            "proof_boundary": candidate["proof_boundary"],
            "exact_ids_equal": base_ids == transformed_ids,
            "matched_ids": len(base_ids),
            "indexed": measure(
                session,
                BASE,
                str(candidate["query"]),
                repeats=repeats,
                seed=f"{seed}:{count}:{rule_id}:indexed",
            ),
            "transformed_plan": plan(session, str(candidate["query"])),
        }
    if count == largest:
        run(session, "DROP INDEX ON :Item(key)")
        for rule_id, candidate in CANDIDATES.items():
            result["candidates"][rule_id]["without_index"] = measure(
                session,
                BASE,
                str(candidate["query"]),
                repeats=repeats,
                seed=f"{seed}:{count}:{rule_id}:without-index",
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--scales", default="20000,100000,200000,500000")
    parser.add_argument("--repeats", type=int, default=6)
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
            "purpose": "fresh-process validation of index-sensitive normalization rules",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "base_query": BASE,
            "scales": list(scales),
            "repeats_per_order": args.repeats,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for item in results:
            print(f"{item['nodes_with_key']} nodes")
            for rule_id, candidate in item["candidates"].items():
                indexed = candidate["indexed"]
                print(
                    f"  {rule_id}: exact={candidate['exact_ids_equal']} "
                    f"base={indexed['median_base_ms']:.3f}ms "
                    f"changed={indexed['median_transformed_ms']:.3f}ms "
                    f"ratio={indexed['ratio_transformed_over_base']:.2f}"
                )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
