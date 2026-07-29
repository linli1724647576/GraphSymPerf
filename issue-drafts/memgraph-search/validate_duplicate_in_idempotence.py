#!/usr/bin/env python3
"""Validate duplicate IN-list element idempotence on Memgraph.

The script is intentionally small and red-capable: it fails with exit status 1
when duplicating the same list element changes the returned rows.  It also runs
the same queries after dropping the property index as a negative control.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, Query


def run(
    session: Any,
    text: str,
    parameters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result = session.run(Query(text, timeout=30), parameters or {})
    rows = [record.data() for record in result]
    result.consume()
    return rows


def best_effort(session: Any, statement: str) -> None:
    try:
        run(session, statement)
    except Exception:
        pass


def query_rows(session: Any, values: str) -> list[dict[str, Any]]:
    return run(
        session,
        f"MATCH (n:Item) WHERE n.key IN [{values}] RETURN n.id AS id ORDER BY id",
    )


def query_count(session: Any, values: str) -> int:
    rows = run(
        session,
        f"MATCH (n:Item) WHERE n.key IN [{values}] RETURN count(*) AS c",
    )
    return int(rows[0]["c"])


def observations(session: Any) -> dict[str, Any]:
    lists = ("42", "42, 42", "42, 42, 42", "7, 42, 42")
    result: dict[str, Any] = {}
    for values in lists:
        result[values] = {
            "rows": query_rows(session, values),
            "count": query_count(session, values),
            "plan": run(
                session,
                f"EXPLAIN MATCH (n:Item) WHERE n.key IN [{values}] RETURN n.id AS id",
            ),
        }
    result["parameter_single"] = run(
        session,
        "MATCH (n:Item) WHERE n.key IN $values RETURN n.id AS id ORDER BY id",
        {"values": [42]},
    )
    result["parameter_duplicate"] = run(
        session,
        "MATCH (n:Item) WHERE n.key IN $values RETURN n.id AS id ORDER BY id",
        {"values": [42, 42]},
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://127.0.0.1:7688")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=None)
    try:
        driver.verify_connectivity()
        info = driver.get_server_info()
        with driver.session() as session:
            version = run(session, "SHOW VERSION")
            run(session, "MATCH (n) DETACH DELETE n")
            best_effort(session, "DROP INDEX ON :Item(key)")
            run(session, "CREATE INDEX ON :Item(key)")
            run(session, "CREATE (:Item {id: 1, key: 42}), (:Item {id: 2, key: 7}), (:Item {id: 3})")
            indexed = observations(session)
            run(session, "DROP INDEX ON :Item(key)")
            without_index = observations(session)

        expected = [{"id": 1}]
        indexed_single = indexed["42"]["rows"]
        indexed_duplicate = indexed["42, 42"]["rows"]
        no_index_single = without_index["42"]["rows"]
        no_index_duplicate = without_index["42, 42"]["rows"]
        payload = {
            "schema_version": 1,
            "purpose": "fresh-process logical validation",
            "server_agent": getattr(info, "agent", None),
            "version": version,
            "graph": {"item_nodes": 3, "indexed_key_42_nodes": 1},
            "expected_rows": expected,
            "indexed": indexed,
            "without_index": without_index,
            "indexed_single_matches_expected": indexed_single == expected,
            "indexed_duplicate_matches_expected": indexed_duplicate == expected,
            "indexed_single_equals_duplicate": indexed_single == indexed_duplicate,
            "without_index_single_equals_duplicate": no_index_single == no_index_duplicate,
            "parameter_single_equals_duplicate": (
                indexed["parameter_single"] == indexed["parameter_duplicate"]
            ),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(
            "indexed rows single/duplicate/triple="
            f"{len(indexed['42']['rows'])}/"
            f"{len(indexed['42, 42']['rows'])}/"
            f"{len(indexed['42, 42, 42']['rows'])}; "
            "without-index single/duplicate="
            f"{len(without_index['42']['rows'])}/"
            f"{len(without_index['42, 42']['rows'])}"
        )
        print(f"wrote {args.output}")
        return 0 if payload["indexed_single_equals_duplicate"] else 1
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
