#!/usr/bin/env python3
"""Screen non-overlapping Apache AGE logical and performance rule families."""

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


GRAPH = "age_rule_screen"


CASES = {
    "known-direction-vs-undirected": {
        "rule_id": "known_direction_undirected_symmetry",
        "query_a": "MATCH (:A)-[:R]->(:B) RETURN count(*) AS value",
        "query_b": "MATCH (:A)-[:R]-(:B) RETURN count(*) AS value",
        "theorem": "all R edges in this graph go from A to B",
    },
    "chain-pattern-vs-split-match": {
        "rule_id": "chain_match_staging_symmetry",
        "query_a": "MATCH (:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": (
            "MATCH (x:X)-[:P]->(y:Y) MATCH (y)-[:Q]->(z:Z) "
            "RETURN count(*) AS value"
        ),
        "theorem": "splitting a fixed two-hop chain at the bound middle node preserves rows",
    },
    "inline-property-vs-where": {
        "rule_id": "property_predicate_surface_symmetry",
        "query_a": "MATCH (n:N {score: 42420}) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "theorem": "an inline property equality equals the same WHERE predicate",
    },
    "equality-vs-singleton-in": {
        "rule_id": "singleton_in_symmetry",
        "query_a": "MATCH (n:N) WHERE n.grp = 42 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.grp IN [42] RETURN count(*) AS value",
        "theorem": "membership in a singleton constant list equals equality",
    },
    "or-vs-in": {
        "rule_id": "finite_disjunction_in_symmetry",
        "query_a": (
            "MATCH (n:N) WHERE n.grp = 42 OR n.grp = 43 RETURN count(*) AS value"
        ),
        "query_b": "MATCH (n:N) WHERE n.grp IN [42, 43] RETURN count(*) AS value",
        "theorem": "a finite equality disjunction equals membership in the same constants",
    },
    "add-zero-filter": {
        "rule_id": "arithmetic_additive_identity_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.score + 0 = 42420 RETURN count(*) AS value",
        "theorem": "adding zero preserves an integer property",
    },
    "multiply-one-filter": {
        "rule_id": "arithmetic_multiplicative_identity_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.score * 1 = 42420 RETURN count(*) AS value",
        "theorem": "multiplying by one preserves an integer property",
    },
    "equality-vs-closed-range": {
        "rule_id": "point_range_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:N) WHERE n.score >= 42420 AND n.score <= 42420 "
            "RETURN count(*) AS value"
        ),
        "theorem": "a closed range with equal bounds equals equality",
    },
    "with-passthrough": {
        "rule_id": "with_passthrough_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WITH n RETURN count(*) AS value",
        "theorem": "a pass-through WITH preserves the input rows",
    },
    "projection-staging": {
        "rule_id": "projection_staging_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) WITH n.grp AS g RETURN sum(g) AS value",
        "theorem": "aliasing an aggregate input through WITH preserves the aggregate",
    },
    "minimum-vs-sort-limit": {
        "rule_id": "minimum_sort_limit_symmetry",
        "query_a": "MATCH (n:N) RETURN min(n.score) AS value",
        "query_b": "MATCH (n:N) RETURN n.score AS value ORDER BY value LIMIT 1",
        "theorem": "minimum equals the first ascending non-null value",
    },
    "max-distinct-idempotence": {
        "rule_id": "max_distinct_idempotence",
        "query_a": "MATCH (n:N) RETURN max(n.grp) AS value",
        "query_b": "MATCH (n:N) RETURN max(DISTINCT n.grp) AS value",
        "theorem": "duplicate removal does not change MAX",
    },
    "distinct-aggregate-staging": {
        "rule_id": "distinct_aggregate_staging_symmetry",
        "query_a": "MATCH (n:N) RETURN count(DISTINCT n.grp) AS value",
        "query_b": "MATCH (n:N) WITH DISTINCT n.grp AS g RETURN count(*) AS value",
        "theorem": "counting distinct values equals counting a distinct projection",
    },
    "count-vs-sum-one": {
        "rule_id": "count_sum_one_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) RETURN sum(1) AS value",
        "theorem": "summing one over a non-empty input equals row count",
    },
    "case-partition-count": {
        "rule_id": "case_partition_count_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:N) RETURN "
            "sum(CASE WHEN n.grp < 48 THEN 1 ELSE 0 END) + "
            "sum(CASE WHEN n.grp >= 48 THEN 1 ELSE 0 END) AS value"
        ),
        "theorem": "two disjoint exhaustive CASE branches partition all rows",
    },
    "coalesce-vs-case": {
        "rule_id": "coalesce_case_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(coalesce(n.missing, 1)) AS value",
        "query_b": (
            "MATCH (n:N) RETURN "
            "sum(CASE WHEN n.missing IS NULL THEN 1 ELSE n.missing END) AS value"
        ),
        "theorem": "two-argument COALESCE equals the corresponding null CASE",
    },
    "relationship-reversal": {
        "rule_id": "relationship_direction_notation_symmetry",
        "query_a": "MATCH (a:A)-[:R]->(b:B) RETURN count(*) AS value",
        "query_b": "MATCH (b:B)<-[:R]-(a:A) RETURN count(*) AS value",
        "theorem": "reversing pattern notation while preserving edge direction preserves rows",
    },
    "relationship-count": {
        "rule_id": "bound_relationship_count_symmetry",
        "query_a": "MATCH (:A)-[r:R]->(:B) RETURN count(*) AS value",
        "query_b": "MATCH (:A)-[r:R]->(:B) RETURN count(r) AS value",
        "theorem": "a matched relationship variable is non-null",
    },
    "typed-relationship-type-guard": {
        "rule_id": "typed_relationship_type_guard_symmetry",
        "query_a": "MATCH (:X)-[r:P]->(:Y) RETURN count(*) AS value",
        "query_b": (
            "MATCH (:X)-[r:P]->(:Y) WHERE type(r) = 'P' "
            "RETURN count(*) AS value"
        ),
        "theorem": "a relationship matched with type P always has type P",
    },
    "bound-node-label-guard": {
        "rule_id": "bound_node_label_guard_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n:N RETURN count(*) AS value",
        "theorem": "a node matched with label N always has label N",
    },
    "is-not-null-vs-exists": {
        "rule_id": "property_existence_surface_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score IS NOT NULL RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE exists(n.score) RETURN count(*) AS value",
        "theorem": "all stored score properties are non-null",
    },
    "order-alias-vs-expression": {
        "rule_id": "order_alias_symmetry",
        "query_a": "MATCH (n:N) RETURN n.score AS value ORDER BY value LIMIT 100",
        "query_b": "MATCH (n:N) RETURN n.score AS value ORDER BY n.score LIMIT 100",
        "theorem": "an ORDER BY alias denotes the projected expression",
    },
    "chain-vs-comma-patterns": {
        "rule_id": "chain_comma_pattern_symmetry",
        "query_a": "MATCH (x:X)-[:P]->(y:Y)-[:Q]->(z:Z) RETURN count(*) AS value",
        "query_b": (
            "MATCH (x:X)-[:P]->(y:Y), (y)-[:Q]->(z:Z) "
            "RETURN count(*) AS value"
        ),
        "theorem": "a fixed chain equals comma-separated patterns joined at the same node",
    },
    "chain-vs-unused-path": {
        "rule_id": "unused_path_binding_symmetry",
        "query_a": "MATCH (:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "theorem": "binding an unused path does not change matched rows",
    },
    "chain-vs-redundant-length": {
        "rule_id": "fixed_path_length_guard_symmetry",
        "query_a": "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "WHERE length(p) = 2 RETURN count(*) AS value"
        ),
        "theorem": "a two-edge fixed path always has length two",
    },
    "anonymous-vs-bound-endpoints": {
        "rule_id": "unused_node_binding_symmetry",
        "query_a": "MATCH (:A)-[:R]->(:B) RETURN count(*) AS value",
        "query_b": "MATCH (a:A)-[:R]->(b:B) RETURN count(*) AS value",
        "theorem": "binding unused endpoint variables does not change matched rows",
    },
    "anonymous-vs-bound-relationship": {
        "rule_id": "unused_relationship_binding_symmetry",
        "query_a": "MATCH (:A)-[:R]->(:B) RETURN count(*) AS value",
        "query_b": "MATCH (:A)-[r:R]->(:B) RETURN count(*) AS value",
        "theorem": "binding an unused relationship variable does not change matched rows",
    },
    "constant-with-projection": {
        "rule_id": "unused_constant_projection_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WITH n, 1 AS marker RETURN count(*) AS value",
        "theorem": "an unused constant projection does not change rows",
    },
    "conjunction-order": {
        "rule_id": "predicate_conjunction_order_symmetry",
        "query_a": (
            "MATCH (n:N) WHERE n.score = 42420 AND n.grp = 31 "
            "RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (n:N) WHERE n.grp = 31 AND n.score = 42420 "
            "RETURN count(*) AS value"
        ),
        "theorem": "conjunction order does not change boolean selection",
    },
    "disjunction-order": {
        "rule_id": "predicate_disjunction_order_symmetry",
        "query_a": (
            "MATCH (n:N) WHERE n.score = 42420 OR n.grp = 31 "
            "RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (n:N) WHERE n.grp = 31 OR n.score = 42420 "
            "RETURN count(*) AS value"
        ),
        "theorem": "disjunction order does not change boolean selection",
    },
    "property-map-key-order": {
        "rule_id": "property_map_order_symmetry",
        "query_a": "MATCH (n:N {score: 42420, grp: 31}) RETURN count(*) AS value",
        "query_b": "MATCH (n:N {grp: 31, score: 42420}) RETURN count(*) AS value",
        "theorem": "property-map key order does not change a pattern predicate",
    },
    "scalar-vs-list-subscript": {
        "rule_id": "singleton_list_subscript_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) RETURN sum([n.grp][0]) AS value",
        "theorem": "index zero of a singleton list equals its element",
    },
    "scalar-vs-head-singleton": {
        "rule_id": "singleton_list_head_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) RETURN sum(head([n.grp])) AS value",
        "theorem": "the head of a singleton list equals its element",
    },
    "null-vs-not-exists": {
        "rule_id": "missing_property_null_symmetry",
        "query_a": "MATCH (n:N) WHERE n.missing IS NULL RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE NOT exists(n.missing) RETURN count(*) AS value",
        "theorem": "the absent property is null and does not exist",
    },
    "equality-commutation": {
        "rule_id": "equality_commutation_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE 42420 = n.score RETURN count(*) AS value",
        "theorem": "equality is commutative",
    },
    "inequality-negation": {
        "rule_id": "inequality_negation_symmetry",
        "query_a": "MATCH (n:N) WHERE n.score = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE NOT (n.score <> 42420) RETURN count(*) AS value",
        "theorem": "for a non-null integer, negated inequality equals equality",
    },
    "nested-coalesce": {
        "rule_id": "coalesce_association_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(coalesce(n.missing, n.grp, 1)) AS value",
        "query_b": (
            "MATCH (n:N) RETURN sum(coalesce(coalesce(n.missing, n.grp), 1)) AS value"
        ),
        "theorem": "nested two-argument COALESCE equals the three-argument form",
    },
    "sum-distinct-staging": {
        "rule_id": "sum_distinct_staging_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(DISTINCT n.grp) AS value",
        "query_b": "MATCH (n:N) WITH DISTINCT n.grp AS g RETURN sum(g) AS value",
        "theorem": "SUM DISTINCT equals SUM over the same distinct projection",
    },
    "path-length-vs-relationship-size": {
        "rule_id": "path_relationship_size_symmetry",
        "query_a": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN sum(length(p)) AS value"
        ),
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "RETURN sum(size(relationships(p))) AS value"
        ),
        "theorem": "path length equals the size of its relationship list",
    },
    "path-node-size": {
        "rule_id": "path_node_size_symmetry",
        "query_a": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "RETURN sum(length(p) + 1) AS value"
        ),
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "RETURN sum(size(nodes(p))) AS value"
        ),
        "theorem": "a non-empty path has one more node than relationships",
    },
    "path-head-node-guard": {
        "rule_id": "path_head_node_symmetry",
        "query_a": (
            "MATCH p=(x:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH p=(x:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "WHERE head(nodes(p)) = x RETURN count(*) AS value"
        ),
        "theorem": "the head of nodes(path) is the path start node",
    },
    "path-last-node-guard": {
        "rule_id": "path_last_node_symmetry",
        "query_a": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(z:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(z:Z) "
            "WHERE last(nodes(p)) = z RETURN count(*) AS value"
        ),
        "theorem": "the last element of nodes(path) is the path end node",
    },
    "sum-addition-distribution": {
        "rule_id": "sum_addition_distribution_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp + 1) AS value",
        "query_b": "MATCH (n:N) RETURN sum(n.grp) + count(*) AS value",
        "theorem": "SUM(x + 1) equals SUM(x) plus row count for non-null integers",
    },
    "max-addition-translation": {
        "rule_id": "max_addition_translation_symmetry",
        "query_a": "MATCH (n:N) RETURN max(n.grp + 1) AS value",
        "query_b": "MATCH (n:N) RETURN max(n.grp) + 1 AS value",
        "theorem": "adding a constant commutes with MAX over non-null integers",
    },
    "min-negation-duality": {
        "rule_id": "min_max_negation_symmetry",
        "query_a": "MATCH (n:N) RETURN min(-n.grp) AS value",
        "query_b": "MATCH (n:N) RETURN -max(n.grp) AS value",
        "theorem": "minimum after negation equals the negation of maximum",
    },
}


