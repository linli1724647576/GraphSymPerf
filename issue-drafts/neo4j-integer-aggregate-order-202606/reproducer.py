from __future__ import annotations

import json
import os

from neo4j import GraphDatabase


URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD")
MAX_INTEGER = 2**63 - 1
VALUES = [MAX_INTEGER, 1, -MAX_INTEGER]
CONTROL_VALUES = [7, 1, -7]
QUERY_TEMPLATE = """
UNWIND $values AS x
WITH x ORDER BY x {direction}
RETURN sum(x) AS sum_value,
       valueType(sum(x)) AS sum_type
"""

if not PASSWORD:
    raise SystemExit("set NEO4J_PASSWORD before running this reproducer")


def execute(session, language: str, values: list[int], direction: str):
    query = QUERY_TEMPLATE.format(direction=direction)
    return session.run(f"CYPHER {language} {query}", values=values).single(strict=True).data()


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
            cases = (("ASC", "ASC"), ("DESC", "DESC"))
            if iteration % 2:
                cases = tuple(reversed(cases))
            for language in ("5", "25"):
                for name, direction in cases:
                    observation = {
                        "iteration": iteration + 1,
                        "language": language,
                        "case": name,
                        "values": VALUES,
                        **execute(session, language, VALUES, direction),
                    }
                    observations.append(observation)
                    print(json.dumps(observation, sort_keys=True))

        controls = {
            language: {
                "ASC": execute(session, language, CONTROL_VALUES, "ASC"),
                "DESC": execute(session, language, CONTROL_VALUES, "DESC"),
            }
            for language in ("5", "25")
        }
        print(json.dumps({"controls": controls}, sort_keys=True))
finally:
    driver.close()

assert all(item["sum_value"] == 1 for item in observations), (
    "sum() should be 1 for both permutations, but observed: "
    f"{observations[0]} and {observations[1]}"
)
