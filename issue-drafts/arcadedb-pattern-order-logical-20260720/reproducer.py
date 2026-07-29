#!/usr/bin/env python3
"""Minimal ArcadeDB label/order wrong-result reproducer over the HTTP API."""

from __future__ import annotations

import argparse
import os
import re
from typing import Any

import requests


QUERY_A = "MATCH (n:L5), (n) RETURN count(*) AS c"
QUERY_B = "MATCH (n), (n:L5) RETURN count(*) AS c"
SETUP = "CREATE VERTEX TYPE L5; CREATE VERTEX L5"


def _database_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None:
        raise ValueError(f"unsafe database name: {value!r}")
    return value


def _server_command(session: requests.Session, base_url: str, command: str) -> requests.Response:
    return session.post(
        f"{base_url}/api/v1/server",
        json={"command": command},
        timeout=30,
    )


def _query(
    session: requests.Session,
    base_url: str,
    database: str,
    query: str,
) -> list[dict[str, Any]]:
    response = session.post(
        f"{base_url}/api/v1/query/{database}",
        json={"language": "opencypher", "command": query},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json().get("result", [])
    return [
        {key: value for key, value in row.items() if not key.startswith("@")}
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2480")
    parser.add_argument("--user", default=os.environ.get("ARCADEDB_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("ARCADEDB_PASSWORD"))
    parser.add_argument("--database-prefix", default="arcadedb_label_order_repro")
    parser.add_argument("--fresh-databases", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=6)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.password:
        parser.error("provide --password or ARCADEDB_PASSWORD")
    if args.fresh_databases <= 0 or args.iterations <= 0:
        parser.error("--fresh-databases and --iterations must be positive")

    base_url = args.url.rstrip("/")
    session = requests.Session()
    session.auth = (args.user, args.password)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    databases: list[str] = []
    try:
        for database_number in range(1, args.fresh_databases + 1):
            database = _database_name(f"{args.database_prefix}_{database_number}")
            databases.append(database)
            _server_command(session, base_url, f"drop database {database}")
            created = _server_command(session, base_url, f"create database {database}")
            created.raise_for_status()
            setup = session.post(
                f"{base_url}/api/v1/command/{database}",
                json={"language": "sqlscript", "command": SETUP},
                timeout=30,
            )
            setup.raise_for_status()

            for iteration in range(args.iterations):
                order = (("A", QUERY_A), ("B", QUERY_B))
                if iteration % 2:
                    order = tuple(reversed(order))
                observed = {
                    name: _query(session, base_url, database, query)
                    for name, query in order
                }
                print(
                    f"database={database} iteration={iteration + 1} "
                    f"A={observed['A']} B={observed['B']}"
                )
    finally:
        if args.cleanup:
            for database in databases:
                _server_command(session, base_url, f"drop database {database}")
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
