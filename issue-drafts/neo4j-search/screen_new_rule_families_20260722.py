#!/usr/bin/env python3
"""Screen new Neo4j 2026.06 rule families with ordinary execution timing."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


@dataclass(frozen=True)
class Case:
    case_id: str
    rule_id: str
    theorem: str
    query_a: str
    query_b: str
    family: str = "SYM"


def cases(nodes: int) -> tuple[Case, ...]:
    limit = min(50_000, max(1, nodes // 2))
    skip = min(20_000, max(1, nodes // 5))
    target = max(1, nodes // 2)
    return (
        Case(
            "order-before-limited-count",
            "order_before_limited_count_symmetry",
            "ordering changes which limited rows survive but not how many survive",
            f"MATCH (n:N) WITH n LIMIT {limit} RETURN count(*) AS value",
            (
                f"MATCH (n:N) WITH n ORDER BY n.id LIMIT {limit} "
                "RETURN count(*) AS value"
            ),
        ),
        Case(
            "order-before-skip-count",
            "order_before_skip_count_symmetry",
            "ordering cannot change the number of rows remaining after a fixed skip",
            f"MATCH (n:N) WITH n SKIP {skip} RETURN count(*) AS value",
            (
                f"MATCH (n:N) WITH n ORDER BY n.id SKIP {skip} "
                "RETURN count(*) AS value"
            ),
        ),
        Case(
            "min-distinct-idempotence",
            "min_distinct_idempotence",
            "removing duplicate aggregate inputs cannot change their minimum",
            "MATCH (n:N) RETURN min(n.grp) AS value",
            "MATCH (n:N) RETURN min(DISTINCT n.grp) AS value",
        ),
        Case(
            "max-distinct-idempotence",
            "max_distinct_idempotence",
            "removing duplicate aggregate inputs cannot change their maximum",
            "MATCH (n:N) RETURN max(n.grp) AS value",
            "MATCH (n:N) RETURN max(DISTINCT n.grp) AS value",
        ),
        Case(
            "unique-property-distinct-projection",
            "unique_property_distinct_symmetry",
            "a uniqueness constraint makes DISTINCT redundant for non-null property values",
            "MATCH (n:N) WITH n.id AS id RETURN count(id) AS value",
            "MATCH (n:N) WITH DISTINCT n.id AS id RETURN count(id) AS value",
        ),
        Case(
            "unique-property-distinct-aggregate",
            "unique_property_distinct_symmetry",
            "a uniqueness constraint makes count(DISTINCT property) equal count(property)",
            "MATCH (n:N) RETURN count(n.id) AS value",
            "MATCH (n:N) RETURN count(DISTINCT n.id) AS value",
        ),
        Case(
            "element-id-distinct-projection",
            "element_id_distinct_symmetry",
            "a node scan produces unique elementId values",
            "MATCH (n:N) WITH elementId(n) AS id RETURN count(id) AS value",
            (
                "MATCH (n:N) WITH DISTINCT elementId(n) AS id "
                "RETURN count(id) AS value"
            ),
        ),
        Case(
            "exists-vs-positive-count-subquery",
            "positive_count_exists_symmetry",
            "a pattern count greater than zero is equivalent to pattern existence",
            (
                "MATCH (n:Source) WHERE EXISTS { MATCH (n)-[:R]->() } "
                "RETURN count(*) AS value"
            ),
            (
                "MATCH (n:Source) WHERE COUNT { MATCH (n)-[:R]->() } > 0 "
                "RETURN count(*) AS value"
            ),
        ),
        Case(
            "not-exists-vs-zero-count-subquery",
            "zero_count_not_exists_symmetry",
            "a zero pattern count is equivalent to pattern non-existence",
            (
                "MATCH (n:N) WHERE NOT EXISTS { MATCH (n)-[:R]->() } "
                "RETURN count(*) AS value"
            ),
            (
                "MATCH (n:N) WHERE COUNT { MATCH (n)-[:R]->() } = 0 "
                "RETURN count(*) AS value"
            ),
        ),
        Case(
            "optional-false-pattern",
            "optional_false_pattern_symmetry",
            "a false optional pattern contributes one null-extended row per input row",
            "MATCH (n:N) RETURN sum(n.id) AS value",
            (
                "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m) WHERE false "
                "RETURN sum(n.id) AS value"
            ),
        ),
        Case(
            "relationship-type-pattern-vs-where",
            "relationship_type_predicate_symmetry",
            "a static relationship type equals the same type predicate",
            "MATCH ()-[r:R]->() RETURN count(*) AS value",
            "MATCH ()-[r]->() WHERE type(r) = 'R' RETURN count(*) AS value",
        ),
        Case(
            "empty-union-branch-elimination",
            "empty_union_branch_symmetry",
            "adding a statically empty UNION ALL branch preserves every row",
            (
                "CALL () { MATCH (n:N) RETURN n.id AS id } "
                "RETURN count(id) AS value"
            ),
            (
                "CALL () { MATCH (n:N) RETURN n.id AS id "
                "UNION ALL MATCH (n:N) WHERE false RETURN n.id AS id } "
                "RETURN count(id) AS value"
            ),
        ),
        Case(
            "correlated-identity-subquery",
            "correlated_identity_subquery_symmetry",
            "a correlated subquery that only returns its input property is an identity",
            "MATCH (n:N) RETURN sum(n.id) AS value",
            (
                "MATCH (n:N) CALL (n) { RETURN n.id AS id } "
                "RETURN sum(id) AS value"
            ),
        ),
        Case(
            "count-pushdown-through-union-all",
            "union_all_count_pushdown_symmetry",
            "count over UNION ALL equals the sum of the branch counts",
            (
                "CALL () { MATCH (n:N) WHERE n.id <= "
                f"{target} RETURN n.id AS id UNION ALL "
                f"MATCH (n:N) WHERE n.id > {target} RETURN n.id AS id }} "
                "RETURN count(id) AS value"
            ),
            (
                "CALL () { MATCH (n:N) WHERE n.id <= "
                f"{target} RETURN count(n.id) AS subtotal UNION ALL "
                f"MATCH (n:N) WHERE n.id > {target} "
                "RETURN count(n.id) AS subtotal } "
                "RETURN sum(subtotal) AS value"
            ),
        ),
        Case(
            "count-pushdown-through-union-all-node",
            "union_all_count_pushdown_symmetry",
            "count over UNION ALL equals the sum of the branch counts",
            (
                "CALL () { MATCH (n:N) RETURN n AS item UNION ALL "
                "MATCH (m:N) RETURN m AS item } RETURN count(item) AS value"
            ),
            (
                "CALL () { MATCH (n:N) RETURN count(n) AS subtotal UNION ALL "
                "MATCH (m:N) RETURN count(m) AS subtotal } "
                "RETURN sum(subtotal) AS value"
            ),
        ),
        Case(
            "count-pushdown-through-identity-subquery",
            "identity_subquery_count_pushdown_symmetry",
            "an identity subquery preserves the rows counted by its caller",
            "MATCH (n:N) RETURN count(n) AS value",
            (
                "CALL () { MATCH (n:N) RETURN n } "
                "RETURN count(n) AS value"
            ),
        ),
        Case(
            "partition-count-sum",
            "partition_count_sum_symmetry",
            "the sum of disjoint group counts equals the total row count",
            "MATCH (n:N) RETURN count(*) AS value",
            (
                "MATCH (n:N) WITH n.grp AS grp, count(*) AS subtotal "
                "RETURN sum(subtotal) AS value"
            ),
        ),
        Case(
            "filtered-count-vs-conditional-sum",
            "conditional_count_filter_symmetry",
            "a filtered count equals a zero-one conditional sum",
            (
                f"MATCH (n:N) WHERE n.id = {target} "
                "RETURN count(*) AS value"
            ),
            (
                f"MATCH (n:N) RETURN sum(CASE WHEN n.id = {target} "
                "THEN 1 ELSE 0 END) AS value"
            ),
        ),
        Case(
            "filtered-count-vs-conditional-count",
            "conditional_count_filter_symmetry",
            "a filtered count equals counting a nullable conditional expression",
            (
                f"MATCH (n:N) WHERE n.id = {target} "
                "RETURN count(*) AS value"
            ),
            (
                f"MATCH (n:N) RETURN count(CASE WHEN n.id = {target} "
                "THEN 1 END) AS value"
            ),
        ),
        Case(
            "repeated-bound-node-pattern",
            "bound_node_pattern_idempotence",
            "repeating a bound node pattern does not change its rows",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N) MATCH (n) RETURN count(*) AS value",
        ),
        Case(
            "node-distinct-projection",
            "bound_node_distinct_symmetry",
            "a node scan binds each node once, so DISTINCT on the node is redundant",
            "MATCH (n:N) WITH n RETURN count(n) AS value",
            "MATCH (n:N) WITH DISTINCT n RETURN count(n) AS value",
        ),
        Case(
            "relationship-count-bound-value",
            "count_bound_relationship_symmetry",
            "a matched relationship is non-null, so count(r) equals count(*)",
            "MATCH ()-[r:R]->() RETURN count(*) AS value",
            "MATCH ()-[r:R]->() RETURN count(r) AS value",
        ),
    )


def setup_graph(driver: Any, nodes: int, sources: int, degree: int) -> None:
    if not 0 < sources < nodes:
        raise ValueError("sources must be between zero and nodes")
    statements: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { CREATE (:N {id: i, grp: i % 1000}) } "
            "IN TRANSACTIONS OF 5000 ROWS",
            {"nodes": nodes},
        ),
        (
            "MATCH (n:N) WHERE n.id <= $sources "
            "CALL (n) { SET n:Source } IN TRANSACTIONS OF 5000 ROWS",
            {"sources": sources},
        ),
        (
            "CREATE CONSTRAINT n_id_unique IF NOT EXISTS "
            "FOR (n:N) REQUIRE n.id IS UNIQUE",
            {},
        ),
        ("CALL db.awaitIndexes(300)", {}),
        (
            "UNWIND range(1, $sources) AS i "
            "CALL (i) { MATCH (a:N {id: i}) "
            "UNWIND range(1, $degree) AS offset "
            "MATCH (b:N {id: (($sources + i + offset - 2) % "
            "($nodes - $sources)) + $sources + 1}) "
            "CREATE (a)-[:R]->(b) } IN TRANSACTIONS OF 200 ROWS",
            {"nodes": nodes, "sources": sources, "degree": degree},
        ),
    )
    with driver.session(database="neo4j") as session:
        for statement, parameters in statements:
            session.run(statement, parameters).consume()


def execute(session: Any, query: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    started = time.perf_counter()
    try:
        result = session.run(Query(f"CYPHER 25 {query}", timeout=timeout))
        rows = tuple(to_typed_value(record.data()) for record in result)
        result.consume()
        return {
            "status": "ok",
            "rows": rows,
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
    comparison = compare_typed_results(left["rows"], right["rows"], ordered=False)
    if comparison.equivalent:
        return True
    left_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in left["rows"]
    )
    right_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in right["rows"]
    )
    return left_multiset == right_multiset


def measure_pair(
    driver: Any,
    case: Case,
    warmups: int,
    repetitions: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(f"{seed}:{case.case_id}")
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        first_a = execute(session, case.query_a, timeout)
        first_b = execute(session, case.query_b, timeout)
        same = equivalent(first_a, first_b)
        if not same:
            return {"equivalent": False, "a": first_a, "b": first_b}
        if first_a["status"] != "ok":
            return {
                "equivalent": True,
                "unsupported": True,
                "a": first_a,
                "b": first_b,
            }
        for _ in range(warmups):
            execute(session, case.query_a, timeout)
            execute(session, case.query_b, timeout)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                outcome = execute(
                    session,
                    case.query_a if side == "A" else case.query_b,
                    timeout,
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
    direction = sum(
        (sample["a_ms"] > sample["b_ms"]) == (slow_query == "A")
        for sample in samples
    ) / len(samples)
    return {
        "equivalent": True,
        "result_a": first_a["rows"],
        "result_b": first_b["rows"],
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": max(median_a, median_b)
        / max(min(median_a, median_b), 0.001),
        "median_delta_ms": abs(median_a - median_b),
        "slow_query": slow_query,
        "direction_consistency": direction,
        "samples": samples,
    }


def explain(driver: Any, query: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    with driver.session(database="neo4j") as session:
        result = session.run(Query(f"CYPHER 25 EXPLAIN {query}", timeout=timeout))
        summary = result.consume()
        return summary.plan or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--sources", type=int, default=5_000)
    parser.add_argument("--degree", type=int, default=50)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.uri or not args.password:
        parser.error("set NEO4J_URI and NEO4J_PASSWORD")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    records: list[dict[str, Any]] = []
    try:
        if not args.skip_setup:
            setup_graph(driver, args.nodes, args.sources, args.degree)
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD versions, edition "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            environment = {
                **component.data(),
                "nodes": args.nodes,
                "sources": args.sources,
                "degree": args.degree,
                "relationships": args.sources * args.degree,
            }
        any_lead = False
        for index, case in enumerate(cases(args.nodes)):
            measurement = measure_pair(
                driver,
                case,
                args.warmups,
                args.repetitions,
                args.timeout,
                args.seed + index,
            )
            logical_mismatch = not measurement.get("equivalent", False)
            performance_lead = bool(
                measurement.get("equivalent")
                and measurement.get("median_ratio", 0.0) >= 1.5
                and measurement.get("median_delta_ms", 0.0) >= 10.0
                and measurement.get("direction_consistency", 0.0) >= 0.75
            )
            lead = logical_mismatch or performance_lead
            record: dict[str, Any] = {
                "case_id": case.case_id,
                "rule_id": case.rule_id,
                "family": case.family,
                "theorem": case.theorem,
                "query_a": case.query_a,
                "query_b": case.query_b,
                "logical_mismatch": logical_mismatch,
                "performance_lead": performance_lead,
                "lead": lead,
                "measurement": measurement,
            }
            if lead and measurement.get("equivalent"):
                record["plan_a"] = explain(driver, case.query_a, args.timeout)
                record["plan_b"] = explain(driver, case.query_b, args.timeout)
            records.append(record)
            any_lead = any_lead or lead
    finally:
        driver.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(
            [json.dumps({"environment": environment}, sort_keys=True)]
            + [
                json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
                for record in records
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "output": str(args.output),
        "leads": [record["case_id"] for record in records if record["lead"]],
        "logical_mismatches": [
            record["case_id"] for record in records if record["logical_mismatch"]
        ],
        "errors": [
            record["case_id"]
            for record in records
            if record["measurement"].get("unsupported")
            or record["measurement"].get("measurement_error")
        ],
    }
    print(json.dumps(summary, sort_keys=True))
    return 2 if any_lead else 0


if __name__ == "__main__":
    raise SystemExit(main())
