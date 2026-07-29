#!/usr/bin/env python3
"""Screen independent AgensGraph logical and performance rule families."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg


GRAPH = "agens_rule_screen"


@dataclass(frozen=True)
class Case:
    name: str
    rule: str
    kind: str
    claim: str
    query_a: str
    query_b: str
    performance: bool = False


def cases(target: int) -> tuple[Case, ...]:
    return (
        Case(
            "zero-hop-node-equivalence",
            "zero_hop_path_symmetry",
            "SYM",
            "An exact zero-hop path returns the same node pairs as a node match",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N)-[:R*0..0]->(m:N) RETURN count(*) AS value",
            True,
        ),
        Case(
            "fixed-vs-exact-one-hop",
            "fixed_varlength_path_symmetry",
            "SYM",
            "A fixed relationship and an exact one-hop range return the same endpoints",
            "MATCH (:RA)-[:R]->(:RB) RETURN count(*) AS value",
            "MATCH (:RA)-[:R*1..1]->(:RB) RETURN count(*) AS value",
            True,
        ),
        Case(
            "relationship-direction-reversal",
            "relationship_direction_symmetry",
            "SYM",
            "Reversing the written pattern and arrow together preserves endpoint rows",
            "MATCH (a:RA)-[:R]->(b:RB) RETURN sum(a.id), sum(b.id), count(*)",
            "MATCH (b:RB)<-[:R]-(a:RA) RETURN sum(a.id), sum(b.id), count(*)",
        ),
        Case(
            "typed-edge-vs-type-filter",
            "relationship_type_surface_symmetry",
            "SYM",
            "A static edge label equals filtering the generic edge scan by type",
            "MATCH ()-[r:R]->() RETURN count(*) AS value",
            "MATCH ()-[r]->() WHERE type(r) = 'r' RETURN count(*) AS value",
            True,
        ),
        Case(
            "inline-property-vs-where",
            "property_predicate_placement_symmetry",
            "SYM",
            "An inline property predicate equals the same WHERE equality",
            f"MATCH (n:N {{id: {target}}}) RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            True,
        ),
        Case(
            "equality-vs-singleton-in",
            "singleton_membership_symmetry",
            "SYM",
            "Equality equals membership in a singleton list",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n.id IN [{target}] RETURN count(*) AS value",
            True,
        ),
        Case(
            "where-vs-with-where",
            "with_filter_placement_symmetry",
            "SYM",
            "Filtering before or after an identity WITH keeps the same rows",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            f"MATCH (n:N) WITH n WHERE n.id = {target} RETURN count(*) AS value",
            True,
        ),
        Case(
            "match-vs-optional-reject-null",
            "optional_null_rejection_symmetry",
            "SYM",
            "Rejecting null endpoints after OPTIONAL MATCH equals an inner MATCH",
            "MATCH (:RA)-[:R]->(m:RB) RETURN count(*) AS value",
            "MATCH (n:RA) OPTIONAL MATCH (n)-[:R]->(m:RB) WITH n, m WHERE m IS NOT NULL RETURN count(*) AS value",
            True,
        ),
        Case(
            "identity-unwind-singleton",
            "singleton_unwind_symmetry",
            "SYM",
            "UNWIND over a singleton list preserves each input row once",
            "MATCH (n:N) RETURN sum(n.grp) AS value",
            "MATCH (n:N) UNWIND [1] AS x RETURN sum(n.grp) AS value",
            True,
        ),
        Case(
            "identity-constant-projection",
            "constant_projection_symmetry",
            "SYM",
            "Adding an unused constant projection preserves the aggregate input",
            "MATCH (n:N) RETURN sum(n.grp) AS value",
            "MATCH (n:N) WITH n, 1 AS unused RETURN sum(n.grp) AS value",
            True,
        ),
        Case(
            "direct-vs-staged-aggregate",
            "aggregate_staging_symmetry",
            "SYM",
            "Returning an aggregate directly equals returning it through WITH",
            "MATCH (n:N) RETURN sum(n.grp) AS value",
            "MATCH (n:N) WITH sum(n.grp) AS value RETURN value",
            True,
        ),
        Case(
            "count-property-vs-filter",
            "count_non_null_filter_symmetry",
            "SYM",
            "Counting a property equals counting rows where that property is non-null",
            "MATCH (n:N) RETURN count(n.value) AS value",
            "MATCH (n:N) WHERE n.value IS NOT NULL RETURN count(*) AS value",
            True,
        ),
        Case(
            "return-distinct-vs-with-distinct",
            "distinct_staging_symmetry",
            "SYM",
            "RETURN DISTINCT equals the same DISTINCT projection through WITH",
            "MATCH (n:N) RETURN DISTINCT n.grp AS value",
            "MATCH (n:N) WITH DISTINCT n.grp AS value RETURN value",
            True,
        ),
        Case(
            "direct-property-filter-vs-aliased-filter",
            "property_alias_filter_symmetry",
            "SYM",
            "Filtering a property equals filtering its identity alias",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            f"MATCH (n:N) WITH n.id AS id WHERE id = {target} RETURN count(*) AS value",
            True,
        ),
        Case(
            "comparison-commutation",
            "comparison_commutation_symmetry",
            "SYM",
            "Commuting equality operands preserves the predicate",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            f"MATCH (n:N) WHERE {target} = n.id RETURN count(*) AS value",
            True,
        ),
        Case(
            "minimum-vs-sort-limit",
            "aggregate_extremum_symmetry",
            "SYM",
            "MIN equals the first non-null value in ascending order",
            "MATCH (n:N) RETURN min(n.id) AS value",
            "MATCH (n:N) RETURN n.id AS value ORDER BY value LIMIT 1",
            True,
        ),
        Case(
            "bound-node-idempotence",
            "bound_node_pattern_idempotence",
            "SYM",
            "Repeating an already bound node pattern does not add rows",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N), (n) RETURN count(*) AS value",
            True,
        ),
        Case(
            "disjoint-case-branch-order",
            "case_disjoint_branch_order_symmetry",
            "SYM",
            "Swapping mutually exclusive CASE branches preserves the value",
            "MATCH (n:N) RETURN sum(CASE WHEN n.grp = 1 THEN 11 WHEN n.grp = 2 THEN 22 ELSE 0 END) AS value",
            "MATCH (n:N) RETURN sum(CASE WHEN n.grp = 2 THEN 22 WHEN n.grp = 1 THEN 11 ELSE 0 END) AS value",
        ),
        Case(
            "union-all-branch-order",
            "union_branch_order_symmetry",
            "SYM",
            "Swapping UNION ALL branches preserves the row multiset",
            "MATCH (n:N) WHERE n.grp = 1 RETURN n.id AS value UNION ALL MATCH (n:N) WHERE n.grp = 2 RETURN n.id AS value",
            "MATCH (n:N) WHERE n.grp = 2 RETURN n.id AS value UNION ALL MATCH (n:N) WHERE n.grp = 1 RETURN n.id AS value",
        ),
        Case(
            "aggregate-input-order",
            "aggregate_order_symmetry",
            "SYM",
            "Integer SUM is independent of input order",
            "MATCH (n:N) WITH n ORDER BY n.id ASC RETURN sum(n.value) AS value",
            "MATCH (n:N) WITH n ORDER BY n.id DESC RETURN sum(n.value) AS value",
        ),
        Case(
            "union-distinct-mixed-numeric-branch-order",
            "union_branch_order_symmetry",
            "SYM",
            "UNION duplicate elimination is independent of branch order",
            "RETURN 9007199254740993 AS value UNION RETURN 9007199254740992.0 AS value",
            "RETURN 9007199254740992.0 AS value UNION RETURN 9007199254740993 AS value",
        ),
        Case(
            "case-mixed-numeric-branch-order",
            "case_disjoint_branch_order_symmetry",
            "SYM",
            "Swapping disjoint CASE branches preserves values and types",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 0 THEN 9007199254740993 WHEN x = 1 THEN 9007199254740992.0 END AS value",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 1 THEN 9007199254740992.0 WHEN x = 0 THEN 9007199254740993 END AS value",
        ),
    )


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, tuple | list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return value


def canonical(rows: list[tuple[Any, ...]]) -> list[str]:
    return sorted(
        json.dumps(jsonable(row), ensure_ascii=False, sort_keys=True, default=str)
        for row in rows
    )


def execute(cursor: Any, query: str) -> tuple[list[tuple[Any, ...]], float]:
    started = time.perf_counter()
    cursor.execute(query)
    rows = cursor.fetchall() if cursor.description else []
    return rows, (time.perf_counter() - started) * 1000.0


def setup(cursor: Any, nodes: int, noise_multiplier: int) -> None:
    cursor.execute(f"DROP GRAPH IF EXISTS {GRAPH} CASCADE")
    cursor.execute(f"CREATE GRAPH {GRAPH}")
    cursor.execute(f"SET graph_path = {GRAPH}")
    base_values = "[" + ",".join(str(value) for value in range(1, nodes + 1)) + "]"
    cursor.execute(
        f"UNWIND {base_values} AS i "
        "CREATE (:N {id: i, grp: i % 97, value: CASE WHEN i % 5 = 0 THEN null ELSE i END})"
    )
    cursor.execute(
        f"UNWIND {base_values} AS i "
        "CREATE (:RA {id: i})-[:R {id: i}]->(:RB {id: i})"
    )
    noise_nodes = nodes * noise_multiplier
    noise_values = "[" + ",".join(
        str(value) for value in range(1, noise_nodes + 1)
    ) + "]"
    cursor.execute(
        f"UNWIND {noise_values} AS i "
        f"CREATE (:RA {{id: i + {nodes}}})"
    )
    for edge_type, offset in (("S", 0), ("T", nodes * noise_multiplier)):
        cursor.execute(
            f"UNWIND {noise_values} AS i "
            "CREATE (:X%s {id: i + %s})-[:%s {id: i}]->(:Y%s {id: i + %s})"
            % (
                edge_type,
                offset,
                edge_type,
                edge_type,
                offset,
            )
        )
    cursor.execute("CREATE PROPERTY INDEX ON N (id)")
    cursor.execute("CREATE PROPERTY INDEX ON N (grp)")
    for label in ("n", "ra", "rb", "r", "s", "t"):
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
    equal = canonical(rows_a) == canonical(rows_b)
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
        "rows_equal": equal,
        "rows_a": jsonable(rows_a),
        "rows_b": jsonable(rows_b),
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


def explain(cursor: Any, query: str) -> Any:
    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {query}")
    return [row[0] for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", default="agens")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("AGENS_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=5_000)
    parser.add_argument("--noise-multiplier", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--case-pattern", default=".*")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.password:
        parser.error("set AGENS_PASSWORD or pass --password")

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
            cursor.execute("SET statement_timeout = '60s'")
            setup(cursor, args.nodes, args.noise_multiplier)
            target = max(1, args.nodes // 2)
            selected_cases = tuple(
                case for case in cases(target) if re.search(args.case_pattern, case.name)
            )
            for index, case in enumerate(selected_cases):
                record: dict[str, Any] = {
                    "name": case.name,
                    "rule": case.rule,
                    "kind": case.kind,
                    "claim": case.claim,
                    "query_a": case.query_a,
                    "query_b": case.query_b,
                    "performance": case.performance,
                    "nodes": args.nodes,
                    "noise_multiplier": args.noise_multiplier,
                }
                try:
                    measurement = measure_pair(
                        cursor,
                        case.query_a,
                        case.query_b,
                        args.warmups if case.performance else 0,
                        args.repetitions if case.performance else 1,
                        args.seed + index,
                    )
                    record["measurement"] = measurement
                    record["logical_mismatch"] = not measurement["rows_equal"]
                    record["performance_candidate"] = bool(
                        case.performance
                        and measurement["rows_equal"]
                        and measurement["median_ratio"] >= 1.5
                        and measurement["median_delta_ms"] >= 5.0
                        and measurement["direction_consistency"] >= 0.75
                    )
                    if record["logical_mismatch"] or record["performance_candidate"]:
                        try:
                            record["profile_a"] = explain(cursor, case.query_a)
                            record["profile_b"] = explain(cursor, case.query_b)
                        except Exception as profile_exc:
                            record["profile_error"] = (
                                f"{type(profile_exc).__name__}: {profile_exc}"
                            )
                except Exception as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    record["logical_mismatch"] = False
                    record["performance_candidate"] = False
                records.append(record)
                print(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))

    args.output.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            for record in records
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "logical_mismatches": [
            record["name"] for record in records if record.get("logical_mismatch")
        ],
        "performance_candidates": [
            record["name"] for record in records if record.get("performance_candidate")
        ],
        "errors": [record["name"] for record in records if "error" in record],
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["logical_mismatches"] or summary["performance_candidates"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
