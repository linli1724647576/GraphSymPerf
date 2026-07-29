#!/usr/bin/env python3
"""Validate new LadybugDB rule families in one fresh embedded database."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

import ladybug


def query_pairs(nodes: int) -> dict[str, dict[str, Any]]:
    return {
        "primary_key_distinct_symmetry": {
            "query_a": (
                "MATCH (n:N) WITH n.id AS id RETURN count(id) AS value"
            ),
            "query_b": (
                "MATCH (n:N) WITH DISTINCT n.id AS id RETURN count(id) AS value"
            ),
            "expected": nodes,
            "theorem": "primary-key values are non-null and unique",
        },
        "optional_false_pattern_symmetry": {
            "query_a": "MATCH (n:N) RETURN count(*) AS value",
            "query_b": (
                "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) WHERE false "
                "RETURN count(*) AS value"
            ),
            "expected": nodes,
            "theorem": (
                "an optional pattern rejected by false contributes one "
                "null-extended row per input row"
            ),
        },
        "order_before_limited_count_symmetry": {
            "query_a": (
                "MATCH (n:N) WITH n LIMIT 100000 RETURN count(*) AS value"
            ),
            "query_b": (
                "MATCH (n:N) WITH n ORDER BY n.id LIMIT 100000 "
                "RETURN count(*) AS value"
            ),
            "expected": min(nodes, 100_000),
            "theorem": (
                "the order of a limited row set cannot change the number "
                "of rows in that set"
            ),
        },
    }


def supporting_mutators(nodes: int) -> dict[str, dict[str, Any]]:
    return {
        "primary_key_distinct_aggregate": {
            "family": "primary_key_distinct_symmetry",
            "query_a": "MATCH (n:N) RETURN count(n.id) AS value",
            "query_b": "MATCH (n:N) RETURN count(DISTINCT n.id) AS value",
            "expected": nodes,
        },
        "primary_key_distinct_sum": {
            "family": "primary_key_distinct_symmetry",
            "query_a": "MATCH (n:N) RETURN sum(n.id) AS value",
            "query_b": "MATCH (n:N) RETURN sum(DISTINCT n.id) AS value",
            "expected": nodes * (nodes + 1) // 2,
        },
        "optional_constant_false": {
            "family": "optional_false_pattern_symmetry",
            "query_a": "MATCH (n:N) RETURN count(*) AS value",
            "query_b": (
                "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) WHERE 1 = 0 "
                "RETURN count(*) AS value"
            ),
            "expected": nodes,
        },
        "order_before_full_limit_count": {
            "family": "order_before_limited_count_symmetry",
            "query_a": (
                f"MATCH (n:N) WITH n LIMIT {nodes} RETURN count(*) AS value"
            ),
            "query_b": (
                f"MATCH (n:N) WITH n ORDER BY n.id LIMIT {nodes} "
                "RETURN count(*) AS value"
            ),
            "expected": nodes,
        },
        "order_before_single_limit_count": {
            "family": "order_before_limited_count_symmetry",
            "query_a": "MATCH (n:N) WITH n LIMIT 1 RETURN count(*) AS value",
            "query_b": (
                "MATCH (n:N) WITH n ORDER BY n.id LIMIT 1 "
                "RETURN count(*) AS value"
            ),
            "expected": min(nodes, 1),
        },
        "order_before_skip_count": {
            "family": "order_before_limited_count_symmetry",
            "query_a": (
                "MATCH (n:N) WITH n SKIP 100000 RETURN count(*) AS value"
            ),
            "query_b": (
                "MATCH (n:N) WITH n ORDER BY n.id SKIP 100000 "
                "RETURN count(*) AS value"
            ),
            "expected": max(nodes - 100_000, 0),
        },
    }


def execute(connection: Any, query: str) -> tuple[list[list[Any]], float]:
    started = time.perf_counter()
    result = connection.execute(query).get_all()
    return result, (time.perf_counter() - started) * 1000.0


def setup(connection: Any, nodes: int, relationships: int) -> None:
    connection.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE REL TABLE S(FROM N TO N, id INT64)")
    connection.execute(
        f"UNWIND range(1, {nodes}) AS i CREATE (:N {{id:i}})"
    )
    if relationships:
        connection.execute(
            f"UNWIND range(1, {relationships}) AS i "
            "MATCH (a:N {id:i}), (b:N {id:i + 1}) "
            "CREATE (a)-[:S {id:i}]->(b)"
        )


def measure_pair(
    connection: Any,
    query_a: str,
    query_b: str,
    warmups: int,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    rows_a, _ = execute(connection, query_a)
    rows_b, _ = execute(connection, query_b)
    for _ in range(warmups):
        execute(connection, query_a)
        execute(connection, query_b)
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        order = ["A", "B"]
        rng.shuffle(order)
        elapsed: dict[str, float] = {}
        for side in order:
            _rows, elapsed[side] = execute(
                connection, query_a if side == "A" else query_b
            )
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
        "result_a": rows_a,
        "result_b": rows_b,
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


def measure_control(connection: Any, query: str, repetitions: int) -> dict[str, Any]:
    for _ in range(2):
        execute(connection, query)
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        _rows, first = execute(connection, query)
        _rows, second = execute(connection, query)
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


def profile(connection: Any, query: str) -> str:
    rows = connection.execute(f"PROFILE {query}").get_all()
    return str(rows[0][0])


def validate_case(
    connection: Any,
    pair: dict[str, Any],
    warmups: int,
    repetitions: int,
    controls: int,
    seed: int,
) -> dict[str, Any]:
    measurement = measure_pair(
        connection,
        pair["query_a"],
        pair["query_b"],
        warmups,
        repetitions,
        seed,
    )
    a_a = measure_control(connection, pair["query_a"], controls)
    b_b = measure_control(connection, pair["query_b"], controls)
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
    return {
        **pair,
        "measurement": measurement,
        "a_a_control": a_a,
        "b_b_control": b_b,
        "control_noise_fraction": control_noise_fraction,
        "profile_a": profile(connection, pair["query_a"]),
        "profile_b": profile(connection, pair["query_b"]),
        "validation_pass": validation_pass,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--nodes", type=int, default=200_000)
    parser.add_argument("--relationships", type=int, default=20_000)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--controls", type=int, default=6)
    parser.add_argument("--mutator-repetitions", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument(
        "--case",
        action="append",
        choices=tuple(query_pairs(1)),
        help="validate only the selected core case; may be repeated",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.relationships >= args.nodes:
        parser.error("--relationships must be smaller than --nodes")
    if args.database.exists():
        args.database.unlink()

    database = ladybug.Database(str(args.database))
    connection = ladybug.Connection(database)
    setup(connection, args.nodes, args.relationships)
    report: dict[str, Any] = {
        "schema_version": 1,
        "instance": args.instance,
        "ladybug_version": ladybug.__version__,
        "database": str(args.database),
        "dataset": {
            "n_nodes": args.nodes,
            "s_relationships": args.relationships,
        },
        "thresholds": {
            "minimum_ratio": 1.5,
            "minimum_delta_ms": 10.0,
            "minimum_direction_consistency": 0.75,
            "maximum_control_noise_fraction": 0.25,
        },
        "cases": {},
        "supporting_mutators": {},
    }
    selected_cases = args.case or list(query_pairs(args.nodes))
    all_pass = True
    for index, rule in enumerate(selected_cases):
        pair = query_pairs(args.nodes)[rule]
        result = validate_case(
            connection,
            pair,
            args.warmups,
            args.repetitions,
            args.controls,
            args.seed + index,
        )
        report["cases"][rule] = result
        all_pass = all_pass and result["validation_pass"]

    for index, (mutator, pair) in enumerate(supporting_mutators(args.nodes).items()):
        measurement = measure_pair(
            connection,
            pair["query_a"],
            pair["query_b"],
            2,
            args.mutator_repetitions,
            args.seed + 100 + index,
        )
        report["supporting_mutators"][mutator] = {
            **pair,
            "measurement": measurement,
        }

    report["validation_pass"] = all_pass
    args.output.parent.mkdir(parents=True, exist_ok=True)
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
                "metrics": {
                    rule: {
                        "a_ms": round(case["measurement"]["median_a_ms"], 3),
                        "b_ms": round(case["measurement"]["median_b_ms"], 3),
                        "ratio": round(case["measurement"]["median_ratio"], 2),
                        "delta_ms": round(
                            case["measurement"]["median_delta_ms"], 3
                        ),
                        "direction": case["measurement"][
                            "direction_consistency"
                        ],
                        "control_noise": round(
                            case["control_noise_fraction"], 3
                        ),
                    }
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
