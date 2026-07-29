#!/usr/bin/env python3
"""Strictly validate significant Neo4j rule families on one fresh instance."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


def query_pairs(nodes: int) -> dict[str, dict[str, Any]]:
    limit = nodes // 2
    target = nodes // 2
    return {
        "order_before_limited_count_symmetry": {
            "query_a": (
                f"MATCH (n:N) WITH n LIMIT {limit} RETURN count(*) AS value"
            ),
            "query_b": (
                f"MATCH (n:N) WITH n ORDER BY n.id LIMIT {limit} "
                "RETURN count(*) AS value"
            ),
            "expected": limit,
            "theorem": (
                "ordering changes which limited rows survive but not how many survive"
            ),
        },
        "element_id_distinct_symmetry": {
            "query_a": (
                "MATCH (n:N) WITH elementId(n) AS id RETURN count(id) AS value"
            ),
            "query_b": (
                "MATCH (n:N) WITH DISTINCT elementId(n) AS id "
                "RETURN count(id) AS value"
            ),
            "expected": nodes,
            "theorem": "a node scan produces unique elementId values",
        },
        "union_all_count_pushdown_symmetry": {
            "query_a": (
                "CALL () { MATCH (n:N) RETURN n AS item UNION ALL "
                "MATCH (m:N) RETURN m AS item } RETURN count(item) AS value"
            ),
            "query_b": (
                "CALL () { MATCH (n:N) RETURN count(n) AS subtotal UNION ALL "
                "MATCH (m:N) RETURN count(m) AS subtotal } "
                "RETURN sum(subtotal) AS value"
            ),
            "expected": nodes * 2,
            "theorem": "count over UNION ALL equals the sum of the branch counts",
        },
        "conditional_count_filter_symmetry": {
            "query_a": (
                f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value"
            ),
            "query_b": (
                f"MATCH (n:N) RETURN count(CASE WHEN n.id = {target} "
                "THEN 1 END) AS value"
            ),
            "expected": 1,
            "theorem": (
                "a filtered count equals counting a nullable conditional expression"
            ),
        },
    }


def setup_graph(driver: Any, nodes: int) -> None:
    statements: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { CREATE (:N {id: i}) } IN TRANSACTIONS OF 5000 ROWS",
            {"nodes": nodes},
        ),
        ("CREATE RANGE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id)", {}),
        ("CALL db.awaitIndexes(300)", {}),
    )
    with driver.session(database="neo4j") as session:
        for statement, parameters in statements:
            session.run(statement, parameters).consume()


def execute(session: Any, query: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    started = time.perf_counter()
    try:
        result = session.run(Query(f"CYPHER 25 {query}", timeout=timeout))
        plain_rows: list[dict[str, Any]] = []
        typed_rows: list[Any] = []
        for record in result:
            data = record.data()
            plain_rows.append(data)
            typed_rows.append(to_typed_value(data))
        result.consume()
        return {
            "status": "ok",
            "plain_rows": plain_rows,
            "typed_rows": typed_rows,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "code", None),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }


def equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "error":
        return left.get("error_code") == right.get("error_code")
    comparison = compare_typed_results(
        tuple(left["typed_rows"]), tuple(right["typed_rows"]), ordered=False
    )
    if comparison.equivalent:
        return True
    left_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in left["typed_rows"]
    )
    right_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in right["typed_rows"]
    )
    return left_multiset == right_multiset


def measure_pair(
    driver: Any,
    query_a: str,
    query_b: str,
    warmups: int,
    repetitions: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        first_a = execute(session, query_a, timeout)
        first_b = execute(session, query_b, timeout)
        if not equivalent(first_a, first_b):
            return {"equivalent": False, "a": first_a, "b": first_b}
        for _ in range(warmups):
            execute(session, query_a, timeout)
            execute(session, query_b, timeout)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                outcome = execute(
                    session, query_a if side == "A" else query_b, timeout
                )
                if outcome["status"] != "ok":
                    return {
                        "equivalent": True,
                        "measurement_error": outcome,
                        "samples": samples,
                    }
                elapsed[side] = outcome["elapsed_ms"]
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
    slow_query = "A" if median_a >= median_b else "B"
    direction_consistency = sum(
        (sample["a_ms"] > sample["b_ms"]) == (slow_query == "A")
        for sample in samples
    ) / len(samples)
    slow = max(median_a, median_b)
    fast = min(median_a, median_b)
    return {
        "equivalent": True,
        "result_a": first_a["plain_rows"],
        "result_b": first_b["plain_rows"],
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": slow / max(fast, 0.001),
        "median_delta_ms": slow - fast,
        "slow_query": slow_query,
        "direction_consistency": direction_consistency,
        "samples": samples,
    }


def measure_control(
    driver: Any, query: str, repetitions: int, timeout: int
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for _ in range(2):
            execute(session, query, timeout)
        for repetition in range(repetitions):
            first = execute(session, query, timeout)["elapsed_ms"]
            second = execute(session, query, timeout)["elapsed_ms"]
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


def plan_to_dict(plan: Any) -> dict[str, Any]:
    if isinstance(plan, dict):
        return {
            str(key): (
                [plan_to_dict(child) for child in value]
                if key == "children"
                else value
            )
            for key, value in plan.items()
        }
    return {
        "operator_type": plan.operator_type,
        "arguments": dict(plan.arguments),
        "identifiers": list(plan.identifiers),
        "db_hits": getattr(plan, "db_hits", None),
        "rows": getattr(plan, "rows", None),
        "children": [plan_to_dict(child) for child in plan.children],
    }


def profile(driver: Any, query: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    with driver.session(database="neo4j") as session:
        result = session.run(Query(f"CYPHER 25 PROFILE {query}", timeout=timeout))
        list(result)
        summary = result.consume()
        return plan_to_dict(summary.profile)


def validate_case(
    driver: Any,
    pair: dict[str, Any],
    warmups: int,
    repetitions: int,
    controls: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    measurement = measure_pair(
        driver,
        pair["query_a"],
        pair["query_b"],
        warmups,
        repetitions,
        timeout,
        seed,
    )
    a_a = measure_control(driver, pair["query_a"], controls, timeout)
    b_b = measure_control(driver, pair["query_b"], controls, timeout)
    control_noise_fraction = max(
        a_a["median_delta_ms"], b_b["median_delta_ms"]
    ) / max(measurement.get("median_delta_ms", 0.001), 0.001)
    expected_rows = [{"value": pair["expected"]}]
    validation_pass = bool(
        measurement.get("equivalent")
        and measurement.get("result_a") == expected_rows
        and measurement.get("result_b") == expected_rows
        and measurement.get("median_ratio", 0.0) >= 1.5
        and measurement.get("median_delta_ms", 0.0) >= 10.0
        and measurement.get("direction_consistency", 0.0) >= 0.75
        and control_noise_fraction <= 0.25
    )
    return {
        **pair,
        "measurement": measurement,
        "a_a_control": a_a,
        "b_b_control": b_b,
        "control_noise_fraction": control_noise_fraction,
        "profile_a": profile(driver, pair["query_a"], timeout),
        "profile_b": profile(driver, pair["query_b"], timeout),
        "validation_pass": validation_pass,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--nodes", type=int, default=500_000)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--controls", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument(
        "--case",
        action="append",
        choices=tuple(query_pairs(2)),
        help="validate only the selected case; may be repeated",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.uri or not args.password:
        parser.error("set NEO4J_URI and NEO4J_PASSWORD")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    report: dict[str, Any] = {
        "schema_version": 1,
        "instance": args.instance,
        "dataset": {"n_nodes": args.nodes},
        "thresholds": {
            "minimum_ratio": 1.5,
            "minimum_delta_ms": 10.0,
            "minimum_direction_consistency": 0.75,
            "maximum_control_noise_fraction": 0.25,
        },
        "cases": {},
    }
    selected_cases = args.case or list(query_pairs(args.nodes))
    try:
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD versions, edition "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            report["environment"] = component.data()
        setup_graph(driver, args.nodes)
        all_pass = True
        for index, rule in enumerate(selected_cases):
            pair = query_pairs(args.nodes)[rule]
            result = validate_case(
                driver,
                pair,
                args.warmups,
                args.repetitions,
                args.controls,
                args.timeout,
                args.seed + index,
            )
            report["cases"][rule] = result
            all_pass = all_pass and result["validation_pass"]
        report["validation_pass"] = all_pass
    finally:
        driver.close()

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
            sort_keys=True,
        )
    )
    return 0 if report["validation_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
