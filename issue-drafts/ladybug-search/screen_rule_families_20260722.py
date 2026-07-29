#!/usr/bin/env python3
"""Screen new LadybugDB rule families with ordinary execute timings."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

import ladybug


CASES = {
    "equality-commutation": {
        "rule_id": "equality_commutation_symmetry",
        "query_a": "MATCH (n:N) WHERE n.id = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE 42420 = n.id RETURN count(*) AS value",
        "theorem": "equality is commutative",
    },
    "equality-singleton-in": {
        "rule_id": "singleton_in_symmetry",
        "query_a": "MATCH (n:N) WHERE n.id = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.id IN [42420] RETURN count(*) AS value",
        "theorem": "membership in a singleton constant list equals equality",
    },
    "equality-point-range": {
        "rule_id": "point_range_symmetry",
        "query_a": "MATCH (n:N) WHERE n.id = 42420 RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:N) WHERE n.id >= 42420 AND n.id <= 42420 "
            "RETURN count(*) AS value"
        ),
        "theorem": "a closed integer range with equal bounds equals equality",
    },
    "or-versus-in": {
        "rule_id": "finite_disjunction_in_symmetry",
        "query_a": (
            "MATCH (n:N) WHERE n.grp = 42 OR n.grp = 43 RETURN count(*) AS value"
        ),
        "query_b": "MATCH (n:N) WHERE n.grp IN [42, 43] RETURN count(*) AS value",
        "theorem": "a finite equality disjunction equals membership in the same values",
    },
    "add-zero-filter": {
        "rule_id": "arithmetic_additive_identity_symmetry",
        "query_a": "MATCH (n:N) WHERE n.id = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.id + 0 = 42420 RETURN count(*) AS value",
        "theorem": "adding zero preserves an integer primary key",
    },
    "multiply-one-filter": {
        "rule_id": "arithmetic_multiplicative_identity_symmetry",
        "query_a": "MATCH (n:N) WHERE n.id = 42420 RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.id * 1 = 42420 RETURN count(*) AS value",
        "theorem": "multiplying by one preserves an integer primary key",
    },
    "static-label-guard": {
        "rule_id": "static_label_guard_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE label(n) = 'N' RETURN count(*) AS value",
        "theorem": "a node matched from N has label N",
    },
    "static-labels-guard": {
        "rule_id": "static_labels_guard_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE labels(n) = 'N' RETURN count(*) AS value",
        "theorem": "labels of a node matched from N are N",
    },
    "primary-key-not-null-guard": {
        "rule_id": "primary_key_not_null_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.id IS NOT NULL RETURN count(*) AS value",
        "theorem": "a primary key is non-null",
    },
    "primary-key-reflexive-guard": {
        "rule_id": "primary_key_reflexivity_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE n.id = n.id RETURN count(*) AS value",
        "theorem": "a non-null primary key equals itself",
    },
    "internal-id-reflexive-guard": {
        "rule_id": "internal_id_reflexivity_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WHERE id(n) = id(n) RETURN count(*) AS value",
        "theorem": "a matched node identifier equals itself",
    },
    "with-passthrough": {
        "rule_id": "with_passthrough_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WITH n RETURN count(*) AS value",
        "theorem": "a pass-through WITH preserves rows",
    },
    "dead-order-before-count": {
        "rule_id": "aggregate_order_elimination_symmetry",
        "query_a": "MATCH (n:N) WITH n RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WITH n ORDER BY n.id RETURN count(*) AS value",
        "theorem": "input order does not change row count",
    },
    "dead-order-before-sum": {
        "rule_id": "sum_order_elimination_symmetry",
        "query_a": "MATCH (n:N) WITH n RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) WITH n ORDER BY n.id RETURN sum(n.grp) AS value",
        "theorem": "input order does not change an exact integer sum",
    },
    "dead-order-before-min": {
        "rule_id": "min_order_elimination_symmetry",
        "query_a": "MATCH (n:N) WITH n RETURN min(n.grp) AS value",
        "query_b": "MATCH (n:N) WITH n ORDER BY n.id RETURN min(n.grp) AS value",
        "theorem": "input order does not change minimum",
    },
    "unused-constant-projection": {
        "rule_id": "unused_constant_projection_symmetry",
        "query_a": "MATCH (n:N) WITH n RETURN count(*) AS value",
        "query_b": "MATCH (n:N) WITH n, 1 AS marker RETURN count(*) AS value",
        "theorem": "an unused constant projection does not change rows",
    },
    "unused-expression-projection": {
        "rule_id": "unused_expression_projection_symmetry",
        "query_a": "MATCH (n:N) WITH n RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:N) WITH n, n.id * n.id AS marker RETURN count(*) AS value"
        ),
        "theorem": "an unused expression does not change rows",
    },
    "primary-key-distinct": {
        "rule_id": "primary_key_distinct_symmetry",
        "query_a": "MATCH (n:N) RETURN count(n.id) AS value",
        "query_b": "MATCH (n:N) RETURN count(DISTINCT n.id) AS value",
        "theorem": "primary-key values are distinct",
    },
    "max-distinct": {
        "rule_id": "max_distinct_idempotence",
        "query_a": "MATCH (n:N) RETURN max(n.grp) AS value",
        "query_b": "MATCH (n:N) RETURN max(DISTINCT n.grp) AS value",
        "theorem": "duplicate removal does not change maximum",
    },
    "projection-staging": {
        "rule_id": "projection_staging_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": "MATCH (n:N) WITH n.grp AS g RETURN sum(g) AS value",
        "theorem": "aliasing an aggregate input preserves the aggregate",
    },
    "true-case-fold": {
        "rule_id": "constant_case_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.grp) AS value",
        "query_b": (
            "MATCH (n:N) RETURN sum(CASE WHEN true THEN n.grp ELSE 0 END) AS value"
        ),
        "theorem": "a true CASE branch equals its result expression",
    },
    "coalesce-primary-key": {
        "rule_id": "primary_key_coalesce_symmetry",
        "query_a": "MATCH (n:N) RETURN sum(n.id) AS value",
        "query_b": "MATCH (n:N) RETURN sum(coalesce(n.id, 0)) AS value",
        "theorem": "coalescing a non-null primary key preserves it",
    },
    "chain-comma-pattern": {
        "rule_id": "chain_comma_pattern_symmetry",
        "query_a": (
            "MATCH (x:X)-[:P]->(y:Y)-[:Q]->(z:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (x:X)-[:P]->(y:Y), (y)-[:Q]->(z:Z) RETURN count(*) AS value"
        ),
        "theorem": "a fixed chain equals comma-separated patterns joined at y",
    },
    "chain-split-match": {
        "rule_id": "chain_match_staging_symmetry",
        "query_a": (
            "MATCH (x:X)-[:P]->(y:Y)-[:Q]->(z:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH (x:X)-[:P]->(y:Y) MATCH (y)-[:Q]->(z:Z) "
            "RETURN count(*) AS value"
        ),
        "theorem": "splitting a fixed chain at a bound node preserves rows",
    },
    "unused-path-binding": {
        "rule_id": "unused_path_binding_symmetry",
        "query_a": "MATCH (:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "theorem": "binding an unused path preserves rows",
    },
    "fixed-path-length-guard": {
        "rule_id": "fixed_path_length_guard_symmetry",
        "query_a": "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "WHERE length(p) = 2 RETURN count(*) AS value"
        ),
        "theorem": "a fixed two-edge path has length two",
        "cross_db_family": "Apache AGE draft already exists",
    },
    "path-node-count-guard": {
        "rule_id": "fixed_path_node_count_symmetry",
        "query_a": "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value",
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "WHERE size(nodes(p)) = 3 RETURN count(*) AS value"
        ),
        "theorem": "a fixed two-edge path contains three nodes",
        "cross_db_family": "same path-invariant fix scope as Apache AGE draft",
    },
    "path-first-node-guard": {
        "rule_id": "path_first_node_symmetry",
        "query_a": (
            "MATCH p=(x:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH p=(x:X)-[:P]->(:Y)-[:Q]->(:Z) "
            "WHERE nodes(p)[1] = x RETURN count(*) AS value"
        ),
        "theorem": "the first node in a path is its bound start node",
        "cross_db_family": "same path-invariant fix scope as Apache AGE draft",
    },
    "path-last-node-guard": {
        "rule_id": "path_last_node_symmetry",
        "query_a": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(z:Z) RETURN count(*) AS value"
        ),
        "query_b": (
            "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(z:Z) "
            "WHERE nodes(p)[3] = z RETURN count(*) AS value"
        ),
        "theorem": "the last node in a fixed path is its bound end node",
        "cross_db_family": "same path-invariant fix scope as Apache AGE draft",
    },
    "relationship-reversal": {
        "rule_id": "relationship_direction_notation_symmetry",
        "query_a": "MATCH (a:A)-[:R]->(b:B) RETURN count(*) AS value",
        "query_b": "MATCH (b:B)<-[:R]-(a:A) RETURN count(*) AS value",
        "theorem": "reversing notation while preserving direction preserves rows",
    },
    "optional-false-pattern": {
        "rule_id": "optional_false_pattern_symmetry",
        "query_a": "MATCH (n:N) RETURN count(*) AS value",
        "query_b": (
            "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) WHERE false "
            "RETURN count(*) AS value"
        ),
        "theorem": "an optional pattern rejected by false preserves each input row",
    },
}


def execute(connection: Any, query: str) -> tuple[list[list[Any]], float]:
    started = time.perf_counter()
    result = connection.execute(query).get_all()
    return result, (time.perf_counter() - started) * 1000.0


def setup(connection: Any, nodes: int, edges: int, chains: int) -> None:
    connection.execute(
        "CREATE NODE TABLE N(id INT64, score INT64, grp INT64, nullable INT64, "
        "PRIMARY KEY(id))"
    )
    connection.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE X(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE Y(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE Z(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE REL TABLE R(FROM A TO B, id INT64)")
    connection.execute("CREATE REL TABLE P(FROM X TO Y, id INT64)")
    connection.execute("CREATE REL TABLE Q(FROM Y TO Z, id INT64)")
    connection.execute("CREATE REL TABLE S(FROM N TO N, id INT64)")
    connection.execute(
        f"UNWIND range(1, {nodes}) AS i "
        "CREATE (:N {id:i, score:i, grp:i % 97, "
        "nullable: CASE WHEN i % 2 = 0 THEN NULL ELSE i END})"
    )
    connection.execute(
        f"UNWIND range(1, {edges}) AS i "
        "CREATE (:A {id:i})-[:R {id:i}]->(:B {id:i})"
    )
    connection.execute(
        f"UNWIND range(1, {chains}) AS i "
        "CREATE (:X {id:i})-[:P {id:i}]->(:Y {id:i})-[:Q {id:i}]->(:Z {id:i})"
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
            _, elapsed[side] = execute(
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
    slow_side = "A" if median_a >= median_b else "B"
    slow = max(median_a, median_b)
    fast = min(median_a, median_b)
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
            (sample["a_ms"] > sample["b_ms"]) == (slow_side == "A")
            for sample in samples
        )
        / len(samples),
        "samples": samples,
    }


def explain(connection: Any, query: str) -> str:
    result = connection.execute(f"EXPLAIN {query}").get_all()
    return "\n".join(str(row[0]) for row in result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--edges", type=int, default=10_000)
    parser.add_argument("--chains", type=int, default=10_000)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database.exists():
        args.database.unlink()
    database = ladybug.Database(str(args.database))
    connection = ladybug.Connection(database)
    setup(connection, args.nodes, args.edges, args.chains)
    records: list[dict[str, Any]] = []
    for index, (case_id, case) in enumerate(CASES.items()):
        record: dict[str, Any] = {
            "case_id": case_id,
            **case,
            "ladybug_version": ladybug.__version__,
            "dataset": {
                "nodes": args.nodes,
                "directed_edges": args.edges,
                "chains": args.chains,
            },
        }
        try:
            measurement = measure_pair(
                connection,
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
                    and measurement["median_delta_ms"] >= 2.0
                    and measurement["direction_consistency"] >= 0.75
                )
            )
            record.update(status="ok", measurement=measurement, lead=lead)
            if lead:
                record["plan_a"] = explain(connection, case["query_a"])
                record["plan_b"] = explain(connection, case["query_b"])
        except Exception as exc:
            record.update(status="error", error=str(exc), lead=False)
        records.append(record)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, default=str) for record in records)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "version": ladybug.__version__,
                "output": str(args.output),
                "leads": [record["case_id"] for record in records if record["lead"]],
                "errors": [
                    record["case_id"]
                    for record in records
                    if record["status"] == "error"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
