#!/usr/bin/env python3
"""Standalone balanced-timing reproducer for the minimized ArcadeDB query pair."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

import requests


QUERY_A = "WITH 1 AS x MATCH (a)-[:T1]->(b), (c)-[:T4]->(d), (n) RETURN count(*) AS c"
QUERY_B = "WITH 1 AS x MATCH (a)-[:T1]->(b), (n), (c)-[:T4]->(d) RETURN count(*) AS c"


def _database_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None:
        raise ValueError(f"unsafe database name: {value!r}")
    return value


def _server_command(session: requests.Session, base_url: str, command: str) -> requests.Response:
    return session.post(
        f"{base_url}/api/v1/server",
        json={"command": command},
        timeout=60,
    )


def _request(
    session: requests.Session,
    base_url: str,
    database: str,
    endpoint: str,
    query: str,
) -> tuple[list[dict[str, Any]], float]:
    started = perf_counter()
    response = session.post(
        f"{base_url}/api/v1/{endpoint}/{database}",
        json={"language": "opencypher", "command": query},
        timeout=60,
    )
    elapsed_ms = (perf_counter() - started) * 1000.0
    response.raise_for_status()
    rows = response.json().get("result", [])
    normalized = [
        {key: value for key, value in row.items() if not key.startswith("@")}
        for row in rows
    ]
    return normalized, elapsed_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2480")
    parser.add_argument("--user", default=os.environ.get("ARCADEDB_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("ARCADEDB_PASSWORD"))
    parser.add_argument("--database", default="arcadedb_pattern_order_perf_repro")
    parser.add_argument("--setup", type=Path, default=Path(__file__).with_name("setup.cypher"))
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--pairs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.password:
        parser.error("provide --password or ARCADEDB_PASSWORD")
    if args.warmups < 0 or args.pairs <= 0 or args.pairs % 2:
        parser.error("--warmups must be non-negative and --pairs must be a positive even number")

    database = _database_name(args.database)
    base_url = args.url.rstrip("/")
    statements = tuple(
        line.strip()
        for line in args.setup.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    session = requests.Session()
    session.auth = (args.user, args.password)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    try:
        _server_command(session, base_url, f"drop database {database}")
        created = _server_command(session, base_url, f"create database {database}")
        created.raise_for_status()
        for statement in statements:
            _request(session, base_url, database, "command", statement)

        counts = {}
        for name, query in {
            "nodes": "MATCH (n) RETURN count(*) AS c",
            "relationships": "MATCH ()-[r]->() RETURN count(*) AS c",
            "T1": "MATCH ()-[r:T1]->() RETURN count(r) AS c",
            "T4": "MATCH ()-[r:T4]->() RETURN count(r) AS c",
        }.items():
            rows, _ = _request(session, base_url, database, "query", query)
            counts[name] = rows[0]["c"]

        result_a, _ = _request(session, base_url, database, "query", QUERY_A)
        result_b, _ = _request(session, base_url, database, "query", QUERY_B)
        if result_a != result_b:
            raise RuntimeError(f"query results differ: A={result_a!r}, B={result_b!r}")

        for _ in range(args.warmups):
            _request(session, base_url, database, "query", QUERY_A)
            _request(session, base_url, database, "query", QUERY_B)
        schedule = ["AB"] * (args.pairs // 2) + ["BA"] * (args.pairs // 2)
        random.Random(args.seed).shuffle(schedule)
        samples = []
        for order in schedule:
            if order == "AB":
                _, a_ms = _request(session, base_url, database, "query", QUERY_A)
                _, b_ms = _request(session, base_url, database, "query", QUERY_B)
            else:
                _, b_ms = _request(session, base_url, database, "query", QUERY_B)
                _, a_ms = _request(session, base_url, database, "query", QUERY_A)
            samples.append({"order": order, "a_ms": a_ms, "b_ms": b_ms})

        ratios = [sample["b_ms"] / sample["a_ms"] for sample in samples]
        deltas = [sample["b_ms"] - sample["a_ms"] for sample in samples]
        print(
            json.dumps(
                {
                    "counts": counts,
                    "result": result_a,
                    "warmups": args.warmups,
                    "pairs": args.pairs,
                    "AB": schedule.count("AB"),
                    "BA": schedule.count("BA"),
                    "median_a_ms": median(sample["a_ms"] for sample in samples),
                    "median_b_ms": median(sample["b_ms"] for sample in samples),
                    "median_paired_ratio": median(ratios),
                    "median_paired_delta_ms": median(deltas),
                    "b_slower_fraction": sum(delta > 0 for delta in deltas) / len(deltas),
                    "samples": samples,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        if args.cleanup:
            _server_command(session, base_url, f"drop database {database}")
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
