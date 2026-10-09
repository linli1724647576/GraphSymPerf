#!/usr/bin/env python3
"""Run the attached native setup and A/B queries on an empty disposable database.

The recorded repeat measurements are in result.json; this script is a portable
input reproducer, not a replacement for those AB/BA confirmation measurements.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import time

HERE = Path(__file__).resolve().parent

def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))

def query(name):
    return (HERE / name).read_text(encoding="utf-8").strip().rstrip(";")

def typed(value):
    if value is None:
        return ("null",)
    if type(value) in (bool, int, str):
        return (type(value).__name__, value)
    if type(value) is float and math.isfinite(value):
        return ("float", value)
    if type(value) in (list, tuple):
        return ("list", tuple(typed(item) for item in value))
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return ("map", tuple(sorted((key, typed(item)) for key, item in value.items())))
    raise ValueError("unsupported complete-result value type: " + type(value).__name__)

def bag(rows):
    return Counter(typed(row) for row in rows)

def main():
    evidence = load("evidence.json")
    dbms = evidence["dbms"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("setup", "queries", "profile", "counts"))
    parser.add_argument("--uri", default=os.environ.get("GSP_URI", "redis://127.0.0.1:6379" if dbms == "falkordb" else "bolt://127.0.0.1:7687"))
    parser.add_argument("--database", default=os.environ.get("GSP_DATABASE", "gsp_reproducer" if dbms == "falkordb" else dbms))
    parser.add_argument("--user", default=os.environ.get("GSP_USER", "neo4j" if dbms == "neo4j" else ""))
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    password = os.environ.get("GSP_PASSWORD")
    parameters = load("parameters.json")
    expected = load("expected-results.json")
    measured_ms = {}
    if dbms == "falkordb":
        from falkordb import FalkorDB
        client = FalkorDB.from_url(args.uri, password=password, socket_timeout=args.timeout + 5)
        if int(client.config_get("RESULTSET_SIZE")) != -1:
            client.close()
            raise RuntimeError("this complete-result reproducer requires RESULTSET_SIZE=-1; it does not change shared server settings")
        graph = client.select_graph(args.database)
        def run(text, params=None):
            started = time.perf_counter()
            response = graph.query(text, params=params or None, timeout=max(1, int(args.timeout * 1000)))
            names = [column[1] for column in response.header]
            if len(set(names)) != len(names) or any(len(row) != len(names) for row in response.result_set):
                raise ValueError("ambiguous or incomplete result columns")
            rows = [dict(zip(names, row)) for row in response.result_set]
            measured_ms["last"] = (time.perf_counter() - started) * 1000
            return rows
        def profile(text):
            print("\n".join(graph.profile(text, params=parameters or None).plan))
        close = client.close
    else:
        from neo4j import GraphDatabase, Query
        client = GraphDatabase.driver(args.uri, auth=(args.user, password) if args.user else None)
        client.verify_connectivity()
        def run(text, params=None):
            with client.session(database=args.database) as session:
                started = time.perf_counter()
                cursor = session.run(Query(text, timeout=args.timeout), params or {})
                rows = [dict(record) for record in cursor]
                cursor.consume()
                measured_ms["last"] = (time.perf_counter() - started) * 1000
                return rows
        def profile(text):
            with client.session(database=args.database) as session:
                cursor = session.run(Query("PROFILE " + text, timeout=args.timeout), parameters)
                rows = [dict(record) for record in cursor]
                summary = cursor.consume()
                if dbms == "memgraph":
                    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
                else:
                    plan = summary.profile or {}
                    original = plan.get("args", {}).get("string-representation")
                    print(original if isinstance(original, str) else json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        close = client.close
    try:
        if args.mode == "setup":
            existing = run("MATCH (n) RETURN count(n) AS nodes")
            if not existing or existing[0].get("nodes") != 0:
                raise RuntimeError("setup requires an empty disposable database/graph")
            for statement in load("setup-statements.json"):
                run(statement)
            if dbms == "neo4j":
                run("CALL db.awaitIndexes($seconds)", {"seconds": max(1, int(args.timeout))})
                run("CALL db.prepareForReplanning()")
            if dbms == "falkordb" and evidence.get("index_spec"):
                label, prop = evidence["index_spec"]
                deadline = time.monotonic() + args.timeout
                while True:
                    rows = run("CALL db.indexes() YIELD label, properties, status RETURN label, properties, status")
                    if any(row.get("label") == label and row.get("properties") == [prop] and row.get("status") == "OPERATIONAL" for row in rows):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("recorded cost index did not become operational")
                    time.sleep(0.05)
            print("Native setup completed; indexes ready.")
        elif args.mode == "counts":
            for text in ("MATCH (n) RETURN count(n) AS nodes", "MATCH ()-[r]->() RETURN count(r) AS relationships"):
                print(json.dumps(run(text), ensure_ascii=False))
        elif args.mode == "queries":
            run("RETURN 1 AS connected")
            observed = []
            for name in ("base.cypher", "transformed.cypher"):
                rows = run(query(name), parameters)
                elapsed = measured_ms["last"]
                actual = bag(rows)
                if expected is not None and actual != bag(expected):
                    raise AssertionError(name + " differs from the complete constructed expectation")
                observed.append(actual)
                print(json.dumps({"query": name, "client_elapsed_ms": elapsed, "rows": rows,
                                  "matches_constructed_expectation": expected is not None}, ensure_ascii=False, indent=2))
            if observed[0] != observed[1]:
                raise AssertionError("the complete typed A/B result multisets differ")
            print("Complete typed A/B result multisets match; bool and int remain distinct.")
        else:
            for name in ("base.cypher", "transformed.cypher"):
                print(name)
                profile(query(name))
    finally:
        close()

if __name__ == "__main__":
    main()
