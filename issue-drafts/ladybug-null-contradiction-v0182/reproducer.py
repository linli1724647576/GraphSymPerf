from __future__ import annotations

import random
import statistics
import time
from pathlib import Path

import ladybug


DATABASE = Path("ladybug-null-contradiction-reproducer")
QUERY_A = """
MATCH (n:N)
WHERE n.v IS NULL AND n.v IS NOT NULL
RETURN count(*) AS c
"""
QUERY_B = """
MATCH (n:N)
WHERE false
RETURN count(*) AS c
"""


def execute(connection: ladybug.Connection, query: str):
    started = time.perf_counter()
    rows = connection.execute(query).get_as_df().to_dict(orient="records")
    return (time.perf_counter() - started) * 1000.0, rows


if DATABASE.exists():
    DATABASE.unlink()

database = ladybug.Database(str(DATABASE))
connection = ladybug.Connection(database)
connection.execute("CREATE NODE TABLE N(id INT64, v INT64, PRIMARY KEY(id))")
connection.execute(
    "UNWIND RANGE(1, 5000000) AS i "
    "CREATE (:N {id:i, v: CASE WHEN i % 2 = 0 THEN NULL ELSE i END})"
)

for _ in range(5):
    execute(connection, QUERY_A)
    execute(connection, QUERY_B)

samples = {"a": [], "b": []}
results = {}
schedule = ["AB"] * 10 + ["BA"] * 10
random.Random(20260718).shuffle(schedule)
for order in schedule:
    for side in order.lower():
        elapsed_ms, rows = execute(
            connection,
            QUERY_A if side == "a" else QUERY_B,
        )
        samples[side].append(elapsed_ms)
        results[side] = rows

median_a = statistics.median(samples["a"])
median_b = statistics.median(samples["b"])
print(f"query A result: {results['a']}")
print(f"query B result: {results['b']}")
print(f"query A median: {median_a:.6f} ms")
print(f"query B median: {median_b:.6f} ms")
print(f"ratio A/B: {median_a / median_b:.6f}x")
print(f"delta A-B: {median_a - median_b:.6f} ms")
