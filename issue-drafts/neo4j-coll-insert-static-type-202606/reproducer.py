from __future__ import annotations

import json
import os

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not PASSWORD:
    raise SystemExit("set NEO4J_PASSWORD before running this reproducer")


def execute(session, query: str):
    try:
        record = session.run(f"CYPHER 25 {query}").single(strict=True)
        return {"outcome": "value", "value": record["value"]}
    except Neo4jError as exc:
        return {
            "outcome": "error",
            "code": exc.code,
            "message": str(exc),
        }


QUERIES = {
    "size": "RETURN size(coll.insert([1, 2, 3], 1, 9)) AS value",
    "head": "RETURN head(coll.insert([1, 2, 3], 1, 9)) AS value",
    "remove_inserted": (
        "RETURN coll.remove(coll.insert([1, 2, 3], 1, 9), 1) AS value"
    ),
}

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
driver.verify_connectivity()
observations = []
try:
    with driver.session(database="neo4j") as session:
        component = session.run(
            "CALL dbms.components() YIELD name, versions, edition "
            "WHERE name = 'Neo4j Kernel' "
            "RETURN versions[0] AS version, edition LIMIT 1"
        ).single(strict=True)
        print(json.dumps({"environment": component.data()}, sort_keys=True))

        direct = session.run(
            "CYPHER 25 "
            "RETURN coll.insert([1, 2, 3], 1, 9) AS value, "
            "valueType(coll.insert([1, 2, 3], 1, 9)) AS value_type"
        ).single(strict=True)
        print(json.dumps({"direct_control": direct.data()}, sort_keys=True))

        names = tuple(QUERIES)
        for iteration in range(6):
            order = names if iteration % 2 == 0 else tuple(reversed(names))
            for name in order:
                observation = {
                    "iteration": iteration + 1,
                    "query": name,
                    **execute(session, QUERIES[name]),
                }
                observations.append(observation)
                print(json.dumps(observation, sort_keys=True))

        controls = {
            "size_after_remove": execute(
                session,
                "RETURN size(coll.remove([1, 2, 3], 1)) AS value",
            ),
            "remove_after_flatten": execute(
                session,
                "RETURN coll.remove(coll.flatten([[1], [2, 3]]), 0) AS value",
            ),
            "remove_then_insert": execute(
                session,
                "RETURN coll.insert(coll.remove([1, 2, 3], 1), 1, 2) AS value",
            ),
        }
        print(json.dumps({"controls": controls}, sort_keys=True))
finally:
    driver.close()

expected = {"size": 4, "head": 1, "remove_inserted": [1, 2, 3]}
assert all(
    item["outcome"] == "value" and item["value"] == expected[item["query"]]
    for item in observations
), (
    "expected documented LIST consumers to accept coll.insert() output, "
    f"but Neo4j rejected it: {observations[0]}"
)
