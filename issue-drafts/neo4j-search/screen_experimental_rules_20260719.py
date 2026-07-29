#!/usr/bin/env python3
"""Bounded Neo4j metamorphic screen for new BOOL/SYM rule candidates.

The cases in this file are explicit experimental rules with narrow safety
preconditions.  A logical lead must differ on complete typed results and then
repeat in alternating execution order.  Runtime measurements are only broad
recall; they are not submission evidence.
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
    preconditions: tuple[str, ...]
    query_a: str
    query_b: str
    runtime_screen: bool = False
    ordered: bool = False
    logical_candidate: bool = True


def typed(expression: str) -> str:
    return f"RETURN {expression} AS value, valueType({expression}) AS value_type"


def cases(nodes: int) -> tuple[Case, ...]:
    items: list[Case] = []

    def add(
        case_id: str,
        rule_id: str,
        family: str,
        theorem: str,
        preconditions: tuple[str, ...],
        query_a: str,
        query_b: str,
        *,
        runtime: bool = False,
        ordered: bool = False,
        logical_candidate: bool = True,
    ) -> None:
        items.append(
            Case(
                case_id,
                rule_id,
                family,
                theorem,
                preconditions,
                query_a,
                query_b,
                runtime,
                ordered,
                logical_candidate,
            )
        )

    truth_values = ("true", "false", "null")
    for p in truth_values:
        add(
            f"double-negation-{p}",
            "double_negation_predicate",
            "BOOL",
            "NOT NOT P = P in Kleene three-valued logic",
            ("P is a pure boolean expression",),
            typed(p),
            typed(f"NOT NOT {p}"),
        )
        add(
            f"case-boolean-identity-{p}",
            "case_true_experimental",
            "BOOL",
            "CASE reconstruction preserves a boolean truth value",
            ("P is a pure boolean expression",),
            typed(p),
            typed(f"CASE WHEN {p} THEN true WHEN NOT {p} THEN false ELSE null END"),
        )

    for p in truth_values:
        for q in truth_values:
            add(
                f"de-morgan-and-{p}-{q}",
                "de_morgan_predicate",
                "BOOL",
                "NOT (P AND Q) = NOT P OR NOT Q in Kleene logic",
                ("P and Q are pure boolean expressions",),
                typed(f"NOT ({p} AND {q})"),
                typed(f"NOT {p} OR NOT {q}"),
            )
            add(
                f"de-morgan-or-{p}-{q}",
                "de_morgan_predicate",
                "BOOL",
                "NOT (P OR Q) = NOT P AND NOT Q in Kleene logic",
                ("P and Q are pure boolean expressions",),
                typed(f"NOT ({p} OR {q})"),
                typed(f"NOT {p} AND NOT {q}"),
            )

    quantifier_lists = (
        "[true, false, null]",
        "[null, true, true]",
        "[null, false, false]",
        "[]",
    )
    for index, value_list in enumerate(quantifier_lists):
        reversed_list = f"reverse({value_list})"
        for quantifier in ("all", "any", "none", "single"):
            add(
                f"{quantifier}-order-{index}",
                "list_quantifier_order_symmetry",
                "SYM",
                f"{quantifier} depends on truth-value multiplicity, not list order",
                ("the predicate is pure and total over list elements",),
                typed(f"{quantifier}(x IN {value_list} WHERE x)"),
                typed(f"{quantifier}(x IN {reversed_list} WHERE x)"),
            )

    for index, (needle, value_list) in enumerate(
        (
            ("3", "[null, 2, 3]"),
            ("4", "[null, 2, 3]"),
            ("9007199254740993", "[9007199254740992.0, 9007199254740993]"),
            ("null", "[null, 1]"),
        )
    ):
        add(
            f"in-order-{index}",
            "in_list_order_symmetry",
            "SYM",
            "Membership truth value is invariant under list permutation",
            ("list construction and equality checks are pure",),
            typed(f"{needle} IN {value_list}"),
            typed(f"{needle} IN reverse({value_list})"),
        )

    comparison_values = (
        ("9223372036854775807", "9.223372036854776E18"),
        ("9007199254740993", "9007199254740992.0"),
        ("0", "-0.0"),
        ("toFloat('NaN')", "toFloat('NaN')"),
        ("date('2026-01-01')", "date('2026-01-02')"),
        ("duration('P1M')", "duration('P30D')"),
    )
    for index, (left, right) in enumerate(comparison_values):
        add(
            f"equality-commutes-{index}",
            "comparison_operand_symmetry",
            "SYM",
            "Equality is commutative",
            ("both operands are pure values",),
            typed(f"{left} = {right}"),
            typed(f"{right} = {left}"),
        )
        add(
            f"ordering-dual-{index}",
            "comparison_operand_symmetry",
            "SYM",
            "A < B is equivalent to B > A",
            ("both operands are pure values from a comparable domain",),
            typed(f"{left} < {right}"),
            typed(f"{right} > {left}"),
        )

    list_pairs = (
        ("list-concat", "[1, 2] + [3, null]", "[1, 2] || [3, null]"),
        ("list-prepend", "0 + [1, 2]", "[0] || [1, 2]"),
        ("list-append", "[1, 2] + 3", "[1, 2] || [3]"),
        ("list-associativity", "([1] || [2]) || [3]", "[1] || ([2] || [3])"),
        ("string-concat", "'ab' + 'cd'", "'ab' || 'cd'"),
        ("string-associativity", "('a' || 'b') || 'c'", "'a' || ('b' || 'c')"),
        ("head-index", "head([1, null, 3])", "[1, null, 3][0]"),
        ("last-index", "last([1, null, 3])", "[1, null, 3][-1]"),
        ("tail-slice", "tail([1, null, 3])", "[1, null, 3][1..]"),
        ("double-reverse", "reverse(reverse([1, null, 3]))", "[1, null, 3]"),
    )
    for case_id, left, right in list_pairs:
        add(
            case_id,
            "list_surface_equivalence",
            "SYM",
            "The two expressions denote the same list or string value",
            ("all operands are pure literals",),
            typed(left),
            typed(right),
        )

    add(
        "coll-insert-size",
        "list_insert_composition",
        "SYM",
        "Replacing a literal list with the same list produced by coll.insert preserves size",
        (
            "the insertion index is within the literal list bounds",
            "all inputs are pure literals",
        ),
        typed("size(coll.insert([1, 2, 3], 1, 9))"),
        typed("size([1, 9, 2, 3])"),
    )
    add(
        "regex-empty-supplementary",
        "regex_zero_width_unicode",
        "SYM",
        "Empty-regex replacement around one Unicode character preserves that character",
        (
            "the input contains exactly one Unicode code point",
            "the replacement is a pure literal without group references",
        ),
        typed("string.regexReplace('🙂', '', '-')"),
        typed("'-' || '🙂' || '-'"),
    )

    aggregate_inputs = (
        ("small", "[7, 1, -7]"),
        ("int-overflow-cancel", "[9223372036854775807, 1, -9223372036854775807]"),
        ("int-float-boundary", "[9007199254740993, 1.0, -9007199254740993]"),
        ("float-scale", "[1.0E16, 1.0, -1.0E16]"),
        ("nan", "[1.0, toFloat('NaN'), 2.0]"),
    )
    aggregate_functions = (
        "sum(x)",
        "avg(x)",
        "min(x)",
        "max(x)",
        "count(DISTINCT x)",
        "stDev(x)",
        "stDevP(x)",
        "percentileDisc(x, 0.5)",
        "percentileCont(x, 0.5)",
    )
    for input_id, value_list in aggregate_inputs:
        for function in aggregate_functions:
            function_id = function.split("(", 1)[0]
            expression = f"{function} AS value, valueType({function}) AS value_type"
            add(
                f"aggregate-order-{function_id}-{input_id}",
                "aggregate_order_symmetry",
                "SYM",
                "An aggregate over a fixed multiset is invariant under input permutation",
                ("the same complete multiset reaches both aggregate operators",),
                f"UNWIND {value_list} AS x WITH x ORDER BY x ASC RETURN {expression}",
                f"UNWIND {value_list} AS x WITH x ORDER BY x DESC RETURN {expression}",
                logical_candidate=(
                    input_id in {"small", "int-overflow-cancel"}
                    and function_id
                    in {"sum", "min", "max", "count", "percentileDisc"}
                ),
            )

    graph_cases = (
        (
            "label-where",
            "label_where_symmetry",
            "A static pattern label is equivalent to the same label predicate",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n) WHERE n:N RETURN count(*) AS value",
        ),
        (
            "property-map-where",
            "property_map_where_symmetry",
            "A static property map is equivalent to the same equality predicate",
            f"MATCH (n:N {{id: {nodes // 2}}}) RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n.id = {nodes // 2} RETURN count(*) AS value",
        ),
        (
            "single-in-equality",
            "single_value_in_symmetry",
            "Equality to a value is equivalent to membership in its singleton list",
            f"MATCH (n:N) WHERE n.id = {nodes // 2} RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n.id IN [{nodes // 2}] RETURN count(*) AS value",
        ),
        (
            "equality-range",
            "numeric_equality_range",
            "For a finite integer, x=c iff x>=c AND x<=c",
            f"MATCH (n:N) WHERE n.id = {nodes // 2} RETURN count(*) AS value",
            f"MATCH (n:N) WHERE n.id >= {nodes // 2} AND n.id <= {nodes // 2} RETURN count(*) AS value",
        ),
        (
            "fixed-single-hop",
            "single_hop_varlength_symmetry",
            "A hidden one-hop path has the same endpoint rows as a *1..1 path",
            "MATCH (a:N)-[:R]->(b:N) RETURN count(*) AS value",
            "MATCH (a:N)-[:R*1..1]->(b:N) RETURN count(*) AS value",
        ),
        (
            "relationship-type-order",
            "relationship_type_order_symmetry",
            "Relationship-type disjunction is commutative",
            "MATCH ()-[:R|S]->() RETURN count(*) AS value",
            "MATCH ()-[:S|R]->() RETURN count(*) AS value",
        ),
        (
            "relationship-direction-surface",
            "relationship_direction_surface_symmetry",
            "Reversing the surface arrows and endpoints preserves the same bindings",
            "MATCH (a:N)-[:R]->(b:N) RETURN count(*) AS value",
            "MATCH (b:N)<-[:R]-(a:N) RETURN count(*) AS value",
        ),
        (
            "exists-pattern-surface",
            "exists_pattern_surface_symmetry",
            "A pattern predicate and an EXISTS subquery test the same existence condition",
            "MATCH (n:N) WHERE (n)-[:R]->() RETURN count(*) AS value",
            "MATCH (n:N) WHERE EXISTS { MATCH (n)-[:R]->() } RETURN count(*) AS value",
        ),
        (
            "count-bound-node",
            "count_bound_value_symmetry",
            "A matched node is never null, so count(n)=count(*)",
            "MATCH (n:N) RETURN count(*) AS value",
            "MATCH (n:N) RETURN count(n) AS value",
        ),
    )
    for case_id, rule_id, theorem, query_a, query_b in graph_cases:
        add(
            case_id,
            rule_id,
            "SYM" if "numeric" not in rule_id else "BOOL",
            theorem,
            ("ordinary read-only MATCH", "no path or relationship value is returned"),
            query_a,
            query_b,
            runtime=True,
        )

    return tuple(items)


def setup_graph(driver: Any, nodes: int) -> None:
    statements = (
        "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
        "DROP INDEX n_id IF EXISTS",
        f"UNWIND range(1, {nodes}) AS i CREATE (:N {{id: i, grp: i % 1000}})",
        "CREATE INDEX n_id IF NOT EXISTS FOR (n:N) ON (n.id)",
        "CALL db.awaitIndexes(300)",
        f"MATCH (a:N) WHERE a.id < {nodes} MATCH (b:N {{id: a.id + 1}}) CREATE (a)-[:R]->(b)",
        f"MATCH (a:N) WHERE a.id <= {nodes - 2} AND a.id % 2 = 0 MATCH (b:N {{id: a.id + 2}}) CREATE (a)-[:S]->(b)",
    )
    with driver.session(database="neo4j") as session:
        for statement in statements:
            session.run(statement).consume()


def execute(session: Any, query: str, timeout_seconds: int) -> dict[str, Any]:
    from neo4j import Query

    started = time.perf_counter()
    try:
        result = session.run(Query(query, timeout=timeout_seconds))
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
            "error_type": type(exc).__name__,
            "error_code": getattr(exc, "code", None),
            "error": str(exc)[:500],
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }


def compare(left: dict[str, Any], right: dict[str, Any], *, ordered: bool) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "error":
        return left.get("error_code") == right.get("error_code")
    return compare_typed_results(left["rows"], right["rows"], ordered=ordered).equivalent


def replay_case(driver: Any, case: Case, timeout_seconds: int, rounds: int) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for round_index in range(rounds):
            order = ("A", "B") if round_index % 2 == 0 else ("B", "A")
            result: dict[str, dict[str, Any]] = {}
            for side in order:
                result[side] = execute(
                    session,
                    case.query_a if side == "A" else case.query_b,
                    timeout_seconds,
                )
            observations.append(
                {
                    "round": round_index + 1,
                    "order": "".join(order),
                    "equivalent": compare(result["A"], result["B"], ordered=case.ordered),
                    "a": result["A"],
                    "b": result["B"],
                }
            )
    return {
        "deterministic_mismatch": all(not item["equivalent"] for item in observations),
        "observations": observations,
    }


def runtime_screen(
    driver: Any,
    case: Case,
    *,
    timeout_seconds: int,
    warmups: int,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(f"{seed}:{case.case_id}")
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for _ in range(warmups):
            execute(session, case.query_a, timeout_seconds)
            execute(session, case.query_b, timeout_seconds)
        for repetition in range(repetitions):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for side in order:
                outcome = execute(
                    session,
                    case.query_a if side == "A" else case.query_b,
                    timeout_seconds,
                )
                if outcome["status"] != "ok":
                    raise RuntimeError(f"runtime screen failed on {side}: {outcome}")
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
    slower_a = median_a >= median_b
    return {
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": max(median_a, median_b) / max(min(median_a, median_b), 0.001),
        "median_delta_ms": abs(median_a - median_b),
        "slow_query": "A" if slower_a else "B",
        "direction_consistency": sum(
            1
            for sample in samples
            if (sample["a_ms"] >= sample["b_ms"]) == slower_a
        )
        / len(samples),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--replay-rounds", type=int, default=6)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260719)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    summary: Counter[str] = Counter()
    try:
        setup_graph(driver, args.nodes)
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "WHERE name = 'Neo4j Kernel' "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            print(
                json.dumps(
                    {"environment": {**component.data(), "nodes": args.nodes}},
                    sort_keys=True,
                ),
                flush=True,
            )

        for case in cases(args.nodes):
            with driver.session(database="neo4j") as session:
                left = execute(session, case.query_a, args.timeout)
                right = execute(session, case.query_b, args.timeout)
            equivalent = compare(left, right, ordered=case.ordered)
            report: dict[str, Any] = {
                "case_id": case.case_id,
                "rule_id": case.rule_id,
                "family": case.family,
                "theorem": case.theorem,
                "preconditions": case.preconditions,
                "equivalent": equivalent,
                "query_a": case.query_a,
                "query_b": case.query_b,
                "a": left,
                "b": right,
            }
            if not equivalent and case.logical_candidate:
                report["replay"] = replay_case(
                    driver, case, args.timeout, args.replay_rounds
                )
                summary["logical_leads"] += 1
                if report["replay"]["deterministic_mismatch"]:
                    summary["deterministic_logical_leads"] += 1
            elif not equivalent:
                report["screening_disposition"] = (
                    "INELIGIBLE_APPROXIMATE_OR_NAN_ORDER"
                )
                summary["ineligible_numeric_order_differences"] += 1
            elif case.runtime_screen and left["status"] == "ok":
                report["runtime"] = runtime_screen(
                    driver,
                    case,
                    timeout_seconds=args.timeout,
                    warmups=args.warmups,
                    repetitions=args.repetitions,
                    seed=args.seed,
                )
                runtime = report["runtime"]
                if (
                    runtime["median_ratio"] >= 1.5
                    and runtime["median_delta_ms"] >= 10.0
                    and runtime["direction_consistency"] >= 0.75
                ):
                    summary["runtime_leads"] += 1
            summary["cases"] += 1
            summary[f"rule:{case.rule_id}"] += 1
            print(json.dumps(report, sort_keys=True, ensure_ascii=False), flush=True)
        print(json.dumps({"summary": dict(summary)}, sort_keys=True), flush=True)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
