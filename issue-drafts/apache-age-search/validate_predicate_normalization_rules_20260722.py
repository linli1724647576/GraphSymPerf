#!/usr/bin/env python3
"""Validate Apache AGE predicate-normalization performance candidates."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg


GRAPH = "age_predicate_validation"

QUERY_PAIRS = {
    "finite_disjunction_in_symmetry": {
        "query_a": (
            "MATCH (n:N) WHERE n.grp = 42 OR n.grp = 43 "
            "RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (n:N) WHERE n.grp IN [42, 43] RETURN count(*) AS value"
        ),
        "expected_slow_side": "A",
    },
    "point_range_symmetry": {
        "query_a": (
            "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (n:N) WHERE n.score >= 42420 AND n.score <= 42420 "
            "RETURN count(*) AS value"
        ),
        "expected_slow_side": "B",
    },
}


def sql_for(query: str) -> str:
    return (
        f"SELECT * FROM ag_catalog.cypher('{GRAPH}', $age_predicate${query}$age_predicate$) "
        'AS ("value" agtype)'
    )


def execute(cursor: Any, query: str) -> tuple[list[list[str]], float]:
    started = time.perf_counter()
    cursor.execute(sql_for(query))
    rows = [[str(value) for value in row] for row in cursor.fetchall()]
    return rows, (time.perf_counter() - started) * 1000.0


def setup(cursor: Any, nodes: int) -> None:
    cursor.execute(
        "SELECT ag_catalog.drop_graph(%s, true) "
        "WHERE EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s)",
        (GRAPH, GRAPH),
    )
    cursor.execute("SELECT ag_catalog.create_graph(%s)", (GRAPH,))
    execute(
        cursor,
        f"UNWIND range(1, {nodes}) AS i "
        "CREATE (:N {id: i, score: i, grp: i % 97}) RETURN count(*) AS value",
    )
    cursor.execute(f'ANALYZE {GRAPH}."N"')


def measure_pair(
    cursor: Any,
    query_a: str,
    query_b: str,
    warmups: int,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    rows_a, _ = execute(cursor, query_a)
    rows_b, _ = execute(cursor, query_b)
    for _ in range(warmups):
        execute(cursor, query_a)
        execute(cursor, query_b)
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        order = ["A", "B"]
        rng.shuffle(order)
        elapsed: dict[str, float] = {}
        for side in order:
            _, elapsed[side] = execute(cursor, query_a if side == "A" else query_b)
        samples.append(
            {
                "repetition": repetition + 1,
                "order": "".join(order),
                "a_ms": elapsed["A"],
                "b_ms": elapsed["B"],
            }
        )
    median_a = statistics.median(sample["a_ms"] for sample in samples)
    median_b = statistics.median(sample["b_ms"] for sample in samples)
    slow_side = "A" if median_a >= median_b else "B"
    slow = max(median_a, median_b)
    fast = min(median_a, median_b)
    return {
        "rows_equal": rows_a == rows_b,
        "result_a": rows_a,
        "result_b": rows_b,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "slow_side": slow_side,
        "median_ratio": slow / max(fast, 0.001),
        "median_delta_ms": slow - fast,
        "direction_consistency": sum(
            (sample["a_ms"] > sample["b_ms"]) == (slow_side == "A")
            for sample in samples
        )
        / len(samples),
        "samples": samples,
    }


def measure_control(cursor: Any, query: str, repetitions: int) -> dict[str, Any]:
    for _ in range(2):
        execute(cursor, query)
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        _, first = execute(cursor, query)
        _, second = execute(cursor, query)
        samples.append(
            {"repetition": repetition + 1, "first_ms": first, "second_ms": second}
        )
    first_median = statistics.median(sample["first_ms"] for sample in samples)
    second_median = statistics.median(sample["second_ms"] for sample in samples)
    return {
        "first_median_ms": first_median,
        "second_median_ms": second_median,
        "median_delta_ms": abs(first_median - second_median),
        "samples": samples,
    }


def explain(cursor: Any, query: str) -> list[str]:
    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql_for(query)}")
    return [row[0] for row in cursor.fetchall()]


def expected_rows(rule: str, nodes: int) -> list[list[str]]:
    if rule == "point_range_symmetry":
        return [["1" if nodes >= 42420 else "0"]]
    count = sum(1 for value in range(1, nodes + 1) if value % 97 in (42, 43))
    return [[str(count)]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("AGE_PASSWORD"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--nodes", type=int, default=200_000)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set AGE_PASSWORD or pass --password")

    report: dict[str, Any] = {
        "schema_version": 1,
        "instance": args.instance,
        "graph": GRAPH,
        "dataset": {"n_nodes": args.nodes},
        "cases": {},
    }
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
            cursor.execute("SET statement_timeout = '120s'")
            cursor.execute("SELECT version()")
            report["postgres_version"] = cursor.fetchone()[0]
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'age'")
            report["age_version"] = cursor.fetchone()[0]
            cursor.execute("SHOW jit")
            report["jit"] = cursor.fetchone()[0]
            setup(cursor, args.nodes)
            all_pass = True
            for index, (rule, pair) in enumerate(QUERY_PAIRS.items()):
                measurement = measure_pair(
                    cursor,
                    pair["query_a"],
                    pair["query_b"],
                    args.warmups,
                    args.repetitions,
                    args.seed + index,
                )
                a_a = measure_control(cursor, pair["query_a"], 4)
                b_b = measure_control(cursor, pair["query_b"], 4)
                control_noise_fraction = max(
                    a_a["median_delta_ms"], b_b["median_delta_ms"]
                ) / max(measurement["median_delta_ms"], 0.001)
                expected = expected_rows(rule, args.nodes)
                validation_pass = bool(
                    measurement["rows_equal"]
                    and measurement["result_a"] == expected
                    and measurement["slow_side"] == pair["expected_slow_side"]
                    and measurement["median_ratio"] >= 1.5
                    and measurement["median_delta_ms"] >= 10.0
                    and measurement["direction_consistency"] >= 0.75
                    and control_noise_fraction <= 0.25
                )
                report["cases"][rule] = {
                    **pair,
                    "expected": expected,
                    "measurement": measurement,
                    "a_a_control": a_a,
                    "b_b_control": b_b,
                    "control_noise_fraction": control_noise_fraction,
                    "profile_a": explain(cursor, pair["query_a"]),
                    "profile_b": explain(cursor, pair["query_b"]),
                    "validation_pass": validation_pass,
                }
                all_pass = all_pass and validation_pass
            report["validation_pass"] = all_pass

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "validation_pass": report["validation_pass"],
                "case_passes": {
                    rule: case["validation_pass"]
                    for rule, case in report["cases"].items()
                },
            },
            sort_keys=True,
        )
    )
    return 0 if report["validation_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
