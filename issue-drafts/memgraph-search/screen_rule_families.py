#!/usr/bin/env python3
"""Screen under-covered Memgraph equivalence-rule families.

This is a targeted discovery harness, not a submit-ready validator.  It uses
one disposable synthetic graph, checks the scalar result before timing, and
alternates A/B execution order.  Any hit must still pass fresh-process,
fresh-load validation before it can become an issue.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


@dataclass(frozen=True)
class RulePair:
    rule_id: str
    family: str
    query_a: str
    query_b: str
    proof_boundary: str


PAIRS = (
    RulePair(
        "double_negation_predicate",
        "boolean_normalization",
        "MATCH (n:Item) WHERE n.flag RETURN count(*) AS c",
        "MATCH (n:Item) WHERE NOT NOT n.flag RETURN count(*) AS c",
        "Kleene double negation over a pure property predicate",
    ),
    RulePair(
        "de_morgan_predicate",
        "boolean_normalization",
        "MATCH (n:Item) WHERE NOT (n.flag OR n.bucket = 7) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE NOT n.flag AND NOT (n.bucket = 7) RETURN count(*) AS c",
        "Kleene De Morgan identity over pure property predicates",
    ),
    RulePair(
        "case_true_experimental",
        "boolean_normalization",
        "MATCH (n:Item) WHERE n.flag RETURN count(*) AS c",
        "MATCH (n:Item) WHERE CASE WHEN n.flag THEN true ELSE false END RETURN count(*) AS c",
        "WHERE selection only; null and false both reject the row",
    ),
    RulePair(
        "numeric_equality_range",
        "numeric_predicate",
        "MATCH (n:Item) WHERE n.score = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.score >= 42 AND n.score <= 42 RETURN count(*) AS c",
        "same finite integer property and integer literal; no cast or overflow",
    ),
    RulePair(
        "property_map_where_symmetry",
        "pattern_predicate",
        "MATCH (n:Item {key: 42}) RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "ordinary mandatory MATCH with one static integer property",
    ),
    RulePair(
        "label_where_symmetry",
        "pattern_predicate",
        "MATCH (n:Rare) RETURN count(*) AS c",
        "MATCH (n) WHERE n:Rare RETURN count(*) AS c",
        "ordinary mandatory MATCH with one static label",
    ),
    RulePair(
        "single_hop_varlength_symmetry",
        "path_shape",
        "MATCH (a:A)-[:LINK]->(b:B) RETURN count(*) AS c",
        "MATCH (a:A)-[:LINK*1..1]->(b:B) RETURN count(*) AS c",
        "exactly one hop; path and relationship-list values are unobserved",
    ),
    RulePair(
        "node_order_symmetry",
        "path_shape",
        "MATCH (a:A)-[r:LINK]-(b:B) RETURN count(r) AS c",
        "MATCH (b:B)-[r:LINK]-(a:A) RETURN count(r) AS c",
        "undirected edge with variables and labels preserved",
    ),
    RulePair(
        "path_endpoint_symmetry",
        "path_shape",
        "MATCH (a:A)-[:LINK]->(b:B)-[:BACK]->(c:C) RETURN count(*) AS c",
        "MATCH (c:C)<-[:BACK]-(b:B)<-[:LINK]-(a:A) RETURN count(*) AS c",
        "same fixed directed path written from the opposite endpoint",
    ),
    RulePair(
        "independent_mandatory_match_reorder",
        "clause_order",
        "MATCH (a:Rare) MATCH (b:Wide) WHERE b.bucket = 7 RETURN count(*) AS c",
        "MATCH (b:Wide) WHERE b.bucket = 7 MATCH (a:Rare) RETURN count(*) AS c",
        "dependency-free mandatory MATCH clauses; filter remains attached to b",
    ),
    RulePair(
        "predicate_order_symmetry_targeted",
        "predicate_order",
        "MATCH (n:Item) WHERE n.bucket = 7 AND n.flag = true RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.flag = true AND n.bucket = 7 RETURN count(*) AS c",
        "pure total conjunction terms",
    ),
    RulePair(
        "singleton_in_equality",
        "membership_predicate",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IN [42] RETURN count(*) AS c",
        "one finite integer property and one same-typed non-null literal",
    ),
    RulePair(
        "finite_in_disjunction",
        "membership_predicate",
        "MATCH (n:Item) WHERE n.key = 42 OR n.key = 43 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key IN [42, 43] RETURN count(*) AS c",
        "one finite integer property and same-typed non-null literals",
    ),
    RulePair(
        "relationship_type_where_symmetry",
        "pattern_predicate",
        "MATCH (a:A)-[r:LINK]->(b:B) RETURN count(r) AS c",
        "MATCH (a:A)-[r]->(b:B) WHERE type(r) = 'LINK' RETURN count(r) AS c",
        "ordinary mandatory MATCH with one static relationship type",
    ),
    RulePair(
        "bound_count_identity",
        "aggregation",
        "MATCH (n:Item) RETURN count(*) AS c",
        "MATCH (n:Item) RETURN count(n) AS c",
        "n is introduced by mandatory MATCH and therefore cannot be null",
    ),
    RulePair(
        "indexed_double_negation_predicate",
        "boolean_normalization",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE NOT NOT (n.key = 42) RETURN count(*) AS c",
        "Kleene double negation over a pure same-typed indexed equality",
    ),
    RulePair(
        "negated_inequality_normalization",
        "comparison_normalization",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE NOT (n.key <> 42) RETURN count(*) AS c",
        "same finite integer property and literal; null remains rejected",
    ),
    RulePair(
        "integer_add_zero_identity",
        "arithmetic_normalization",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        "MATCH (n:Item) WHERE n.key + 0 = 42 RETURN count(*) AS c",
        "finite integer property; adding zero preserves value and null",
    ),
    RulePair(
        "case_equality_selection",
        "boolean_normalization",
        "MATCH (n:Item) WHERE n.key = 42 RETURN count(*) AS c",
        (
            "MATCH (n:Item) "
            "WHERE CASE WHEN n.key = 42 THEN true ELSE false END "
            "RETURN count(*) AS c"
        ),
        "WHERE selection only; null and false both reject the row",
    ),
)


def _run(session: Any, text: str, parameters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def _setup(driver: Any, item_count: int, path_count: int) -> dict[str, int]:
    with driver.session() as session:
        _run(session, "MATCH (n) DETACH DELETE n")
        for statement in (
            "DROP INDEX ON :Item(key)",
            "DROP INDEX ON :Item(score)",
            "DROP INDEX ON :Item(bucket)",
            "DROP INDEX ON :Wide(bucket)",
            "DROP INDEX ON :A(id)",
            "DROP INDEX ON :B(id)",
            "DROP INDEX ON :C(id)",
        ):
            try:
                _run(session, statement)
            except Exception:
                pass
        for statement in (
            "CREATE INDEX ON :Item(key)",
            "CREATE INDEX ON :Item(score)",
            "CREATE INDEX ON :Item(bucket)",
            "CREATE INDEX ON :Wide(bucket)",
            "CREATE INDEX ON :A(id)",
            "CREATE INDEX ON :B(id)",
            "CREATE INDEX ON :C(id)",
        ):
            _run(session, statement)

        chunk = 5_000
        for start in range(0, item_count, chunk):
            end = min(item_count, start + chunk)
            _run(
                session,
                """
                UNWIND range($start, $end - 1) AS i
                CREATE (:Item {
                  id: i,
                  key: i % 1000,
                  score: i % 1000,
                  bucket: i % 100,
                  flag: CASE i % 3 WHEN 0 THEN true WHEN 1 THEN false ELSE null END
                })
                """,
                {"start": start, "end": end},
            )
        _run(
            session,
            "UNWIND range(0, 49) AS i CREATE (:Rare {id: i})",
        )
        _run(
            session,
            "UNWIND range(0, 9999) AS i CREATE (:Wide {id: i, bucket: i % 100})",
        )
        _run(session, "UNWIND range(0, $count - 1) AS i CREATE (:A {id: i})", {"count": path_count})
        _run(session, "UNWIND range(0, $count - 1) AS i CREATE (:B {id: i})", {"count": path_count})
        _run(session, "UNWIND range(0, $count - 1) AS i CREATE (:C {id: i})", {"count": path_count})
        for start in range(0, path_count, 1_000):
            end = min(path_count, start + 1_000)
            _run(
                session,
                """
                MATCH (a:A)
                WHERE a.id >= $start AND a.id < $end
                UNWIND range(
                  a.id,
                  CASE
                    WHEN a.id + 7 < $count THEN a.id + 7
                    ELSE $count - 1
                  END
                ) AS target
                MATCH (b:B {id: target})
                CREATE (a)-[:LINK]->(b)
                """,
                {"start": start, "end": end, "count": path_count},
            )
        _run(
            session,
            "MATCH (b:B), (c:C) WHERE b.id = c.id CREATE (b)-[:BACK]->(c)",
        )
        node_rows, _ = _run(session, "MATCH (n) RETURN count(n) AS c")
        edge_rows, _ = _run(session, "MATCH ()-[r]->() RETURN count(r) AS c")
        return {"nodes": node_rows[0]["c"], "relationships": edge_rows[0]["c"]}


def _graph_counts(driver: Any) -> dict[str, int]:
    with driver.session() as session:
        node_rows, _ = _run(session, "MATCH (n) RETURN count(n) AS c")
        edge_rows, _ = _run(session, "MATCH ()-[r]->() RETURN count(r) AS c")
        return {"nodes": node_rows[0]["c"], "relationships": edge_rows[0]["c"]}


def _screen_pair(session: Any, pair: RulePair, repeats: int, seed: int) -> dict[str, Any]:
    rows_a, _ = _run(session, pair.query_a)
    rows_b, _ = _run(session, pair.query_b)
    equivalent = rows_a == rows_b
    if not equivalent:
        return {
            **asdict(pair),
            "equivalent": False,
            "rows_a": rows_a,
            "rows_b": rows_b,
            "timings": [],
        }

    for _ in range(3):
        _run(session, pair.query_a)
        _run(session, pair.query_b)

    rng = random.Random(f"{seed}:{pair.rule_id}")
    orders = ["AB", "BA"] * repeats
    rng.shuffle(orders)
    timings: list[dict[str, Any]] = []
    for order in orders:
        current: dict[str, Any] = {"order": order}
        for side in order:
            _, elapsed = _run(session, pair.query_a if side == "A" else pair.query_b)
            current[f"{side.lower()}_ms"] = elapsed
        timings.append(current)
    a_values = [item["a_ms"] for item in timings]
    b_values = [item["b_ms"] for item in timings]
    median_a = statistics.median(a_values)
    median_b = statistics.median(b_values)
    slower = "a" if median_a > median_b else "b"
    ratio = max(median_a, median_b) / max(min(median_a, median_b), 1e-9)
    return {
        **asdict(pair),
        "equivalent": True,
        "rows": rows_a,
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "slower": slower,
        "ratio": ratio,
        "absolute_delta_ms": abs(median_a - median_b),
        "timings": timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--items", type=int, default=50_000)
    parser.add_argument("--paths", type=int, default=2_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument(
        "--rules",
        default="",
        help="optional comma-separated rule ids; the default screens every pair",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.items <= 0 or args.paths <= 0 or args.repeats <= 0:
        parser.error("scale and repeats must be positive")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        graph = _graph_counts(driver) if args.skip_setup else _setup(driver, args.items, args.paths)
        with driver.session() as session:
            version_rows, _ = _run(session, "SHOW VERSION")
            selected = set(filter(None, args.rules.split(",")))
            unknown = selected - {pair.rule_id for pair in PAIRS}
            if unknown:
                parser.error(f"unknown rule ids: {sorted(unknown)}")
            results = [
                _screen_pair(session, pair, args.repeats, args.seed)
                for pair in PAIRS
                if not selected or pair.rule_id in selected
            ]
        payload = {
            "schema_version": 1,
            "purpose": "targeted discovery screen; not submit-ready validation",
            "server_agent": getattr(info, "agent", None),
            "version_rows": version_rows,
            "uri": args.uri,
            "items": args.items,
            "paths": args.paths,
            "repeats_per_order": args.repeats,
            "graph": graph,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for result in results:
            if not result["equivalent"]:
                print(f"LOGICAL {result['rule_id']}: {result['rows_a']} != {result['rows_b']}")
            else:
                print(
                    f"{result['rule_id']}: "
                    f"A={result['median_a_ms']:.3f}ms "
                    f"B={result['median_b_ms']:.3f}ms "
                    f"ratio={result['ratio']:.2f} slower={result['slower']}"
                )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
