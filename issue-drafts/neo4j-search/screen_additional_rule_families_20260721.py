#!/usr/bin/env python3
"""Screen additional Neo4j logical and performance metamorphic rules.

The cases avoid the existing pattern-order, integer-overflow, aggregate-order,
coll.insert(), and zero-width-regex issue families. Logical mismatches are
replayed in alternating order. Performance measurements are broad recall only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


@dataclass(frozen=True)
class Case:
    case_id: str
    rule_id: str
    family: str
    theorem: str
    query_a: str
    query_b: str
    languages: tuple[str, ...] = ("5", "25")
    performance: bool = False
    ordered: bool = False


def logical_cases() -> tuple[Case, ...]:
    cases = [
        Case(
            "union-all-integer-float-swap",
            "union_branch_order_symmetry",
            "SYM",
            "UNION ALL preserves the same typed row multiset when its branches are swapped",
            "RETURN 9007199254740993 AS value UNION ALL RETURN 9007199254740992.0 AS value",
            "RETURN 9007199254740992.0 AS value UNION ALL RETURN 9007199254740993 AS value",
        ),
        Case(
            "union-all-list-type-swap",
            "union_branch_order_symmetry",
            "SYM",
            "UNION ALL branch order does not change list values or types",
            "RETURN [1, 2] AS value UNION ALL RETURN [1.0, 2.0] AS value",
            "RETURN [1.0, 2.0] AS value UNION ALL RETURN [1, 2] AS value",
        ),
        Case(
            "union-all-null-temporal-swap",
            "union_branch_order_symmetry",
            "SYM",
            "UNION ALL branch order does not change a null and temporal row multiset",
            "RETURN null AS value UNION ALL RETURN date('2026-01-01') AS value",
            "RETURN date('2026-01-01') AS value UNION ALL RETURN null AS value",
        ),
        Case(
            "union-distinct-integer-float-swap",
            "union_branch_order_symmetry",
            "SYM",
            "UNION duplicate elimination is independent of branch order",
            "RETURN 9007199254740993 AS value UNION RETURN 9007199254740992.0 AS value",
            "RETURN 9007199254740992.0 AS value UNION RETURN 9007199254740993 AS value",
        ),
        Case(
            "case-mutually-exclusive-numeric-swap",
            "case_branch_order_symmetry",
            "SYM",
            "Swapping mutually exclusive CASE branches preserves values and types",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 0 THEN 9007199254740993 WHEN x = 1 THEN 9007199254740992.0 END AS value",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 1 THEN 9007199254740992.0 WHEN x = 0 THEN 9007199254740993 END AS value",
        ),
        Case(
            "case-mutually-exclusive-list-swap",
            "case_branch_order_symmetry",
            "SYM",
            "Swapping mutually exclusive CASE branches preserves list values and types",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 0 THEN [1, 2] WHEN x = 1 THEN [1.0, 2.0] END AS value",
            "UNWIND [0, 1] AS x RETURN x, CASE WHEN x = 1 THEN [1.0, 2.0] WHEN x = 0 THEN [1, 2] END AS value",
        ),
        Case(
            "union-list-null-consumer-swap",
            "union_branch_composition_symmetry",
            "SYM",
            "Swapping UNION ALL branches preserves list-consumer outcomes",
            "CALL () { RETURN [1, 2] AS value UNION ALL RETURN null AS value } RETURN size(value) AS result",
            "CALL () { RETURN null AS value UNION ALL RETURN [1, 2] AS value } RETURN size(value) AS result",
        ),
        Case(
            "union-map-null-consumer-swap",
            "union_branch_composition_symmetry",
            "SYM",
            "Swapping UNION ALL branches preserves map-consumer outcomes",
            "CALL () { RETURN {a: 1} AS value UNION ALL RETURN null AS value } RETURN keys(value) AS result",
            "CALL () { RETURN null AS value UNION ALL RETURN {a: 1} AS value } RETURN keys(value) AS result",
        ),
        Case(
            "union-numeric-consumer-swap",
            "union_branch_composition_symmetry",
            "SYM",
            "Swapping UNION ALL branches preserves numeric-expression outcomes",
            "CALL () { RETURN 9007199254740993 AS value UNION ALL RETURN 9007199254740992.0 AS value } RETURN value + 1 AS result",
            "CALL () { RETURN 9007199254740992.0 AS value UNION ALL RETURN 9007199254740993 AS value } RETURN value + 1 AS result",
        ),
        Case(
            "case-list-consumer-swap",
            "case_branch_composition_symmetry",
            "SYM",
            "Swapping mutually exclusive CASE branches preserves list-consumer outcomes",
            "UNWIND [0, 1] AS x RETURN x, size(CASE WHEN x = 0 THEN [1, 2] WHEN x = 1 THEN [3] END) AS result",
            "UNWIND [0, 1] AS x RETURN x, size(CASE WHEN x = 1 THEN [3] WHEN x = 0 THEN [1, 2] END) AS result",
        ),
        Case(
            "coalesce-list-composition",
            "coalesce_composition_symmetry",
            "SYM",
            "Replacing a non-null literal list with coalesce(null, list) preserves its consumer",
            "RETURN size(coalesce(null, [1, 2])) AS result",
            "RETURN size([1, 2]) AS result",
        ),
        Case(
            "label-conjunction-surface",
            "label_expression_surface_symmetry",
            "SYM",
            "A label-expression conjunction and two static labels select the same nodes",
            "MATCH (n:N&A) RETURN n.id AS id ORDER BY id",
            "MATCH (n:N:A) RETURN n.id AS id ORDER BY id",
            languages=("25",),
            ordered=True,
        ),
        Case(
            "label-disjunction-surface",
            "label_expression_surface_symmetry",
            "SYM",
            "A label-expression disjunction and the equivalent WHERE predicate select the same nodes",
            "MATCH (n:N|B) RETURN n.id AS id ORDER BY id",
            "MATCH (n) WHERE n:N OR n:B RETURN n.id AS id ORDER BY id",
            languages=("25",),
            ordered=True,
        ),
        Case(
            "path-length-relationships-size",
            "path_length_surface_symmetry",
            "SYM",
            "length(path) equals the size of its relationship list",
            "MATCH p=(a:N)-[:R*1..2]->(b:N) RETURN sum(length(p)) AS value",
            "MATCH p=(a:N)-[:R*1..2]->(b:N) RETURN sum(size(relationships(p))) AS value",
        ),
        Case(
            "count-subquery-collect-size",
            "count_collect_surface_symmetry",
            "SYM",
            "A COUNT subquery equals the size of a COLLECT subquery over the same rows",
            "MATCH (n:N) WHERE n.id <= 100 RETURN sum(COUNT { MATCH (n)-[:R]->(m) }) AS value",
            "MATCH (n:N) WHERE n.id <= 100 RETURN sum(size(COLLECT { MATCH (n)-[:R]->(m) RETURN m })) AS value",
            languages=("25",),
        ),
        Case(
            "relationship-direction-surface-ids",
            "relationship_direction_surface_symmetry",
            "SYM",
            "Reversing surface arrows and endpoint order preserves bound endpoint rows",
            "MATCH (a:N)-[:R]->(b:N) WHERE a.id <= 100 RETURN a.id AS a, b.id AS b ORDER BY a, b",
            "MATCH (b:N)<-[:R]-(a:N) WHERE a.id <= 100 RETURN a.id AS a, b.id AS b ORDER BY a, b",
            ordered=True,
        ),
    ]
    return tuple(cases)


def performance_cases(nodes: int) -> tuple[Case, ...]:
    target = max(1, nodes // 2)
    return (
        Case(
            "fixed-hop-vs-exact-varlength",
            "single_hop_varlength_symmetry",
            "SYM",
            "A fixed one-hop relationship and an exact *1..1 path return the same endpoint rows",
            "MATCH (a:N)-[:R]->(b:N) RETURN count(*) AS value",
            "MATCH (a:N)-[:R*1..1]->(b:N) RETURN count(*) AS value",
            performance=True,
        ),
        Case(
            "node-match-vs-exact-zero-hop",
            "zero_hop_path_symmetry",
            "SYM",
            "An exact *0..0 path binds both endpoints to the same node",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N)-[:R*0..0]->(m:N) RETURN count(*) AS value",
            performance=True,
        ),
        Case(
            "relationship-type-pattern-vs-where",
            "relationship_type_predicate_symmetry",
            "SYM",
            "A static relationship type in a pattern equals the same type(r) predicate",
            "MATCH ()-[r:R]->() RETURN count(*) AS value",
            "MATCH ()-[r]->() WHERE type(r) = 'R' RETURN count(*) AS value",
            performance=True,
        ),
        Case(
            "static-property-dot-vs-subscript",
            "static_property_access_symmetry",
            "SYM",
            "A literal property subscript equals dot access to the same property",
            f"MATCH (n:N) WHERE n.id = {target} RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n['id'] = {target} RETURN count(*) AS value",
            performance=True,
        ),
        Case(
            "pattern-predicate-vs-exists-subquery",
            "exists_pattern_surface_symmetry",
            "SYM",
            "A pattern predicate and an EXISTS subquery test the same relationship existence",
            "MATCH (n:N) WHERE (n)-[:R]->() RETURN count(*) AS value",
            "MATCH (n:N) WHERE EXISTS { MATCH (n)-[:R]->() } RETURN count(*) AS value",
            performance=True,
        ),
        Case(
            "count-star-vs-count-bound-node",
            "count_bound_value_symmetry",
            "SYM",
            "A matched node is non-null, so count(*) equals count(n)",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N) RETURN count(n) AS value",
            performance=True,
        ),
        Case(
            "count-star-vs-count-non-null-literal",
            "count_non_null_literal_symmetry",
            "SYM",
            "A non-null literal contributes once for every matched row",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N) RETURN count(1) AS value",
            performance=True,
        ),
        Case(
            "label-conjunction-expression-vs-where",
            "label_expression_surface_symmetry",
            "SYM",
            "A label-expression conjunction equals the same conjunction in WHERE",
            "MATCH (n:N&A) RETURN count(*) AS value",
            "MATCH (n) WHERE n:N AND n:A RETURN count(*) AS value",
            languages=("25",),
            performance=True,
        ),
        Case(
            "label-disjunction-expression-vs-where",
            "label_expression_surface_symmetry",
            "SYM",
            "A label-expression disjunction equals the same disjunction in WHERE",
            "MATCH (n:A|B) RETURN count(*) AS value",
            "MATCH (n) WHERE n:A OR n:B RETURN count(*) AS value",
            languages=("25",),
            performance=True,
        ),
        Case(
            "static-label-vs-is-labeled",
            "is_labeled_surface_symmetry",
            "SYM",
            "A static label pattern equals the same IS LABELED predicate",
            "MATCH (n:A) RETURN count(*) AS value",
            "MATCH (n) WHERE n IS LABELED A RETURN count(*) AS value",
            languages=("25",),
            performance=True,
        ),
        Case(
            "unwind-vs-for",
            "for_unwind_surface_symmetry",
            "SYM",
            "FOR is the GQL surface form of UNWIND for the same list",
            "UNWIND range(1, 1000000) AS x RETURN sum(x % 17) AS value",
            "FOR x IN range(1, 1000000) RETURN sum(x % 17) AS value",
            languages=("25",),
            performance=True,
        ),
        Case(
            "with-projection-vs-let",
            "let_with_surface_symmetry",
            "SYM",
            "LET and WITH bind the same expression while retaining the row",
            "MATCH (n:N) WITH *, n.id % 17 AS bucket RETURN sum(bucket) AS value",
            "MATCH (n:N) LET bucket = n.id % 17 RETURN sum(bucket) AS value",
            languages=("25",),
            performance=True,
        ),
        Case(
            "with-where-vs-filter",
            "filter_where_surface_symmetry",
            "SYM",
            "FILTER and WITH * WHERE keep the same rows",
            "MATCH (n:N) WITH * WHERE n.id % 97 = 0 RETURN count(*) AS value",
            "MATCH (n:N) FILTER n.id % 97 = 0 RETURN count(*) AS value",
            languages=("25",),
            performance=True,
        ),
    )


def setup_graph(driver: Any, nodes: int, r_degree: int, s_degree: int) -> None:
    statements: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
            {},
        ),
        ("DROP INDEX n_id IF EXISTS", {}),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { CREATE (:N {id: i, key: i % 1000}) } "
            "IN TRANSACTIONS OF 5000 ROWS",
            {"nodes": nodes},
        ),
        (
            "MATCH (n:N) WHERE n.id % 2 = 0 "
            "CALL (n) { SET n:A } IN TRANSACTIONS OF 5000 ROWS",
            {},
        ),
        (
            "MATCH (n:N) WHERE n.id % 3 = 0 "
            "CALL (n) { SET n:B } IN TRANSACTIONS OF 5000 ROWS",
            {},
        ),
        ("CREATE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id)", {}),
        ("CALL db.awaitIndexes(300)", {}),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { MATCH (a:N {id: i}) UNWIND range(1, $degree) AS offset "
            "MATCH (b:N {id: ((i + offset - 1) % $nodes) + 1}) "
            "CREATE (a)-[:R]->(b) } IN TRANSACTIONS OF 500 ROWS",
            {"nodes": nodes, "degree": r_degree},
        ),
        (
            "UNWIND range(1, $nodes) AS i "
            "CALL (i) { MATCH (a:N {id: i}) UNWIND range(1, $degree) AS offset "
            "MATCH (b:N {id: ((i + offset + $r_degree - 1) % $nodes) + 1}) "
            "CREATE (a)-[:S]->(b) } IN TRANSACTIONS OF 500 ROWS",
            {"nodes": nodes, "degree": s_degree, "r_degree": r_degree},
        ),
    )
    with driver.session(database="neo4j") as session:
        for statement, parameters in statements:
            session.run(statement, parameters).consume()


def execute(session: Any, query: str, language: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    started = time.perf_counter()
    try:
        result = session.run(Query(f"CYPHER {language} {query}", timeout=timeout))
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
            "error": str(exc)[:500],
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }


def equivalent(left: dict[str, Any], right: dict[str, Any], ordered: bool) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "error":
        return left.get("error_code") == right.get("error_code")
    comparison = compare_typed_results(
        left["rows"], right["rows"], ordered=ordered
    )
    if comparison.equivalent or ordered:
        return comparison.equivalent
    left_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in left["rows"]
    )
    right_multiset = Counter(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        for row in right["rows"]
    )
    return left_multiset == right_multiset


def replay_logical(driver: Any, case: Case, rounds: int, timeout: int) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for language in case.languages:
            for round_index in range(rounds):
                order = ("A", "B") if round_index % 2 == 0 else ("B", "A")
                outcomes: dict[str, dict[str, Any]] = {}
                for side in order:
                    outcomes[side] = execute(
                        session,
                        case.query_a if side == "A" else case.query_b,
                        language,
                        timeout,
                    )
                observations.append(
                    {
                        "language": language,
                        "round": round_index + 1,
                        "order": "".join(order),
                        "equivalent": equivalent(
                            outcomes["A"], outcomes["B"], case.ordered
                        ),
                        "a": outcomes["A"],
                        "b": outcomes["B"],
                    }
                )
    return {
        "deterministic_mismatch": all(
            not observation["equivalent"] for observation in observations
        ),
        "observations": observations,
    }


def measure_performance(
    driver: Any,
    case: Case,
    language: str,
    warmups: int,
    repetitions: int,
    timeout: int,
    seed: int,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    rng = random.Random(f"{seed}:{case.case_id}:{language}")
    with driver.session(database="neo4j") as session:
        first_a = execute(session, case.query_a, language, timeout)
        first_b = execute(session, case.query_b, language, timeout)
        same = equivalent(first_a, first_b, case.ordered)
        if not same:
            return {"equivalent": False, "a": first_a, "b": first_b}
        for _ in range(warmups):
            execute(session, case.query_a, language, timeout)
            execute(session, case.query_b, language, timeout)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                outcome = execute(
                    session,
                    case.query_a if side == "A" else case.query_b,
                    language,
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
    slower = "A" if median_a >= median_b else "B"
    direction = sum(
        1
        for sample in samples
        if (sample["a_ms"] >= sample["b_ms"]) == (slower == "A")
    ) / len(samples)
    return {
        "equivalent": True,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": max(median_a, median_b)
        / max(min(median_a, median_b), 0.001),
        "median_delta_ms": abs(median_a - median_b),
        "slow_query": slower,
        "direction_consistency": direction,
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=5_000)
    parser.add_argument("--r-degree", type=int, default=4)
    parser.add_argument("--s-degree", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--logical-only", action="store_true")
    parser.add_argument("--performance-only", action="store_true")
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    summary: Counter[str] = Counter()
    try:
        if not args.skip_setup:
            setup_graph(driver, args.nodes, args.r_degree, args.s_degree)
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD versions, edition "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            print(
                json.dumps(
                    {
                        "environment": {
                            **component.data(),
                            "nodes": args.nodes,
                            "r_degree": args.r_degree,
                            "s_degree": args.s_degree,
                        }
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        selected_logical_cases = () if args.performance_only else logical_cases()
        for case in selected_logical_cases:
            replay = replay_logical(driver, case, args.rounds, args.timeout)
            summary["logical_cases"] += 1
            summary["deterministic_logical_leads"] += int(
                replay["deterministic_mismatch"]
            )
            print(
                json.dumps(
                    {
                        "kind": "logical",
                        "case_id": case.case_id,
                        "rule_id": case.rule_id,
                        "family": case.family,
                        "theorem": case.theorem,
                        "query_a": case.query_a,
                        "query_b": case.query_b,
                        "replay": replay,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )

        selected_performance_cases = (
            () if args.logical_only else performance_cases(args.nodes)
        )
        for case in selected_performance_cases:
            for language in case.languages:
                measurement = measure_performance(
                    driver,
                    case,
                    language,
                    args.warmups,
                    args.repetitions,
                    args.timeout,
                    args.seed,
                )
                is_lead = bool(
                    measurement.get("equivalent")
                    and measurement.get("median_ratio", 0.0) >= 1.5
                    and measurement.get("median_delta_ms", 0.0) >= 10.0
                    and measurement.get("direction_consistency", 0.0) >= 0.75
                )
                summary["performance_cases"] += 1
                summary["performance_leads"] += int(is_lead)
                print(
                    json.dumps(
                        {
                            "kind": "performance",
                            "case_id": case.case_id,
                            "rule_id": case.rule_id,
                            "family": case.family,
                            "theorem": case.theorem,
                            "language": language,
                            "query_a": case.query_a,
                            "query_b": case.query_b,
                            "lead": is_lead,
                            "measurement": measurement,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )
        print(json.dumps({"summary": dict(summary)}, sort_keys=True), flush=True)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
