#!/usr/bin/env python3
"""Validate count(*) versus count(non-null literal) on Neo4j Cypher 25."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any


QUERY_A = "MATCH (n:N) RETURN count(*) AS value"
QUERY_B = "MATCH (n:N) RETURN count(1) AS value"
QUERY_CONTROL = "MATCH (n:N) RETURN count(n) AS value"


def setup_graph(driver: Any, nodes: int) -> None:
    statements: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
            {},
        ),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { CREATE (:N {id: i}) } IN TRANSACTIONS OF 5000 ROWS",
            {"nodes": nodes},
        ),
    )
    with driver.session(database="neo4j") as session:
        for statement, parameters in statements:
            session.run(statement, parameters).consume()


def execute(session: Any, query: str, timeout: int) -> tuple[tuple[Any, ...], float]:
    from neo4j import Query

    started = time.perf_counter()
    result = session.run(Query(f"CYPHER 25 {query}", timeout=timeout))
    rows = tuple(tuple(record.values()) for record in result)
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def measure_pair(
    driver: Any,
    warmups: int,
    repetitions: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        rows_a, _ = execute(session, QUERY_A, timeout)
        rows_b, _ = execute(session, QUERY_B, timeout)
        rows_control, _ = execute(session, QUERY_CONTROL, timeout)
        if rows_a != rows_b or rows_a != rows_control:
            raise AssertionError(
                f"count results differ: A={rows_a}, B={rows_b}, control={rows_control}"
            )
        for _ in range(warmups):
            execute(session, QUERY_A, timeout)
            execute(session, QUERY_B, timeout)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                _rows, elapsed[side] = execute(
                    session, QUERY_A if side == "A" else QUERY_B, timeout
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
    direction_consistency = sum(
        sample["b_ms"] > sample["a_ms"] for sample in samples
    ) / len(samples)
    return {
        "rows_equal": True,
        "result": rows_a,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": median_b / max(median_a, 0.001),
        "median_delta_ms": median_b - median_a,
        "direction_consistency": direction_consistency,
        "samples": samples,
    }


def measure_control(
    driver: Any, query: str, repetitions: int, timeout: int
) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    with driver.session(database="neo4j") as session:
        for _ in range(2):
            execute(session, query, timeout)
        for repetition in range(repetitions):
            _rows, first = execute(session, query, timeout)
            _rows, second = execute(session, query, timeout)
            samples.append(
                {
                    "repetition": repetition + 1,
                    "first_ms": first,
                    "second_ms": second,
                }
            )
    first_median = statistics.median(sample["first_ms"] for sample in samples)
    second_median = statistics.median(sample["second_ms"] for sample in samples)
    return {
        "first_median_ms": first_median,
        "second_median_ms": second_median,
        "median_delta_ms": abs(first_median - second_median),
        "samples": samples,
    }


def capture_plan(driver: Any, query: str, timeout: int, profile: bool) -> Any:
    from neo4j import Query

    prefix = "PROFILE" if profile else "EXPLAIN"
    with driver.session(database="neo4j") as session:
        result = session.run(
            Query(f"CYPHER 25 {prefix} {query}", timeout=timeout)
        )
        tuple(result)
        summary = result.consume()
        plan = summary.profile if profile else summary.plan
        return json.loads(json.dumps(plan, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    report: dict[str, Any] = {
        "schema_version": 1,
        "instance": args.instance,
        "nodes": args.nodes,
        "cypher_version": "25",
        "query_a": QUERY_A,
        "query_b": QUERY_B,
        "control_query": QUERY_CONTROL,
    }
    try:
        setup_graph(driver, args.nodes)
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD versions, edition "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            report["environment"] = component.data()
        measurement = measure_pair(
            driver,
            args.warmups,
            args.repetitions,
            args.timeout,
            args.seed,
        )
        report["measurement"] = measurement
        a_a_control = measure_control(driver, QUERY_A, 4, args.timeout)
        b_b_control = measure_control(driver, QUERY_B, 4, args.timeout)
        report["a_a_control"] = a_a_control
        report["b_b_control"] = b_b_control
        report["explain_a"] = capture_plan(
            driver, QUERY_A, args.timeout, False
        )
        report["explain_b"] = capture_plan(
            driver, QUERY_B, args.timeout, False
        )
        report["profile_a"] = capture_plan(
            driver, QUERY_A, args.timeout, True
        )
        report["profile_b"] = capture_plan(
            driver, QUERY_B, args.timeout, True
        )
        control_noise_fraction = max(
            a_a_control["median_delta_ms"], b_b_control["median_delta_ms"]
        ) / max(measurement["median_delta_ms"], 0.001)
        report["control_noise_fraction"] = control_noise_fraction
        report["validation_pass"] = bool(
            measurement["rows_equal"]
            and measurement["median_ratio"] >= 1.5
            and measurement["median_delta_ms"] >= 10.0
            and measurement["direction_consistency"] >= 0.75
            and control_noise_fraction <= 0.25
        )
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "validation_pass": report["validation_pass"],
                },
                sort_keys=True,
            )
        )
        return 0 if report["validation_pass"] else 2
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
