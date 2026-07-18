from __future__ import annotations

import sys
from pathlib import Path

import ladybug


DATABASE = Path(sys.argv[1] if len(sys.argv) > 1 else "ladybug-label-order-reproducer")
QUERY_A = """
MATCH (n0:L3)<-[:T5]-(n1:L2), (n1)
RETURN count(*) AS c
"""
QUERY_B = """
MATCH (n1), (n0:L3)<-[:T5]-(n1:L2)
RETURN count(*) AS c
"""


def execute(connection: ladybug.Connection, query: str):
    return connection.execute(query).get_as_df().to_dict(orient="records")


if DATABASE.exists():
    DATABASE.unlink()

database = ladybug.Database(str(DATABASE))
connection = ladybug.Connection(database)
connection.execute("CREATE NODE TABLE L1(id INT64, PRIMARY KEY(id))")
connection.execute("CREATE NODE TABLE L2(id INT64, PRIMARY KEY(id))")
connection.execute("CREATE NODE TABLE L3(id INT64, PRIMARY KEY(id))")
connection.execute("CREATE REL TABLE T5(FROM L1 TO L3)")
connection.execute("CREATE (:L1 {id:1})")
connection.execute("CREATE (:L3 {id:2})")
connection.execute(
    "MATCH (a:L1), (b:L3) WHERE a.id=1 AND b.id=2 "
    "CREATE (a)-[:T5]->(b)"
)

for iteration in range(6):
    order = (("A", QUERY_A), ("B", QUERY_B))
    if iteration % 2:
        order = tuple(reversed(order))
    for name, query in order:
        print(f"iteration {iteration + 1}, query {name}: {execute(connection, query)}")
