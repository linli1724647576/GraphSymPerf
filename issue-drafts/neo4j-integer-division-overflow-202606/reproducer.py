from __future__ import annotations

import json
import os

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD")
MIN_INTEGER = -(2**63)

if not PASSWORD:
    raise SystemExit("set NEO4J_PASSWORD before running this reproducer")


def execute(session, query: str, **parameters):
    try:
        record = session.run(query, **parameters).single(strict=True)
        return {"outcome": "value", "value": record["value"]}
    except Neo4jError as exc:
        return {
            "outcome": "error",
            "code": exc.code,
            "message": str(exc),
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

        for iteration in range(6):
            languages = ("5", "25") if iteration % 2 == 0 else ("25", "5")
            for language in languages:
                result = execute(
                    session,
                    f"CYPHER {language} RETURN $left / $right AS value",
                    left=MIN_INTEGER,
                    right=-1,
                )
                observation = {
                    "iteration": iteration + 1,
                    "language": language,
                    **result,
                }
                observations.append(observation)
                print(json.dumps(observation, sort_keys=True))

        controls = {
            "unary_negation": execute(
                session,
                "RETURN -($value) AS value",
                value=MIN_INTEGER,
            ),
            "addition": execute(
                session,
                "RETURN $value + 1 AS value",
                value=2**63 - 1,
            ),
            "multiplication": execute(
                session,
                "RETURN $left * $right AS value",
                left=3_037_000_500,
                right=3_037_000_500,
            ),
            "ordinary_division": execute(
                session,
                "RETURN $left / $right AS value",
                left=42,
                right=-1,
            ),
        }
        print(json.dumps({"controls": controls}, sort_keys=True))
finally:
    driver.close()

assert all(item["outcome"] == "error" for item in observations), (
    "expected integer-overflow errors, but Neo4j returned a wrapped value: "
    f"{observations[0]}"
)
