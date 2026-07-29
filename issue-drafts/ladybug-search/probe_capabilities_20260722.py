#!/usr/bin/env python3
"""Probe LadybugDB 0.18.3 Cypher features used by rule-family screening."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import ladybug


PROBES = {
    "inline-property": "MATCH (n:N {id: 1}) RETURN count(*) AS value",
    "where-property": "MATCH (n:N) WHERE n.id = 1 RETURN count(*) AS value",
    "in-list": "MATCH (n:N) WHERE n.grp IN [1, 2] RETURN count(*) AS value",
    "or": "MATCH (n:N) WHERE n.grp = 1 OR n.grp = 2 RETURN count(*) AS value",
    "with": "MATCH (n:N) WITH n RETURN count(*) AS value",
    "distinct": "MATCH (n:N) RETURN count(DISTINCT n.grp) AS value",
    "union-all": "RETURN 1 AS value UNION ALL RETURN 1 AS value",
    "optional": (
        "MATCH (n:N) OPTIONAL MATCH (n)-[:MISSING]->(m) "
        "RETURN count(*) AS value"
    ),
    "case": (
        "MATCH (n:N) RETURN sum(CASE WHEN n.id > 0 THEN 1 ELSE 0 END) AS value"
    ),
    "coalesce": "MATCH (n:N) RETURN sum(coalesce(n.nullable, 1)) AS value",
    "min": "MATCH (n:N) RETURN min(n.id) AS value",
    "order-limit": "MATCH (n:N) RETURN n.id AS value ORDER BY value LIMIT 1",
    "typed-relationship": "MATCH (:A)-[r:R]->(:B) RETURN count(r) AS value",
    "type-function": "MATCH (:A)-[r:R]->(:B) RETURN type(r) AS value LIMIT 1",
    "path-length": (
        "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN length(p) AS value LIMIT 1"
    ),
    "path-nodes": (
        "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN nodes(p) AS value LIMIT 1"
    ),
    "path-rels": (
        "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) RETURN rels(p) AS value LIMIT 1"
    ),
    "path-size-nodes": (
        "MATCH p=(:X)-[:P]->(:Y)-[:Q]->(:Z) "
        "RETURN size(nodes(p)) AS value LIMIT 1"
    ),
    "head-list": "MATCH (n:N) RETURN head([n.id]) AS value LIMIT 1",
    "last-list": "MATCH (n:N) RETURN last([n.id]) AS value LIMIT 1",
    "list-subscript": "MATCH (n:N) RETURN [n.id][0] AS value LIMIT 1",
    "labels-function": "MATCH (n:N) RETURN labels(n) AS value LIMIT 1",
    "label-function": "MATCH (n:N) RETURN label(n) AS value LIMIT 1",
    "id-function": "MATCH (n:N) RETURN id(n) AS value LIMIT 1",
    "start-node": "MATCH (a:A)-[r:R]->(:B) RETURN startNode(r) = a AS value LIMIT 1",
    "end-node": "MATCH (:A)-[r:R]->(b:B) RETURN endNode(r) = b AS value LIMIT 1",
    "exists-property": "MATCH (n:N) RETURN n.id IS NOT NULL AS value LIMIT 1",
    "string-functions": (
        "MATCH (n:N) RETURN lower(upper(n.txt)) AS value LIMIT 1"
    ),
    "starts-with": "MATCH (n:N) WHERE n.txt STARTS WITH 'v' RETURN count(*) AS value",
    "contains": "MATCH (n:N) WHERE n.txt CONTAINS '1' RETURN count(*) AS value",
    "list-comprehension": (
        "MATCH (n:N) RETURN [x IN [n.id] | x + 1][0] AS value LIMIT 1"
    ),
    "range": "UNWIND range(1, 3) AS x RETURN sum(x) AS value",
    "arithmetic": "MATCH (n:N) RETURN sum(n.id + 0) AS value",
    "count-star": "MATCH (n:N) RETURN count(*) AS value",
    "sum-one": "MATCH (n:N) RETURN sum(1) AS value",
    "profile": "PROFILE MATCH (n:N) RETURN count(*) AS value",
}


def rows(result: Any) -> list[list[Any]]:
    return result.get_all()


def setup(connection: Any) -> None:
    connection.execute(
        "CREATE NODE TABLE N(id INT64, score INT64, grp INT64, txt STRING, "
        "nullable INT64, PRIMARY KEY(id))"
    )
    connection.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE X(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE Y(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE NODE TABLE Z(id INT64, PRIMARY KEY(id))")
    connection.execute("CREATE REL TABLE R(FROM A TO B, id INT64)")
    connection.execute("CREATE REL TABLE P(FROM X TO Y, id INT64)")
    connection.execute("CREATE REL TABLE Q(FROM Y TO Z, id INT64)")
    connection.execute(
        "UNWIND range(1, 100) AS i "
        "CREATE (:N {id:i, score:i, grp:i % 7, txt:'v' + cast(i, 'STRING'), "
        "nullable: CASE WHEN i % 2 = 0 THEN NULL ELSE i END})"
    )
    connection.execute(
        "UNWIND range(1, 10) AS i "
        "CREATE (:A {id:i})-[:R {id:i}]->(:B {id:i})"
    )
    connection.execute(
        "UNWIND range(1, 10) AS i "
        "CREATE (:X {id:i})-[:P {id:i}]->(:Y {id:i})-[:Q {id:i}]->(:Z {id:i})"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database.exists():
        args.database.unlink()
    database = ladybug.Database(str(args.database))
    connection = ladybug.Connection(database)
    setup(connection)
    records: list[dict[str, Any]] = []
    for probe_id, query in PROBES.items():
        record: dict[str, Any] = {"probe_id": probe_id, "query": query}
        try:
            record.update(status="supported", rows=rows(connection.execute(query)))
        except Exception as exc:
            record.update(status="unsupported", error=str(exc))
        records.append(record)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, default=str) for record in records)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "version": ladybug.__version__,
                "supported": sum(record["status"] == "supported" for record in records),
                "unsupported": [
                    record["probe_id"]
                    for record in records
                    if record["status"] == "unsupported"
                ],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
