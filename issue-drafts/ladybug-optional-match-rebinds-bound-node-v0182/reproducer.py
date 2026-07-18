from __future__ import annotations

import sys
from pathlib import Path

import ladybug


DATABASE = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else "ladybug-optional-match-bound-node-reproducer"
)
QUERY_A = """
MATCH (n0:L1), (n3)
OPTIONAL MATCH (n6)-[:T1]->(n0)
WHERE n0.k11 = 1
RETURN count(*) AS c
"""
QUERY_B = """
MATCH (n0:L1), (n3)
OPTIONAL MATCH (n6)-[:T1]->(n0), (n3)
WHERE n0.k11 = 1
RETURN count(*) AS c
"""


def execute(connection: ladybug.Connection, query: str):
    return connection.execute(query).get_as_df().to_dict(orient="records")


if DATABASE.exists():
    DATABASE.unlink()

database = ladybug.Database(str(DATABASE))
connection = ladybug.Connection(database)
connection.execute("CREATE NODE TABLE L1(id INT64, k11 INT64, PRIMARY KEY(id))")
connection.execute("CREATE NODE TABLE X(id INT64, PRIMARY KEY(id))")
connection.execute("CREATE REL TABLE T1(FROM X TO L1)")
connection.execute("CREATE (:L1 {id:1, k11:1})")
connection.execute("CREATE (:X {id:2})")
connection.execute("MATCH (a:X), (b:L1) CREATE (a)-[:T1]->(b)")

for iteration in range(6):
    order = (("A", QUERY_A), ("B", QUERY_B))
    if iteration % 2:
        order = tuple(reversed(order))
    for name, query in order:
        print(f"iteration {iteration + 1}, query {name}: {execute(connection, query)}")
