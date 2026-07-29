#!/usr/bin/env python3
"""Targeted Neo4j 2026.06 metamorphic search for correctness and runtime gaps."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from dataclasses import dataclass
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


@dataclass(frozen=True)
class Pair:
    name: str
    theorem: str
    query_a: str
    query_b: str


BASE = "a.id = b.id AND a.id = 1000"
PAIRS = (
    Pair(
        "equality_operand_order",
        "equality is commutative",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
        "MATCH (n:N) WHERE 1000 = n.id RETURN count(*) AS c",
    ),
    Pair(
        "property_map_vs_where",
        "a static pattern property is the same equality predicate",
        "MATCH (n:N {id: 1000}) RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "singleton_in_vs_equality",
        "membership in a singleton list is equality",
        "MATCH (n:N) WHERE n.id IN [1000] RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "equality_vs_closed_range",
        "for an integer x, x=c iff x>=c and x<=c",
        "MATCH (n:N) WHERE n.id >= 1000 AND n.id <= 1000 RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "double_negation_equality",
        "for a non-null property, NOT NOT P = P",
        "MATCH (n:N) WHERE NOT NOT n.id = 1000 RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "negated_inequality",
        "for a non-null property, NOT (x <> c) = (x = c)",
        "MATCH (n:N) WHERE NOT n.id <> 1000 RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "redundant_not_null",
        "x=c implies x IS NOT NULL",
        "MATCH (n:N) WHERE n.id = 1000 AND n.id IS NOT NULL RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "duplicate_equality",
        "P AND P = P",
        "MATCH (n:N) WHERE n.id = 1000 AND n.id = 1000 RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "case_boolean_wrapper",
        "on a non-null property, CASE reconstructs the equality predicate",
        "MATCH (n:N) WHERE CASE WHEN n.id = 1000 THEN true ELSE false END RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "coalesce_boolean_wrapper",
        "on a non-null property, coalesce(P,false) = P",
        "MATCH (n:N) WHERE coalesce(n.id = 1000, false) RETURN count(*) AS c",
        "MATCH (n:N) WHERE n.id = 1000 RETURN count(*) AS c",
    ),
    Pair(
        "predicate_order_index_join",
        "pure conjunction terms commute",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        "MATCH (a:N), (b:N) WHERE a.id = 1000 AND a.id = b.id RETURN count(*) AS c",
    ),
    Pair(
        "constant_true_tautology",
        "P OR true is true",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        f"MATCH (a:N), (b:N) WHERE ({BASE}) AND (b.flag OR true) RETURN count(*) AS c",
    ),
    Pair(
        "constant_false_tautology",
        "NOT (P AND false) is true",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        f"MATCH (a:N), (b:N) WHERE ({BASE}) AND NOT (b.flag AND false) RETURN count(*) AS c",
    ),
    Pair(
        "null_partition_tautology",
        "P IS NULL OR P IS NOT NULL is true",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        f"MATCH (a:N), (b:N) WHERE ({BASE}) AND (b.nullable IS NULL OR b.nullable IS NOT NULL) RETURN count(*) AS c",
    ),
    Pair(
        "null_contradiction_tautology",
        "NOT (P IS NULL AND P IS NOT NULL) is true",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        f"MATCH (a:N), (b:N) WHERE ({BASE}) AND NOT (b.nullable IS NULL AND b.nullable IS NOT NULL) RETURN count(*) AS c",
    ),
    Pair(
        "excluded_middle_tautology",
        "P OR NOT P OR P IS NULL is true in three-valued logic",
        f"MATCH (a:N), (b:N) WHERE {BASE} RETURN count(*) AS c",
        f"MATCH (a:N), (b:N) WHERE ({BASE}) AND (b.flag OR NOT b.flag OR b.flag IS NULL) RETURN count(*) AS c",
    ),
    Pair(
        "label_order",
        "static label conjunction commutes",
        "MATCH (n:N:Mid) RETURN count(*) AS c",
        "MATCH (n:Mid:N) RETURN count(*) AS c",
    ),
    Pair(
        "is_labeled_vs_pattern",
        "IS LABELED and a static label predicate denote the same test",
        "MATCH (n:Rare) RETURN count(*) AS c",
        "MATCH (n) WHERE n IS LABELED Rare RETURN count(*) AS c",
    ),
    Pair(
        "is_not_labeled_vs_colon",
        "IS NOT LABELED and negated static label predicate denote the same test",
        "MATCH (n) WHERE NOT n:Rare RETURN count(*) AS c",
        "MATCH (n) WHERE n IS NOT LABELED Rare RETURN count(*) AS c",
    ),
    Pair(
        "for_vs_unwind",
        "FOR value IN list is the GQL alternative syntax for UNWIND",
        "UNWIND range(1, 200000) AS x RETURN sum(x % 17) AS s",
        "FOR x IN range(1, 200000) RETURN sum(x % 17) AS s",
    ),
    Pair(
        "let_vs_with_star",
        "LET binds an expression while retaining the current row",
        "MATCH (n:N) WITH *, n.id % 17 AS bucket RETURN sum(bucket) AS s",
        "MATCH (n:N) LET bucket = n.id % 17 RETURN sum(bucket) AS s",
    ),
    Pair(
        "filter_vs_with_where",
        "FILTER is the GQL filtering-clause alternative to WITH * WHERE",
        "MATCH (n:N) WITH * WHERE n.id % 97 = 0 RETURN count(*) AS c",
        "MATCH (n:N) FILTER n.id % 97 = 0 RETURN count(*) AS c",
    ),
)


def collect(session: Any, query: str, timeout_seconds: int) -> tuple[tuple[Any, ...], float]:
    from neo4j import Query

    started = time.perf_counter()
    result = session.run(Query(query, timeout=timeout_seconds))
    rows = tuple(to_typed_value(record.data()) for record in result)
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def setup_graph(driver: Any, nodes: int) -> None:
    with driver.session(database="neo4j") as session:
        session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS").consume()
        for statement in (
            "DROP INDEX n_id IF EXISTS",
            "DROP INDEX mid_group IF EXISTS",
            f"UNWIND range(1, {nodes}) AS i CALL (i) {{ CREATE (:N {{id:i, grp:i % 1000, flag: CASE WHEN i % 3 = 0 THEN null ELSE i % 2 = 0 END, nullable: CASE WHEN i % 5 = 0 THEN null ELSE i END}}) }} IN TRANSACTIONS OF 10000 ROWS",
            "MATCH (n:N) WHERE n.id % 10 = 0 CALL (n) { SET n:Mid } IN TRANSACTIONS OF 10000 ROWS",
            "MATCH (n:N) WHERE n.id % 1000 = 0 CALL (n) { SET n:Rare } IN TRANSACTIONS OF 10000 ROWS",
            "CREATE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id)",
            "CREATE INDEX mid_group IF NOT EXISTS FOR (n:Mid) ON (n.grp)",
            "CALL db.awaitIndexes(300)",
        ):
            session.run(statement).consume()


def measure_pair(
    driver: Any,
    pair: Pair,
    *,
    warmups: int,
    repetitions: int,
    timeout_seconds: int,
    seed: int,
) -> dict[str, Any]:
    with driver.session(database="neo4j") as session:
        rows_a, _ = collect(session, pair.query_a, timeout_seconds)
        rows_b, _ = collect(session, pair.query_b, timeout_seconds)
        equivalence = compare_typed_results(rows_a, rows_b, ordered=False)
        for _ in range(warmups):
            collect(session, pair.query_a, timeout_seconds)
            collect(session, pair.query_b, timeout_seconds)

        rng = random.Random(f"{seed}:{pair.name}")
        samples: list[dict[str, Any]] = []
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for name in order:
                _rows, elapsed[name] = collect(
                    session,
                    pair.query_a if name == "A" else pair.query_b,
                    timeout_seconds,
                )
            samples.append(
                {
                    "repetition": repetition + 1,
                    "order": "".join(order),
                    "a_ms": elapsed["A"],
                    "b_ms": elapsed["B"],
                }
            )
    a_values = [sample["a_ms"] for sample in samples]
    b_values = [sample["b_ms"] for sample in samples]
    med_a = statistics.median(a_values)
    med_b = statistics.median(b_values)
    slow = max(med_a, med_b)
    fast = min(med_a, med_b)
    return {
        "name": pair.name,
        "theorem": pair.theorem,
        "equivalent": equivalence.equivalent,
        "complete_result": equivalence.complete_result,
        "a_rows": len(rows_a),
        "b_rows": len(rows_b),
        "a_preview": rows_a[:3],
        "b_preview": rows_b[:3],
        "median_a_ms": med_a,
        "median_b_ms": med_b,
        "slow_query": "A" if med_a >= med_b else "B",
        "median_ratio": slow / max(fast, 0.001),
        "median_delta_ms": slow - fast,
        "direction_consistency": sum(
            1
            for sample in samples
            if (sample["a_ms"] >= sample["b_ms"]) == (med_a >= med_b)
        )
        / len(samples),
        "samples": samples,
        "query_a": pair.query_a,
        "query_b": pair.query_b,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=200_000)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260718)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    try:
        setup_graph(driver, args.nodes)
        with driver.session(database="neo4j") as session:
            version = session.run(
                "CALL dbms.components() YIELD versions, edition RETURN versions[0] AS version, edition"
            ).single().data()
        print(json.dumps({"environment": {**version, "nodes": args.nodes}}, sort_keys=True))
        for pair in PAIRS:
            try:
                report = measure_pair(
                    driver,
                    pair,
                    warmups=args.warmups,
                    repetitions=args.repetitions,
                    timeout_seconds=args.timeout,
                    seed=args.seed,
                )
            except Exception as exc:
                report = {
                    "name": pair.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "query_a": pair.query_a,
                    "query_b": pair.query_b,
                }
            print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
