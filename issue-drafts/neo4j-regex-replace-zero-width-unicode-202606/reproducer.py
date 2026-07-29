from __future__ import annotations

import json
import os

from neo4j import GraphDatabase


URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD")


QUERY = (
    "CYPHER 25 "
    "RETURN string.regexReplace($input, '', '-') AS actual, "
    "'-' || $input || '-' AS expected, "
    "size(string.regexReplace($input, '', '-')) AS actual_size, "
    "size($input) + 2 AS expected_size"
)

if not PASSWORD:
    raise SystemExit("set NEO4J_PASSWORD before running this reproducer")

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
            inputs = ("🙂", "🙂a") if iteration % 2 == 0 else ("🙂a", "🙂")
            for input_value in inputs:
                record = session.run(QUERY, input=input_value).single(strict=True)
                observation = {
                    "iteration": iteration + 1,
                    "input": input_value,
                    **record.data(),
                }
                observations.append(observation)
                print(json.dumps(observation, sort_keys=True, ensure_ascii=False))

        controls = {}
        for name, input_value in (("ascii", "a"), ("bmp", "汉")):
            controls[name] = session.run(QUERY, input=input_value).single(strict=True).data()
        controls["capturing_group_identity"] = session.run(
            "CYPHER 25 RETURN string.regexReplace($input, '(.)', '$1') AS value",
            input="🙂",
        ).single(strict=True)["value"]
        print(json.dumps({"controls": controls}, sort_keys=True, ensure_ascii=False))
finally:
    driver.close()

assert all(
    item["actual"] == item["expected"]
    and item["actual_size"] == item["expected_size"]
    for item in observations
), (
    "expected zero-width replacement to preserve Unicode code points, "
    f"but Neo4j corrupted the supplementary character: {observations[0]}"
)