def sql_for(query: str) -> str:
    tag = "$age_screen$"
    return (
        f"SELECT * FROM ag_catalog.cypher('{GRAPH}', {tag}{query}{tag}) "
        'AS ("value" agtype)'
    )


def execute(cursor: Any, query: str) -> tuple[list[list[str]], float]:
    started = time.perf_counter()
    cursor.execute(sql_for(query))
    rows = [[str(value) for value in row] for row in cursor.fetchall()]
    return rows, (time.perf_counter() - started) * 1000.0


def setup(cursor: Any, node_count: int, edge_count: int, chain_count: int) -> None:
    cursor.execute(
        "SELECT ag_catalog.drop_graph(%s, true) "
        "WHERE EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s)",
        (GRAPH, GRAPH),
    )
    cursor.execute("SELECT ag_catalog.create_graph(%s)", (GRAPH,))
    execute(
        cursor,
        f"UNWIND range(1, {node_count}) AS i "
        "CREATE (:N {id: i, score: i, grp: i % 97}) RETURN count(*) AS value",
    )
    execute(
        cursor,
        f"UNWIND range(1, {edge_count}) AS i "
        "CREATE (:A {id: i})-[:R {id: i}]->(:B {id: i}) "
        "RETURN count(*) AS value",
    )
    execute(
        cursor,
        f"UNWIND range(1, {chain_count}) AS i "
        "CREATE (:X {id: i})-[:P {id: i}]->(:Y {id: i})-[:Q {id: i}]->(:Z {id: i}) "
        "RETURN count(*) AS value",
    )
    for label in ("N", "A", "B", "R", "X", "Y", "Z", "P", "Q"):
        cursor.execute(f'ANALYZE {GRAPH}."{label}"')


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
    slow_side = "B" if median_b >= median_a else "A"
    fast = min(median_a, median_b)
    slow = max(median_a, median_b)
    return {
        "rows_equal": rows_a == rows_b,
        "rows_a": rows_a,
        "rows_b": rows_b,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "slow_side": slow_side,
        "median_ratio": slow / max(fast, 0.001),
        "median_delta_ms": slow - fast,
        "direction_consistency": sum(
            (sample["b_ms"] > sample["a_ms"]) == (slow_side == "B")
            for sample in samples
        )
        / len(samples),
        "samples": samples,
    }


