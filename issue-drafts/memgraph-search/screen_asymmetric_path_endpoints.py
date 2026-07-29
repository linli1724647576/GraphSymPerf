#!/usr/bin/env python3
"""Screen fixed-path endpoint symmetry on an asymmetric Memgraph graph."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


FORWARD = (
    "MATCH (s:Start)-[:R1]->(m:Mid)-[:R2]->(e:End) "
    "RETURN count(*) AS c"
)
REVERSED = (
    "MATCH (e:End)<-[:R2]-(m:Mid)<-[:R1]-(s:Start) "
    "RETURN count(*) AS c"
)
FORWARD_ROWS = (
    "MATCH (s:Start)-[:R1]->(m:Mid)-[:R2]->(e:End) "
    "RETURN s.id AS s, m.id AS m, e.id AS e ORDER BY s, m, e"
)
REVERSED_ROWS = (
    "MATCH (e:End)<-[:R2]-(m:Mid)<-[:R1]-(s:Start) "
    "RETURN s.id AS s, m.id AS m, e.id AS e ORDER BY s, m, e"
)


def run(session: Any, text: str, parameters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows, (time.perf_counter() - started) * 1000.0


def set_up(session: Any, width: int, end_count: int) -> dict[str, int]:
    run(session, "MATCH (n) DETACH DELETE n")
    for statement in (
        "DROP INDEX ON :Start",
        "DROP INDEX ON :Mid",
        "DROP INDEX ON :End",
        "DROP INDEX ON :Start(id)",
        "DROP INDEX ON :Mid(id)",
        "DROP INDEX ON :End(id)",
    ):
        try:
            run(session, statement)
        except Exception:
            pass
    for statement in (
        "CREATE INDEX ON :Start",
        "CREATE INDEX ON :Mid",
        "CREATE INDEX ON :End",
        "CREATE INDEX ON :Start(id)",
        "CREATE INDEX ON :Mid(id)",
        "CREATE INDEX ON :End(id)",
    ):
        run(session, statement)
    for label, count in (("Start", width), ("Mid", width), ("End", end_count)):
        for start in range(0, count, 5_000):
            end = min(count, start + 5_000)
            run(
                session,
                f"UNWIND range($start, $end - 1) AS i CREATE (:{label} {{id: i}})",
                {"start": start, "end": end},
            )
    for start in range(0, width, 1_000):
        end = min(width, start + 1_000)
        run(
            session,
            """
            MATCH (s:Start)
            WHERE s.id >= $start AND s.id < $end
            UNWIND range(
              s.id,
              CASE WHEN s.id + 7 < $width THEN s.id + 7 ELSE $width - 1 END
            ) AS target
            MATCH (m:Mid {id: target})
            CREATE (s)-[:R1]->(m)
            """,
            {"start": start, "end": end, "width": width},
        )
    run(
        session,
        "MATCH (m:Mid), (e:End {id: m.id}) WHERE m.id < $end_count CREATE (m)-[:R2]->(e)",
        {"end_count": end_count},
    )
    nodes, _ = run(session, "MATCH (n) RETURN count(n) AS c")
    edges, _ = run(session, "MATCH ()-[r]->() RETURN count(r) AS c")
    return {"nodes": nodes[0]["c"], "relationships": edges[0]["c"]}


def measure(session: Any, repeats: int, seed: int) -> dict[str, Any]:
    rows_a, _ = run(session, FORWARD)
    rows_b, _ = run(session, REVERSED)
    if rows_a != rows_b:
        return {"equivalent": False, "rows_a": rows_a, "rows_b": rows_b}
    for _ in range(3):
        run(session, FORWARD)
        run(session, REVERSED)
    orders = ["AB", "BA"] * repeats
    random.Random(seed).shuffle(orders)
    timings: list[dict[str, Any]] = []
    for order in orders:
        item: dict[str, Any] = {"order": order}
        for side in order:
            _, elapsed = run(session, FORWARD if side == "A" else REVERSED)
            item[f"{side.lower()}_ms"] = elapsed
        timings.append(item)
    a = statistics.median(item["a_ms"] for item in timings)
    b = statistics.median(item["b_ms"] for item in timings)
    return {
        "equivalent": True,
        "rows": rows_a,
        "median_forward_ms": a,
        "median_reversed_ms": b,
        "ratio": max(a, b) / max(min(a, b), 1e-9),
        "slower": "forward" if a > b else "reversed",
        "delta_ms": abs(a - b),
        "timings": timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--width", type=int, default=20_000)
    parser.add_argument("--ends", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.width, args.ends, args.repeats) <= 0 or args.ends > args.width:
        parser.error("width, ends, and repeats must be positive; ends cannot exceed width")

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version, _ = run(session, "SHOW VERSION")
            graph = set_up(session, args.width, args.ends)
            forward_rows, _ = run(session, FORWARD_ROWS)
            reversed_rows, _ = run(session, REVERSED_ROWS)
            timing = measure(session, args.repeats, args.seed)
            forward_plan, _ = run(session, f"EXPLAIN {FORWARD}")
            reversed_plan, _ = run(session, f"EXPLAIN {REVERSED}")
        payload = {
            "schema_version": 1,
            "purpose": "targeted discovery screen; not submit-ready validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "width": args.width,
            "ends": args.ends,
            "graph": graph,
            "exact_rows_equal": forward_rows == reversed_rows,
            "matched_rows": len(forward_rows),
            "forward_query": FORWARD,
            "reversed_query": REVERSED,
            "timing": timing,
            "forward_plan": forward_plan,
            "reversed_plan": reversed_plan,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"exact={payload['exact_rows_equal']} matches={payload['matched_rows']} "
            f"forward={timing['median_forward_ms']:.3f}ms "
            f"reversed={timing['median_reversed_ms']:.3f}ms "
            f"ratio={timing['ratio']:.2f} slower={timing['slower']}"
        )
        print(f"wrote {args.output}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
