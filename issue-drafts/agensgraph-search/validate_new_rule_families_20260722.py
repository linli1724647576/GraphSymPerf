#!/usr/bin/env python3
"""Validate new AgensGraph rule families on one independent instance."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg


GRAPH = "agens_new_rules"
DIGITS = "[0,1,2,3,4,5,6,7,8,9]"

QUERY_PAIRS = {
    "optional_null_rejection_symmetry": {
        "query_a": "MATCH (n:RA)-[:R]->(m:RB) RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:RA) OPTIONAL MATCH (n)-[:R]->(m:RB) "
            "WITH n, m WHERE m IS NOT NULL RETURN count(*) AS value"
        ),
        "expected": 20_000,
    },
    "singleton_unwind_symmetry": {
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) UNWIND [1] AS x RETURN sum(n.grp) AS value",
        "expected": 4_799_775,
    },
}


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple | list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return value


def execute(cursor: Any, query: str) -> tuple[list[tuple[Any, ...]], float]:
    started = time.perf_counter()
    cursor.execute(query)
    rows = cursor.fetchall() if cursor.description else []
    return rows, (time.perf_counter() - started) * 1000.0


def setup(cursor: Any) -> None:
    cursor.execute(f"DROP GRAPH IF EXISTS {GRAPH} CASCADE")
    cursor.execute(f"CREATE GRAPH {GRAPH}")
    cursor.execute(f"SET graph_path = {GRAPH}")
    cursor.execute(
        f"UNWIND {DIGITS} AS a UNWIND {DIGITS} AS b UNWIND {DIGITS} AS c "
        f"UNWIND {DIGITS} AS d UNWIND {DIGITS} AS e "
        "WITH a * 10000 + b * 1000 + c * 100 + d * 10 + e + 1 AS id "
        "CREATE (:N {id: id, grp: id % 97})"
    )
    cursor.execute(
        f"UNWIND [0,1] AS batch UNWIND {DIGITS} AS a UNWIND {DIGITS} AS b "
        f"UNWIND {DIGITS} AS c UNWIND {DIGITS} AS d "
        "WITH batch * 10000 + a * 1000 + b * 100 + c * 10 + d + 1 AS id "
        "CREATE (:RA {id: id})-[:R {id: id}]->(:RB {id: id})"
    )
    cursor.execute(
        f"UNWIND {DIGITS} AS batch UNWIND {DIGITS} AS a UNWIND {DIGITS} AS b "
        f"UNWIND {DIGITS} AS c UNWIND {DIGITS} AS d "
        "WITH 20000 + batch * 10000 + a * 1000 + b * 100 + c * 10 + d + 1 AS id "
        "CREATE (:RA {id: id})"
    )
    for label in ("n", "ra", "rb", "r"):
        cursor.execute(f"ANALYZE {GRAPH}.{label}")


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
            _rows, elapsed[side] = execute(cursor, query_a if side == "A" else query_b)
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
    return {
        "rows_equal": rows_a == rows_b,
        "result_a": jsonable(rows_a),
        "result_b": jsonable(rows_b),
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": median_b / max(median_a, 0.001),
        "median_delta_ms": median_b - median_a,
        "direction_consistency": sum(
            sample["b_ms"] > sample["a_ms"] for sample in samples
        )
        / len(samples),
        "samples": samples,
    }


def measure_control(cursor: Any, query: str, repetitions: int) -> dict[str, Any]:
    for _ in range(2):
        execute(cursor, query)
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        _rows, first = execute(cursor, query)
        _rows, second = execute(cursor, query)
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
    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {query}")
    return [row[0] for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", default="agens")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("AGENS_PASSWORD"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set AGENS_PASSWORD or pass --password")

    report: dict[str, Any] = {
        "schema_version": 1,
        "instance": args.instance,
        "graph": GRAPH,
        "dataset": {
            "n_nodes": 100_000,
            "matched_ra_nodes": 20_000,
            "unmatched_ra_nodes": 100_000,
            "rb_nodes": 20_000,
            "r_edges": 20_000,
        },
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
            cursor.execute("SET statement_timeout = '120s'")
            cursor.execute("SELECT version()")
            report["version"] = cursor.fetchone()[0]
            setup(cursor)
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
                result_value = measurement["result_a"][0][0]
                validation_pass = bool(
                    measurement["rows_equal"]
                    and result_value == pair["expected"]
                    and measurement["median_ratio"] >= 1.5
                    and measurement["median_delta_ms"] >= 10.0
                    and measurement["direction_consistency"] >= 0.75
                    and control_noise_fraction <= 0.25
                )
                report["cases"][rule] = {
                    **pair,
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

    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str),
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
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["validation_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
