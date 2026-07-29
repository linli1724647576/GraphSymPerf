#!/usr/bin/env python3
"""Independently validate exact variable-length path-bound performance."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_sym_perf.core import to_typed_value


@dataclass(frozen=True)
class Pair:
    pair_id: str
    theorem: str
    count_a: str
    count_b: str
    rows_a: str
    rows_b: str


PAIRS = (
    Pair(
        "exact-one-hop",
        "A fixed one-hop pattern and an exact *1..1 pattern return the same endpoints",
        "MATCH (a:N)-[:R]->(b:N) RETURN count(*) AS value",
        "MATCH (a:N)-[:R*1..1]->(b:N) RETURN count(*) AS value",
        "MATCH (a:N)-[:R]->(b:N) RETURN a.id AS a, b.id AS b ORDER BY a, b",
        "MATCH (a:N)-[:R*1..1]->(b:N) RETURN a.id AS a, b.id AS b ORDER BY a, b",
    ),
    Pair(
        "exact-zero-hop",
        "An exact *0..0 pattern binds both endpoints to the same node",
        "MATCH (n:N) RETURN count(*) AS value",
        "MATCH (n:N)-[:R*0..0]->(m:N) RETURN count(*) AS value",
        "MATCH (n:N) RETURN n.id AS n, n.id AS m ORDER BY n, m",
        "MATCH (n:N)-[:R*0..0]->(m:N) RETURN n.id AS n, m.id AS m ORDER BY n, m",
    ),
)


def setup_graph(driver: Any, nodes: int, degree: int, noise_degree: int) -> None:
    statements: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
            {},
        ),
        ("DROP INDEX n_id IF EXISTS", {}),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { CREATE (:N {id: i}) } IN TRANSACTIONS OF 5000 ROWS",
            {"nodes": nodes},
        ),
        ("CREATE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id)", {}),
        ("CALL db.awaitIndexes(300)", {}),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { MATCH (a:N {id: i}) UNWIND range(1, $degree) AS offset "
            "MATCH (b:N {id: ((i + offset - 1) % $nodes) + 1}) "
            "CREATE (a)-[:R]->(b) } IN TRANSACTIONS OF 500 ROWS",
            {"nodes": nodes, "degree": degree},
        ),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { MATCH (a:N {id: i}) UNWIND range(1, $degree) AS offset "
            "MATCH (b:N {id: ((i + offset + $r_degree - 1) % $nodes) + 1}) "
            "CREATE (a)-[:S]->(b) } IN TRANSACTIONS OF 500 ROWS",
            {
                "nodes": nodes,
                "degree": noise_degree,
                "r_degree": degree,
            },
        ),
    )
    with driver.session(database="neo4j") as session:
        for statement, parameters in statements:
            session.run(statement, parameters).consume()


def collect(session: Any, language: str, query: str, timeout: int) -> tuple[Any, ...]:
    from neo4j import Query

    result = session.run(Query(f"CYPHER {language} {query}", timeout=timeout))
    rows = tuple(to_typed_value(record.data()) for record in result)
    result.consume()
    return rows


def timed(session: Any, language: str, query: str, timeout: int) -> float:
    started = time.perf_counter()
    collect(session, language, query, timeout)
    return (time.perf_counter() - started) * 1000.0


def measure_pair(
    driver: Any,
    pair: Pair,
    language: str,
    warmups: int,
    repetitions: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    rng = random.Random(f"{seed}:{pair.pair_id}:{language}")
    with driver.session(database="neo4j") as session:
        rows_a = collect(session, language, pair.rows_a, timeout)
        rows_b = collect(session, language, pair.rows_b, timeout)
        if rows_a != rows_b:
            raise AssertionError(
                f"{pair.pair_id} returned different complete endpoint rows"
            )
        for _ in range(warmups):
            timed(session, language, pair.count_a, timeout)
            timed(session, language, pair.count_b, timeout)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                elapsed[side] = timed(
                    session,
                    language,
                    pair.count_a if side == "A" else pair.count_b,
                    timeout,
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
    slower = "A" if median_a >= median_b else "B"
    direction_consistency = sum(
        1
        for sample in samples
        if (sample["a_ms"] >= sample["b_ms"]) == (slower == "A")
    ) / len(samples)
    return {
        "complete_endpoint_rows_equal": True,
        "endpoint_row_count": len(rows_a),
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": max(median_a, median_b)
        / max(min(median_a, median_b), 0.001),
        "median_delta_ms": abs(median_a - median_b),
        "slow_query": slower,
        "direction_consistency": direction_consistency,
        "samples": samples,
    }


def measure_control(
    driver: Any,
    query: str,
    language: str,
    repetitions: int,
    timeout: int,
) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    with driver.session(database="neo4j") as session:
        for _ in range(2):
            timed(session, language, query, timeout)
        for repetition in range(repetitions):
            first = timed(session, language, query, timeout)
            second = timed(session, language, query, timeout)
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


def serialize_plan(plan: Any) -> dict[str, Any] | None:
    if plan is None:
        return None
    if isinstance(plan, dict):
        return json.loads(json.dumps(plan, default=str))
    result: dict[str, Any] = {
        "operator_type": plan.operator_type,
        "identifiers": list(plan.identifiers),
        "arguments": {
            str(key): str(value) for key, value in plan.arguments.items()
        },
        "children": [serialize_plan(child) for child in plan.children],
    }
    for attribute in (
        "db_hits",
        "records",
        "time",
        "page_cache_hits",
        "page_cache_misses",
    ):
        if hasattr(plan, attribute):
            result[attribute] = getattr(plan, attribute)
    return result


def capture_plan(
    driver: Any, language: str, query: str, timeout: int, profile: bool
) -> dict[str, Any] | None:
    from neo4j import Query

    prefix = "PROFILE" if profile else "EXPLAIN"
    with driver.session(database="neo4j") as session:
        result = session.run(
            Query(f"CYPHER {language} {prefix} {query}", timeout=timeout)
        )
        tuple(result)
        summary = result.consume()
        return serialize_plan(summary.profile if profile else summary.plan)


def validate_threshold(measurement: dict[str, Any]) -> bool:
    return bool(
        measurement["slow_query"] == "B"
        and measurement["median_ratio"] >= 1.5
        and measurement["median_delta_ms"] >= 10.0
        and measurement["direction_consistency"] >= 0.75
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--nodes", type=int, default=20_000)
    parser.add_argument("--degree", type=int, default=4)
    parser.add_argument("--noise-degree", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--languages", default="25")
    parser.add_argument(
        "--pairs",
        default=",".join(pair.pair_id for pair in PAIRS),
    )
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
        "r_degree": args.degree,
        "s_degree": args.noise_degree,
        "languages": {},
    }
    languages = tuple(
        language.strip()
        for language in args.languages.split(",")
        if language.strip()
    )
    if not languages or any(language not in {"5", "25"} for language in languages):
        parser.error("--languages must contain only 5 and/or 25")
    requested_pair_ids = {
        pair_id.strip() for pair_id in args.pairs.split(",") if pair_id.strip()
    }
    selected_pairs = tuple(
        pair for pair in PAIRS if pair.pair_id in requested_pair_ids
    )
    if not selected_pairs or requested_pair_ids != {
        pair.pair_id for pair in selected_pairs
    }:
        parser.error("--pairs contains an unknown pair id")
    try:
        setup_graph(driver, args.nodes, args.degree, args.noise_degree)
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD versions, edition "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            report["environment"] = component.data()
        for language in languages:
            language_report: dict[str, Any] = {}
            report["languages"][language] = language_report
            for pair in selected_pairs:
                measurement = measure_pair(
                    driver,
                    pair,
                    language,
                    args.warmups,
                    args.repetitions,
                    args.timeout,
                    args.seed,
                )
                measurement["threshold_pass"] = validate_threshold(measurement)
                language_report[pair.pair_id] = {
                    "theorem": pair.theorem,
                    "query_a": pair.count_a,
                    "query_b": pair.count_b,
                    "measurement": measurement,
                    "a_a_control": measure_control(
                        driver,
                        pair.count_a,
                        language,
                        4,
                        args.timeout,
                    ),
                    "b_b_control": measure_control(
                        driver,
                        pair.count_b,
                        language,
                        4,
                        args.timeout,
                    ),
                    "explain_a": capture_plan(
                        driver, language, pair.count_a, args.timeout, False
                    ),
                    "explain_b": capture_plan(
                        driver, language, pair.count_b, args.timeout, False
                    ),
                    "profile_a": capture_plan(
                        driver, language, pair.count_a, args.timeout, True
                    ),
                    "profile_b": capture_plan(
                        driver, language, pair.count_b, args.timeout, True
                    ),
                }
        report["validation_pass"] = all(
            pair_report["measurement"]["threshold_pass"]
            for language_report in report["languages"].values()
            for pair_report in language_report.values()
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
