#!/usr/bin/env python3
"""Screen previously uncovered Memgraph equivalence-rule families.

This is a discovery harness.  It rebuilds a disposable graph for each group,
compares complete ordered result rows, alternates A/B timing order, and records
EXPLAIN output.  A hit still requires two fresh-process validation runs before
it can become an issue draft.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from neo4j import GraphDatabase, Query


@dataclass(frozen=True)
class RulePair:
    rule_id: str
    family: str
    query_a: str
    query_b: str
    rows_a: str
    rows_b: str
    proof_boundary: str
    params_a: dict[str, Any] | None = None
    params_b: dict[str, Any] | None = None


ITEM_PAIRS = (
    RulePair(
        "equality_operand_symmetry",
        "comparison_operand_symmetry",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE 42 = n.key RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE 42 = n.key RETURN n.id AS id ORDER BY id",
        "equality is symmetric for the same finite integer property and literal",
    ),
    RulePair(
        "range_operand_symmetry",
        "comparison_operand_symmetry",
        "MATCH (n:Item) WHERE n.key > 900 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE 900 < n.key RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key > 900 RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE 900 < n.key RETURN n.id AS id ORDER BY id",
        "reversing operands and the inequality direction preserves comparison and null",
    ),
    RulePair(
        "constant_expression_equality",
        "constant_folding",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 40 + 2 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE n.key = 40 + 2 RETURN n.id AS id ORDER BY id",
        "the closed integer expression 40 + 2 evaluates to the literal 42",
    ),
    RulePair(
        "literal_parameter_equality",
        "parameter_binding",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = $value RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE n.key = $value RETURN n.id AS id ORDER BY id",
        "the parameter is fixed to the same integer literal for every execution",
        params_b={"value": 42},
    ),
    RulePair(
        "static_subscript_property_lookup",
        "property_access_form",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n['key'] = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE n['key'] = 42 RETURN n.id AS id ORDER BY id",
        "the subscript contains the same static property name",
    ),
    RulePair(
        "property_existence_spelling",
        "property_existence",
        "MATCH (n:Item) WHERE n.key IS NOT NULL RETURN count(*) AS c",
        "MATCH (n:Item) WHERE exists(n.key) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IS NOT NULL RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE exists(n.key) RETURN n.id AS id ORDER BY id",
        "both forms select nodes whose property value is present and non-null",
    ),
    RulePair(
        "duplicate_in_element_idempotence",
        "membership_idempotence",
        "MATCH (n:Item) WHERE n.key IN [42] RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IN [42, 42] RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IN [42] RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE n.key IN [42, 42] RETURN n.id AS id ORDER BY id",
        "duplicating an element does not change list-membership truth, including null",
    ),
    RulePair(
        "in_list_permutation_symmetry",
        "membership_order",
        "MATCH (n:Item) WHERE n.key IN [42, 43, 44] RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IN [44, 43, 42] RETURN count(*) AS c",
        (
            "MATCH (n:Item) WHERE n.key IN [42, 43, 44] "
            "RETURN n.id AS id ORDER BY id"
        ),
        (
            "MATCH (n:Item) WHERE n.key IN [44, 43, 42] "
            "RETURN n.id AS id ORDER BY id"
        ),
        "permuting a finite non-null membership list preserves truth",
    ),
    RulePair(
        "null_predicate_negation",
        "null_predicate_normalization",
        "MATCH (n:Item) WHERE n.key IS NOT NULL RETURN count(*) AS c",
        "MATCH (n:Item) WHERE NOT (n.key IS NULL) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IS NOT NULL RETURN n.id AS id ORDER BY id",
        "MATCH (n:Item) WHERE NOT (n.key IS NULL) RETURN n.id AS id ORDER BY id",
        "IS NOT NULL is the negation of IS NULL",
    ),
    RulePair(
        "constant_function_equality",
        "constant_function_folding",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = toInteger('42') RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        (
            "MATCH (n:Item) WHERE n.key = toInteger('42') "
            "RETURN n.id AS id ORDER BY id"
        ),
        "the closed conversion expression evaluates to the same integer literal",
    ),
    RulePair(
        "converted_parameter_equality",
        "parameter_expression",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = toInteger($value) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        (
            "MATCH (n:Item) WHERE n.key = toInteger($value) "
            "RETURN n.id AS id ORDER BY id"
        ),
        "the parameter is fixed to the string representation of the same integer",
        params_b={"value": "42"},
    ),
    RulePair(
        "coalesced_parameter_equality",
        "parameter_expression",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = coalesce($value, -1) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN n.id AS id ORDER BY id",
        (
            "MATCH (n:Item) WHERE n.key = coalesce($value, -1) "
            "RETURN n.id AS id ORDER BY id"
        ),
        "the parameter is fixed to the same non-null integer for every execution",
        params_b={"value": 42},
    ),
    RulePair(
        "count_star_literal_identity",
        "aggregation_argument",
        "MATCH (n:Item) RETURN count(*) AS c",
        "MATCH (n:Item) RETURN count(1) AS c",
        "MATCH (n:Item) RETURN count(*) AS c",
        "MATCH (n:Item) RETURN count(1) AS c",
        "the literal 1 is non-null for every input row",
    ),
    RulePair(
        "min_distinct_idempotence",
        "aggregate_distinct_idempotence",
        "MATCH (n:Item) RETURN min(n.id) AS value",
        "MATCH (n:Item) RETURN min(DISTINCT n.id) AS value",
        "MATCH (n:Item) RETURN min(n.id) AS value",
        "MATCH (n:Item) RETURN min(DISTINCT n.id) AS value",
        "duplicates cannot change the minimum value, including null and empty inputs",
    ),
    RulePair(
        "max_distinct_idempotence",
        "aggregate_distinct_idempotence",
        "MATCH (n:Item) RETURN max(n.id) AS value",
        "MATCH (n:Item) RETURN max(DISTINCT n.id) AS value",
        "MATCH (n:Item) RETURN max(n.id) AS value",
        "MATCH (n:Item) RETURN max(DISTINCT n.id) AS value",
        "duplicates cannot change the maximum value, including null and empty inputs",
    ),
    RulePair(
        "zero_hop_path_identity",
        "zero_hop_path",
        "MATCH (n:Item) RETURN count(*) AS c",
        "MATCH (n:Item)-[:UNUSED*0..0]->(m:Item) RETURN count(*) AS c",
        (
            "MATCH (n:Item) RETURN n.id AS n, n.id AS m "
            "ORDER BY n, m"
        ),
        (
            "MATCH (n:Item)-[:UNUSED*0..0]->(m:Item) "
            "RETURN n.id AS n, m.id AS m ORDER BY n, m"
        ),
        "an exact zero-hop path binds both endpoints to the same node",
    ),
)


LABEL_PAIRS = (
    RulePair(
        "multi_label_order_symmetry",
        "multi_label_pattern",
        "MATCH (n:Common:Rare) RETURN count(*) AS c",
        "MATCH (n:Rare:Common) RETURN count(*) AS c",
        "MATCH (n:Common:Rare) RETURN n.id AS id ORDER BY id",
        "MATCH (n:Rare:Common) RETURN n.id AS id ORDER BY id",
        "label conjunction is commutative",
    ),
    RulePair(
        "multi_label_pattern_where_symmetry",
        "multi_label_constraint_placement",
        "MATCH (n:Common:Rare) RETURN count(*) AS c",
        "MATCH (n:Rare) WHERE n:Common RETURN count(*) AS c",
        "MATCH (n:Common:Rare) RETURN n.id AS id ORDER BY id",
        "MATCH (n:Rare) WHERE n:Common RETURN n.id AS id ORDER BY id",
        "mandatory MATCH with the same two static labels",
    ),
    RulePair(
        "label_conjunction_order_symmetry",
        "label_predicate_order",
        "MATCH (n) WHERE n:Common AND n:Rare RETURN count(*) AS c",
        "MATCH (n) WHERE n:Rare AND n:Common RETURN count(*) AS c",
        "MATCH (n) WHERE n:Common AND n:Rare RETURN n.id AS id ORDER BY id",
        "MATCH (n) WHERE n:Rare AND n:Common RETURN n.id AS id ORDER BY id",
        "conjunction of two pure label predicates is commutative",
    ),
    RulePair(
        "comma_pattern_order_symmetry",
        "comma_pattern_order",
        "MATCH (r:Rare), (c:Common) WHERE c.id < 10 RETURN count(*) AS c",
        "MATCH (c:Common), (r:Rare) WHERE c.id < 10 RETURN count(*) AS c",
        (
            "MATCH (r:Rare), (c:Common) WHERE c.id < 10 "
            "RETURN r.id AS r, c.id AS c ORDER BY r, c"
        ),
        (
            "MATCH (c:Common), (r:Rare) WHERE c.id < 10 "
            "RETURN r.id AS r, c.id AS c ORDER BY r, c"
        ),
        "the two mandatory node patterns are independent",
    ),
    RulePair(
        "duplicate_label_idempotence",
        "label_idempotence",
        "MATCH (n:Rare) RETURN count(*) AS c",
        "MATCH (n:Rare:Rare) RETURN count(*) AS c",
        "MATCH (n:Rare) RETURN n.id AS id ORDER BY id",
        "MATCH (n:Rare:Rare) RETURN n.id AS id ORDER BY id",
        "requiring the same label twice is equivalent to requiring it once",
    ),
)


EDGE_PAIRS = (
    RulePair(
        "edge_property_map_where_symmetry",
        "edge_property_constraint_placement",
        "MATCH ()-[r:LINK {key: 42}]->() RETURN count(r) AS c",
        "MATCH ()-[r:LINK]->() WHERE r.key = 42 RETURN count(r) AS c",
        (
            "MATCH (a)-[r:LINK {key: 42}]->(b) "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        (
            "MATCH (a)-[r:LINK]->(b) WHERE r.key = 42 "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        "mandatory one-hop MATCH with one static edge property",
    ),
    RulePair(
        "edge_equality_operand_symmetry",
        "edge_comparison_operand_symmetry",
        "MATCH ()-[r:LINK]->() WHERE r.key = 42 RETURN count(r) AS c",
        "MATCH ()-[r:LINK]->() WHERE 42 = r.key RETURN count(r) AS c",
        (
            "MATCH (a)-[r:LINK]->(b) WHERE r.key = 42 "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        (
            "MATCH (a)-[r:LINK]->(b) WHERE 42 = r.key "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        "edge-property equality is symmetric",
    ),
    RulePair(
        "edge_literal_parameter_equality",
        "edge_parameter_binding",
        "MATCH ()-[r:LINK]->() WHERE r.key = 42 RETURN count(r) AS c",
        "MATCH ()-[r:LINK]->() WHERE r.key = $value RETURN count(r) AS c",
        (
            "MATCH (a)-[r:LINK]->(b) WHERE r.key = 42 "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        (
            "MATCH (a)-[r:LINK]->(b) WHERE r.key = $value "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        "the parameter is fixed to the same integer literal for every execution",
        params_b={"value": 42},
    ),
    RulePair(
        "optional_node_property_placement",
        "optional_match_constraint_placement",
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B {id: 42}) "
            "RETURN count(b) AS c"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B) "
            "WHERE b.id = 42 RETURN count(b) AS c"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B {id: 42}) "
            "RETURN a.id AS a, b.id AS b ORDER BY a, b"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B) "
            "WHERE b.id = 42 RETURN a.id AS a, b.id AS b ORDER BY a, b"
        ),
        "the WHERE belongs to the same OPTIONAL MATCH and uses one static node property",
    ),
    RulePair(
        "optional_edge_property_placement",
        "optional_match_constraint_placement",
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK {key: 42}]->(b:B) "
            "RETURN count(r) AS c"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B) "
            "WHERE r.key = 42 RETURN count(r) AS c"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK {key: 42}]->(b:B) "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        (
            "MATCH (a:A) OPTIONAL MATCH (a)-[r:LINK]->(b:B) "
            "WHERE r.key = 42 RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        "the WHERE belongs to the same OPTIONAL MATCH and uses one static edge property",
    ),
    RulePair(
        "directed_single_hop_endpoint_symmetry",
        "directed_endpoint_order",
        "MATCH (a:A)-[r:LINK]->(b:B) WHERE b.id < 10 RETURN count(r) AS c",
        "MATCH (b:B)<-[r:LINK]-(a:A) WHERE b.id < 10 RETURN count(r) AS c",
        (
            "MATCH (a:A)-[r:LINK]->(b:B) WHERE b.id < 10 "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        (
            "MATCH (b:B)<-[r:LINK]-(a:A) WHERE b.id < 10 "
            "RETURN a.id AS a, b.id AS b, r.key AS k ORDER BY a, b, k"
        ),
        "the same directed one-hop pattern is written from the opposite endpoint",
    ),
    RulePair(
        "unused_path_binding_identity",
        "path_binding",
        "MATCH (a:A)-[:LINK]->(b:B) WHERE b.id < 10 RETURN count(*) AS c",
        "MATCH p=(a:A)-[:LINK]->(b:B) WHERE b.id < 10 RETURN count(*) AS c",
        (
            "MATCH (a:A)-[:LINK]->(b:B) WHERE b.id < 10 "
            "RETURN a.id AS a, b.id AS b ORDER BY a, b"
        ),
        (
            "MATCH p=(a:A)-[:LINK]->(b:B) WHERE b.id < 10 "
            "RETURN a.id AS a, b.id AS b ORDER BY a, b"
        ),
        "the newly bound path variable is not observed by any later clause",
    ),
)


def run(
    session: Any,
    text: str,
    parameters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def best_effort(session: Any, statement: str) -> None:
    try:
        run(session, statement)
    except Exception:
        pass


def clear_graph_and_indexes(session: Any) -> None:
    run(session, "MATCH (n) DETACH DELETE n")
    for statement in (
        "DROP INDEX ON :Item(key)",
        "DROP INDEX ON :Common",
        "DROP INDEX ON :Rare",
        "DROP INDEX ON :Common(id)",
        "DROP INDEX ON :A(id)",
        "DROP INDEX ON :B(id)",
        "DROP EDGE INDEX ON :LINK(key)",
    ):
        best_effort(session, statement)


def setup_items(session: Any, count: int) -> dict[str, int]:
    clear_graph_and_indexes(session)
    run(session, "CREATE INDEX ON :Item(key)")
    for start in range(0, count, 5_000):
        end = min(count, start + 5_000)
        run(
            session,
            "UNWIND range($start, $end - 1) AS i "
            "CREATE (:Item {id: i, key: i % 1000})",
            {"start": start, "end": end},
        )
    missing = max(100, count // 100)
    for start in range(0, missing, 5_000):
        end = min(missing, start + 5_000)
        run(
            session,
            "UNWIND range($start, $end - 1) AS i "
            "CREATE (:Item {id: $offset + i})",
            {"start": start, "end": end, "offset": count},
        )
    return {"nodes_with_key": count, "nodes_without_key": missing}


def setup_labels(session: Any, common: int, rare: int) -> dict[str, int]:
    clear_graph_and_indexes(session)
    run(session, "CREATE INDEX ON :Common")
    run(session, "CREATE INDEX ON :Rare")
    run(session, "CREATE INDEX ON :Common(id)")
    for start in range(0, common, 5_000):
        end = min(common, start + 5_000)
        run(
            session,
            "UNWIND range($start, $end - 1) AS i CREATE (:Common {id: i})",
            {"start": start, "end": end},
        )
    run(session, "MATCH (n:Common) WHERE n.id < $rare SET n:Rare", {"rare": rare})
    return {"common_nodes": common, "common_and_rare_nodes": rare}


def setup_edges(session: Any, nodes: int, edges_per_node: int) -> dict[str, int]:
    clear_graph_and_indexes(session)
    run(session, "CREATE INDEX ON :A(id)")
    run(session, "CREATE INDEX ON :B(id)")
    run(session, "CREATE EDGE INDEX ON :LINK(key)")
    for label in ("A", "B"):
        for start in range(0, nodes, 5_000):
            end = min(nodes, start + 5_000)
            run(
                session,
                f"UNWIND range($start, $end - 1) AS i CREATE (:{label} {{id: i}})",
                {"start": start, "end": end},
            )
    for start in range(0, nodes, 500):
        end = min(nodes, start + 500)
        run(
            session,
            """
            MATCH (a:A)
            WHERE a.id >= $start AND a.id < $end
            UNWIND range(0, $edges_per_node - 1) AS offset
            MATCH (b:B {id: (a.id + offset) % $nodes})
            CREATE (a)-[:LINK {key: (a.id + offset) % 1000}]->(b)
            """,
            {
                "start": start,
                "end": end,
                "edges_per_node": edges_per_node,
                "nodes": nodes,
            },
        )
    return {
        "a_nodes": nodes,
        "b_nodes": nodes,
        "link_edges": nodes * edges_per_node,
        "edges_per_a": edges_per_node,
    }


def explain(
    session: Any,
    text: str,
    parameters: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows, _ = run(session, f"EXPLAIN {text}", parameters)
    return rows


def measure_pair(
    session: Any,
    pair: RulePair,
    *,
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    try:
        rows_a, _ = run(session, pair.rows_a, pair.params_a)
        rows_b, _ = run(session, pair.rows_b, pair.params_b)
    except Exception as exc:
        return {**asdict(pair), "supported": False, "error": str(exc)}
    if rows_a != rows_b:
        return {
            **asdict(pair),
            "supported": True,
            "equivalent": False,
            "rows_a_count": len(rows_a),
            "rows_b_count": len(rows_b),
            "rows_a_sample": rows_a[:20],
            "rows_b_sample": rows_b[:20],
        }

    try:
        for _ in range(3):
            run(session, pair.query_a, pair.params_a)
            run(session, pair.query_b, pair.params_b)
        orders = ["AB", "BA"] * repeats
        random.Random(f"{seed}:{pair.rule_id}").shuffle(orders)
        timings: list[dict[str, Any]] = []
        for order in orders:
            item: dict[str, Any] = {"order": order}
            for side in order:
                _, elapsed = run(
                    session,
                    pair.query_a if side == "A" else pair.query_b,
                    pair.params_a if side == "A" else pair.params_b,
                )
                item[f"{side.lower()}_ms"] = elapsed
            timings.append(item)
        a_values = [item["a_ms"] for item in timings]
        b_values = [item["b_ms"] for item in timings]
        median_a = statistics.median(a_values)
        median_b = statistics.median(b_values)
        return {
            **asdict(pair),
            "supported": True,
            "equivalent": True,
            "matched_rows": len(rows_a),
            "median_a_ms": median_a,
            "median_b_ms": median_b,
            "slower": "a" if median_a > median_b else "b",
            "ratio": max(median_a, median_b) / max(min(median_a, median_b), 1e-9),
            "absolute_delta_ms": abs(median_a - median_b),
            "timings": timings,
            "plan_a": explain(session, pair.query_a, pair.params_a),
            "plan_b": explain(session, pair.query_b, pair.params_b),
        }
    except Exception as exc:
        return {
            **asdict(pair),
            "supported": False,
            "equivalent": True,
            "matched_rows": len(rows_a),
            "error": str(exc),
        }


def screen_group(
    session: Any,
    *,
    group: str,
    setup: Callable[[], dict[str, int]],
    pairs: tuple[RulePair, ...],
    repeats: int,
    seed: int,
) -> dict[str, Any]:
    graph = setup()
    results = [measure_pair(session, pair, repeats=repeats, seed=seed) for pair in pairs]
    print(group)
    for result in results:
        if not result.get("supported"):
            print(f"  {result['rule_id']}: unsupported: {result.get('error', '')}")
        elif not result.get("equivalent"):
            print(
                f"  {result['rule_id']}: LOGICAL MISMATCH "
                f"rows={result['rows_a_count']}/{result['rows_b_count']}"
            )
        else:
            print(
                f"  {result['rule_id']}: "
                f"A={result['median_a_ms']:.3f}ms B={result['median_b_ms']:.3f}ms "
                f"ratio={result['ratio']:.2f} delta={result['absolute_delta_ms']:.3f}ms"
            )
    return {"group": group, "graph": graph, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--items", type=int, default=200_000)
    parser.add_argument("--common", type=int, default=200_000)
    parser.add_argument("--rare", type=int, default=200)
    parser.add_argument("--edge-nodes", type=int, default=5_000)
    parser.add_argument("--edges-per-node", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--groups", default="items,labels,edges")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.items, args.common, args.rare, args.edge_nodes, args.edges_per_node, args.repeats) <= 0:
        parser.error("all scales and repeats must be positive")
    if args.rare > args.common:
        parser.error("rare cannot exceed common")
    selected = {item for item in args.groups.split(",") if item}
    unknown = selected - {"items", "labels", "edges"}
    if unknown:
        parser.error(f"unknown groups: {sorted(unknown)}")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            groups: list[dict[str, Any]] = []
            if "items" in selected:
                groups.append(
                    screen_group(
                        session,
                        group="items",
                        setup=lambda: setup_items(session, args.items),
                        pairs=ITEM_PAIRS,
                        repeats=args.repeats,
                        seed=args.seed,
                    )
                )
            if "labels" in selected:
                groups.append(
                    screen_group(
                        session,
                        group="labels",
                        setup=lambda: setup_labels(session, args.common, args.rare),
                        pairs=LABEL_PAIRS,
                        repeats=args.repeats,
                        seed=args.seed,
                    )
                )
            if "edges" in selected:
                groups.append(
                    screen_group(
                        session,
                        group="edges",
                        setup=lambda: setup_edges(session, args.edge_nodes, args.edges_per_node),
                        pairs=EDGE_PAIRS,
                        repeats=args.repeats,
                        seed=args.seed,
                    )
                )
        payload = {
            "schema_version": 1,
            "purpose": "targeted discovery screen; not submit-ready validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "uri": args.uri,
            "repeats_per_order": args.repeats,
            "groups": groups,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