def explain(cursor: Any, query: str) -> list[str]:
    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql_for(query)}")
    return [row[0] for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("AGE_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--edges", type=int, default=200)
    parser.add_argument("--chains", type=int, default=10_000)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set AGE_PASSWORD or pass --password")

    records: list[dict[str, Any]] = []
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
            cursor.execute("SET statement_timeout = '60s'")
            cursor.execute("SELECT version()")
            postgres_version = cursor.fetchone()[0]
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'age'")
            age_version = cursor.fetchone()[0]
            cursor.execute("SHOW jit")
            jit = cursor.fetchone()[0]
            setup(cursor, args.nodes, args.edges, args.chains)
            for index, (case_id, case) in enumerate(CASES.items()):
                record: dict[str, Any] = {
                    "case_id": case_id,
                    **case,
                    "postgres_version": postgres_version,
                    "age_version": age_version,
                    "jit": jit,
                    "dataset": {
                        "nodes": args.nodes,
                        "directed_edges": args.edges,
                        "chains": args.chains,
                    },
                }
                try:
                    measurement = measure_pair(
                        cursor,
                        case["query_a"],
                        case["query_b"],
                        args.warmups,
                        args.repetitions,
                        args.seed + index,
                    )
                    lead = bool(
                        not measurement["rows_equal"]
                        or (
                            measurement["median_ratio"] >= 1.5
                            and measurement["median_delta_ms"] >= 5.0
                            and measurement["direction_consistency"] >= 0.75
                        )
                    )
                    record.update(status="ok", measurement=measurement, lead=lead)
                    if lead:
                        record["profile_a"] = explain(cursor, case["query_a"])
                        record["profile_b"] = explain(cursor, case["query_b"])
                except Exception as exc:
                    record.update(status="error", error=str(exc), lead=False)
                records.append(record)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "leads": [record["case_id"] for record in records if record.get("lead")],
                "errors": [record["case_id"] for record in records if record["status"] == "error"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
