"""Small real-database checks using the backend selected in config.yaml."""
from collections import Counter
import json
from pathlib import Path
from time import perf_counter
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from databases import select_database
from data_structures.case import Case
from data_structures.graph import Graph
from data_structures.query import Query
from data_structures.result import freeze
from executor import run_pair, save_case
from main import load_config
from replay import replay_case


def fixture():
    graph = Graph(schema={
        "nodes": {
            "Root": {"uid": "int", "gate_true": "bool", "gate_false": "bool", "prefix": "bool"},
            "Item": {"uid": "int", "keep": "bool", "text": "string", "score": "float"},
            "Witness": {"uid": "int"}, "Empty": {"uid": "int"},
        },
        "relationships": {},
    })
    for _ in range(2):
        graph.node(["Root"], gate_true=True, gate_false=False, prefix=False)
    for i in range(6):
        graph.node(["Item"], keep=i >= 3, text="" if i == 0 else "x", score=None)
    graph.node(["Witness"])
    edges = {"E_LOW": [(0, 2)], "E_HIGH": [(0, 3), (0, 4)], "D_TRUE": [(0, 8)],
             "L_LOW": [(0, 5), (0, 6)], "L_HIGH": [(0, 5), (0, 6), (0, 7)],
             "PARALLEL": [(0, 5), (0, 5)], "LINK": [(0, 8), (8, 7)]}
    for name, pairs in edges.items():
        endpoints = sorted({(graph.nodes[a]["labels"][0], graph.nodes[b]["labels"][0]) for a, b in pairs})
        graph.schema["relationships"][name] = {"endpoints": endpoints, "properties": {}}
        for a, b in pairs:
            graph.edge(a, b, name)
    graph.index("Root", "uid")
    return graph


def check_ladybug_casts(db):
    checks = {}
    for name, expression, expected in [
        ("cast_string", 'CAST(2, "STRING")', "2"),
        ("cast_double", 'CAST(2, "DOUBLE")', 2.0),
        ("cast_count_string", 'CAST(COUNT { MATCH (n:Root) }, "STRING")', "2"),
        ("cast_empty_count_double", 'CAST(COUNT { MATCH (n:Empty) }, "DOUBLE")', 0.0),
    ]:
        query = "RETURN " + expression + " AS value"
        columns, rows, elapsed = db.execute(Query(query), 10)
        assert columns == ["value"] and rows == [(freeze(expected),)], (name, rows)
        checks[name] = {"status": "passed", "query": query, "rows": rows, "elapsed_ms": elapsed}
    return checks


def check(config, output):
    if config["language"] == "gremlin":
        return check_gremlin(config, output)
    if config["database"]["system"] not in {"ladybug", "falkordb", "memgraph", "arcadedb"}:
        raise ValueError("Interface checks require database.system: ladybug, falkordb, memgraph, or arcadedb")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    graph = fixture()
    db = select_database(config["database"]["system"]).Database(config["database"])
    report = {"system": config["database"]["system"], "checks": {}}
    checks = report["checks"]
    try:
        if config["database"]["system"] in {"memgraph", "arcadedb"}:
            graph.nodes[2]["labels"].append("Extra")
            graph.schema["nodes"]["Extra"] = dict(graph.schema["nodes"]["Item"])
        if config["database"]["system"] == "memgraph":
            db.session.close()
            db.session = db.driver.session(database=config["database"]["name"], fetch_size=128)
        db.load(graph)
        report["version"] = db.version
        if config["database"]["system"] == "ladybug":
            checks.update(check_ladybug_casts(db))
        if config["database"]["system"] == "memgraph":
            assert db.execute(Query("MATCH (n:Item:Extra) RETURN n.uid AS uid"), 10)[1] == [(("int", 2),)]
            assert db.execute(Query("MATCH (n:__GSP) RETURN count(n)"), 10)[1] == [(("int", 0),)]
            indexes = db.session.run("SHOW INDEX INFO").data()
            assert [(r["index type"], r["label"], r["property"]) for r in indexes] == [("label+property", "Root", ["uid"])]
            checks["multi_label"] = checks["loading_index_removed"] = "passed"
        for name, text, columns, expected in [
            ("node_count", "MATCH (n) RETURN count(n) AS nodes", ("nodes",), [(("int", 9),)]),
            ("edge_count", "MATCH ()-[r]->() RETURN count(r) AS relationships", ("relationships",), [(("int", 13),)]),
            ("empty_table", "MATCH (n:Empty) RETURN n.uid AS uid", ("uid",), []),
            ("null_and_empty_string", "MATCH (n:Item {uid:2}) RETURN n.text AS text, n.score AS score", ("text", "score"), [(("string", ""), ("null",))]),
        ]:
            columns, rows, elapsed = db.execute(Query(text, columns=columns), 10)
            assert rows == expected, (name, rows)
            if name == "empty_table":
                assert columns == ["uid"]
            checks[name] = "passed"
        query = Query("RETURN $b AS b, $i AS i, $f AS f, $s AS s, $xs AS xs, NULL AS missing",
                      {"b": True, "i": 1, "f": 1.0, "s": "", "xs": [1, 2, 1]}, columns=("b", "i", "f", "s", "xs", "missing"))
        columns, rows, _ = db.execute(query, 10)
        assert columns == ["b", "i", "f", "s", "xs", "missing"]
        assert rows == [(("bool", True), ("int", 1), ("float", (1.0).hex()), ("string", ""),
                         ("list", (("int", 1), ("int", 2), ("int", 1))), ("null",))]
        checks["parameters"] = "passed"
        _, rows, _ = db.execute(Query("MATCH (a:Root {uid:0}), (b:Item {uid:2}) RETURN a, b, [a] AS ns, {entity:a} AS m", columns=("a", "b", "ns", "m")), 10)
        a, b, ns, mapping = rows[0]
        assert a[0] == b[0] == "node" and a != b
        assert ns == ("list", (a,)) and mapping == ("map", (("entity", a),))
        _, rows, _ = db.execute(Query("MATCH (:Root {uid:0})-[r:PARALLEL]->(:Item {uid:5}) RETURN r", columns=("r",)), 10)
        assert len(rows) == len(set(rows)) == 2 and all(row[0][0] == "relationship" for row in rows)
        checks["entity_identity"] = "passed"
        for name, a, b in [
            ("bool_int", "RETURN true AS value", "RETURN 1 AS value"),
            ("null_empty", "RETURN null AS value", "RETURN '' AS value"),
            ("duplicates", "UNWIND [1,1] AS x RETURN x AS value", "UNWIND [1] AS x RETURN x AS value"),
            ("list_order", "RETURN [1,2] AS value", "RETURN [2,1] AS value"),
            ("path_order", "MATCH p=(:Root {uid:0})-[:LINK*2..2]->(:Item {uid:7}) RETURN p",
             "MATCH p=(:Item {uid:7})<-[:LINK*2..2]-(:Root {uid:0}) RETURN p"),
        ]:
            result = run_pair(db, Query(a, columns=("p",) if name == "path_order" else ("value",)),
                              Query(b, columns=("p",) if name == "path_order" else ("value",)), config["measurement"], check_only=True)
            assert result["status"] == "result_mismatch", (name, result)
            checks[name] = "passed"
        _, rows, _ = db.execute(Query("MATCH p=(:Root {uid:0})-[:LINK*2..2]->(:Item {uid:7}) RETURN p, length(p) AS length", columns=("p", "length")), 10)
        assert len(rows) == 1 and rows[0][0][0] == "path" and rows[0][1] == ("int", 2)
        checks["multi_endpoint_path"] = "passed"

        # Native snippets exercise the adapter's result and execution interfaces.
        query = Query("MATCH (u:Root {uid:0}) RETURN EXISTS {MATCH (u)-[:E_LOW]->(x:Item) WHERE x.keep=true} AS low, "
                      "EXISTS {MATCH (u)-[:E_HIGH]->(x:Item) WHERE x.keep=true} AS high, "
                      "EXISTS {MATCH (u)-[:D_TRUE]->(:Witness)} AS decisive", columns=("low", "high", "decisive"))
        try:
            _, rows, _ = db.execute(query, 10)
            assert rows == [(("bool", False), ("bool", False), ("bool", True))]
            checks["exists_syntax"] = "passed"
        except Exception as error:
            checks["exists_syntax"] = {"status": "failed", "message": str(error)}
        for name, prefix, expected in [
            ("limit", "MATCH (u:Root {uid:0})", [(("int", 1),)] * 2),
            ("limit_duplicate_prefix", "UNWIND [0,0] AS id MATCH (u:Root {uid:id})", [(("int", 1),)] * 2),
            ("limit_empty_prefix", "MATCH (u:Root) WHERE u.uid=-1", []),
        ]:
            for region in ["L_LOW", "L_HIGH"]:
                _, rows, _ = db.execute(Query(prefix + f" MATCH (u)-[:{region}]->(x:Item) RETURN 1 AS value LIMIT 2", columns=("value",)), 10)
                assert rows == expected, (name, rows)
            checks[name] = "passed"
        if config["database"]["system"] == "memgraph":
            for name, text, expected in [
                ("full_fetch", "UNWIND range(1,1005) AS value RETURN value", [(i,) for i in range(1,1006)]),
                ("correlated_count", "MATCH (u:Root) RETURN u.uid AS uid, COUNT {MATCH (u)-[:PARALLEL]->(x:Item) RETURN x.uid AS item} AS matches ORDER BY uid", [(0,2),(1,0)]),
                ("nested_count", "MATCH (u:Root) WITH u, 7 AS target RETURN u.uid AS uid, "
                 "COUNT {MATCH (u)-[:LINK]->(w:Witness) WHERE COUNT {MATCH (w)-[:LINK]->(x:Item) "
                 "WHERE x.uid=target RETURN 1 AS value}>0 RETURN 1 AS value} AS matches ORDER BY uid", [(0,1),(1,0)]),
                ("collect", "MATCH (u:Root) RETURN u.uid AS uid, COLLECT {MATCH (u)-[:L_HIGH]->(x:Item) RETURN x.uid AS item ORDER BY item} AS items ORDER BY uid", [(0,[5,6,7]),(1,[])]),
                ("call_union", "MATCH (u:Root {uid:0}) CALL (u) {MATCH (u)-[:L_LOW]->(x:Item) "
                 "RETURN x.uid AS item UNION ALL MATCH (u)-[:D_TRUE]->(w:Witness) "
                 "RETURN w.uid AS item} RETURN item ORDER BY item", [(5,),(6,),(8,)]),
                ("short_aggregate", "WITH range(1,10) AS xs UNWIND xs AS x UNWIND xs AS y RETURN sum(x*y) AS total", [(3025,)]),
            ]:
                _, actual, _ = db.execute(Query(text), 10)
                assert Counter(actual) == Counter(tuple(freeze(v) for v in row) for row in expected), (name, actual)
                checks[name] = "passed"
            try:
                db.execute(Query("MATCH (u:Root) CALL () {RETURN u.uid AS uid} RETURN uid"), 10)
            except db.error_type as error:
                assert "u" in error.message and "unbound" in error.message.lower(), error
                checks["call_scope_error"] = {"status": "passed", "code": error.code, "message": error.message}
            else:
                raise AssertionError("CALL () unexpectedly imported u")
        query = Query("MATCH (u:Root {uid:$uid})-[:L_HIGH]->(x:Item) RETURN x.uid AS uid", {"uid": 0}, columns=("uid",))
        assert Counter(db.execute(query, 10)[1]) == Counter([(("int", n),) for n in (5, 6, 7)])
        plan = db.profile(query, 10)
        assert plan["status"] == "ok" and (plan.get("rows") or plan.get("lines") or plan.get("plan"))
        if config["database"]["system"] == "memgraph":
            assert plan["columns"] == ["OPERATOR", "ACTUAL HITS", "RELATIVE TIME", "ABSOLUTE TIME"]
        (output / "profile.json").write_text(json.dumps(plan, indent=2))
        checks["profile"] = "passed"
        long_query = Query("WITH range(1,30000) AS xs UNWIND xs AS x UNWIND xs AS y RETURN sum(x*y) AS total", columns=("total",))
        for name, operation in [("timeout", db.execute), ("profile_timeout", db.profile)]:
            started = perf_counter()
            try:
                operation(long_query, 1)
                checks[name] = "inconclusive"
            except TimeoutError as error:
                checks[name] = {"status": "passed", "message": str(error),
                                "observed_wait_ms": (perf_counter() - started) * 1000}
            assert db.execute(Query("RETURN 1 AS value", columns=("value",)), 10)[1] == [(("int", 1),)]
        checks["after_timeout"] = "passed"

        if config["database"]["system"] == "arcadedb":
            check_arcadedb(db, checks)
        a = Query("MATCH (u:Root {uid:$uid})-[:L_LOW]->(x:Item) RETURN 1 AS value LIMIT 2", {"uid": 0}, columns=("value",))
        b = Query("MATCH (u:Root {uid:$uid})-[:L_HIGH]->(x:Item) RETURN 1 AS value LIMIT 2", {"uid": 0}, columns=("value",))
        save_case(output / "archive", Case("interface", 0, None, (), set()), graph, a, b,
                  {"status": "equivalent"}, config, db.version)
        small = Graph(schema={"nodes": {"Root": {"uid": "int"}}, "relationships": {}})
        small.node(["Root"])
        db.load(small)
        assert db.execute(Query("MATCH (n) RETURN count(n) AS nodes", columns=("nodes",)), 10)[1] == [(("int", 1),)]
        assert db.execute(Query("MATCH ()-[r]->() RETURN count(r) AS edges", columns=("edges",)), 10)[1] == [(("int", 0),)]
        checks["reload"] = "passed"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        db.close()
        (output / "report.json").write_text(json.dumps(report, indent=2))
    config["replay"]["check_only"] = True
    result = replay_case(output / "archive", config)
    assert result["status"] == "equivalent" and result["rows"] == 2
    checks["replay"] = "passed"
    (output / "report.json").write_text(json.dumps(report, indent=2))
    return report



def check_arcadedb(db, checks):
    from concurrent.futures import ThreadPoolExecutor
    from http.client import HTTPConnection
    from time import monotonic, sleep
    from uuid import uuid4

    indexes = db.command("sql", "SELECT name FROM schema:indexes")["result"]
    assert {row["name"] for row in indexes if "[" in row["name"]} == {"Root[uid]"}
    checks["loading_indexes_removed"] = "passed"
    for name, query, expected in [
        ("count_call", Query("RETURN COUNT {CALL () {RETURN 1 AS x} RETURN x} AS value", columns=("value",)), [(1,)]),
        ("collect_call", Query("RETURN COLLECT {CALL () {RETURN 1 AS x} RETURN x} AS value", columns=("value",)), [([1],)]),
        ("correlated_count", Query("MATCH (u:Root) RETURN u.uid AS uid, COUNT {MATCH (u)-[:PARALLEL]->(x) RETURN x.uid} AS matches ORDER BY uid", columns=("uid", "matches")), [(0,2),(1,0)]),
        ("declared_order", Query("RETURN 2 AS second, 1 AS first", columns=("second", "first")), [(2,1)]),
        ("large_integer", Query("RETURN 9007199254740993 AS value", columns=("value",)), [(9007199254740993,)]),
        ("multi_label", Query("MATCH (n:Item:Extra) RETURN n.uid AS uid", columns=("uid",)), [(2,)]),
        ("unlimited_rows", Query("UNWIND range(1,20005) AS value RETURN value", columns=("value",)), [(n,) for n in range(1,20006)]),
    ]:
        _, actual, _ = db.execute(query, 10)
        assert actual == [tuple(freeze(v) for v in row) for row in expected], name
        checks[name] = "passed"
    for name, text, columns in [
        ("wrong_field", "RETURN 1 AS actual", ("expected",)),
        ("missing_field", "RETURN 1 AS a", ("a", "b")),
        ("extra_field", "RETURN 1 AS a, 2 AS b", ("a",)),
    ]:
        try:
            db.execute(Query(text, columns=columns), 10)
        except ValueError as error:
            assert "fields differ" in str(error)
            checks[name] = "passed"
        else:
            raise AssertionError(name)
    _, rows, _ = db.execute(Query("MATCH (n:Item {uid:2}) RETURN n AS item", columns=("item",)), 10)
    assert len(rows) == 1 and rows[0][0][0] == "node"
    checks["flattened_entity"] = "passed"

    # Keep the query active in a worker while the control connection cancels it.
    tag = "gsp-cancel-" + uuid4().hex
    payload = {"language": "cypher", "command": "WITH range(1,30000) AS xs UNWIND xs AS x UNWIND xs AS y RETURN sum(x*y)", "limit": -1}
    control = HTTPConnection(*db.address, timeout=10)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(db.request, "POST", "/api/v1/query/" + db.name, payload, tag=tag)
            deadline = monotonic() + 3
            while monotonic() < deadline:
                listing, _ = db.request("POST", "/api/v1/server", {"command": "list queries"}, connection=control)
                if any(row.get("tag") == tag for row in listing["result"]):
                    break
                sleep(.05)
            else:
                raise AssertionError("Tagged query was not observed")
            db.cancel(tag)
            try:
                pending.result(timeout=10)
            except InterruptedError:
                pass
            else:
                raise AssertionError("Cancelled query did not report termination")
        listing, _ = db.request("POST", "/api/v1/server", {"command": "list queries"}, connection=control)
        assert not any(row.get("tag") == tag for row in listing["result"])
        checks["cancellation"] = {"status": "passed", "response": db.cancellation}
    finally:
        control.close()
    assert db.execute(Query("RETURN 1 AS value", columns=("value",)), 10)[1] == [(("int", 1),)]
    checks["after_cancellation"] = "passed"


def check_gremlin(config, output):
    if config["database"]["system"] == "janusgraph":
        return check_janusgraph(config, output)
    if config["database"]["system"] == "hugegraph":
        return check_hugegraph(config, output)
    from concurrent.futures import ThreadPoolExecutor
    from http.client import HTTPConnection
    from time import monotonic, sleep
    from uuid import uuid4

    if config["database"]["system"] != "arcadedb":
        raise ValueError("Gremlin interface checks require ArcadeDB")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    graph = fixture()
    db = select_database("arcadedb").Database(config["database"], language="gremlin")
    report = {"system": "arcadedb", "language": "gremlin", "version": db.version, "checks": {}}
    checks = report["checks"]
    try:
        db.load(graph)
        for name, text, params, expected in [
            ("node_count", "g.V().count()", {}, [9]),
            ("edge_count", "g.E().count()", {}, [13]),
            ("scalar_types", "g.inject(true,1,1.0,1.5,'',[1,2,1])", {}, [True,1,1.0,1.5,"",[1,2,1]]),
            ("bound_integer", "g.V().hasLabel('Root').has('uid',p0).values('uid')", {"p0": 0}, [0]),
            ("bound_bool", "g.V().hasLabel('Root').has('gate_true',p0).values('uid')", {"p0": True}, [0,1]),
            ("bound_string", "g.V().hasLabel('Item').has('text',p0).values('uid')", {"p0": ""}, [2]),
            ("bound_float", "g.inject(1.5).is(p0)", {"p0": 1.5}, [1.5]),
            ("empty", "g.V().hasLabel('Empty').values('uid')", {}, []),
            ("empty_count", "g.V().hasLabel('Empty').count()", {}, [0]),
            ("bound_labels", "g.V().hasLabel('Root').has('uid',p0).as('u').out('L_HIGH').select('u').values('uid')", {"p0": 0}, [0,0,0]),
            ("constant_fold", "g.V().hasLabel('Item').constant(1).fold()", {}, [[1]*6]),
            # Return a large result stream to check complete consumption.
            ("full_consumption", "g.inject(1).repeat(__.union(__.identity(),__.identity())).times(15)", {}, [1]*32768),
        ]:
            columns, rows, elapsed = db.execute(Query(text, params, ("result",)), 10)
            assert columns == ["result"] and Counter(rows) == Counter((freeze(v),) for v in expected), name
            checks[name] = {"status": "passed", "rows": len(rows), "elapsed_ms": elapsed}
        nodes = db.execute(Query("g.V().hasLabel('Root')", columns=("result",)), 10)[1]
        edges = db.execute(Query("g.E().hasLabel('PARALLEL')", columns=("result",)), 10)[1]
        assert len(set(nodes)) == len(set(edges)) == 2
        assert all(row[0][0] == "node" for row in nodes) and all(row[0][0] == "relationship" for row in edges)
        checks["entity_identity"] = "passed"
        for name, a, b in [("bool_int", "g.inject(true)", "g.inject(1)"),
                           ("duplicates", "g.inject(1,1)", "g.inject(1)"),
                           ("list_order", "g.inject([1,2])", "g.inject([2,1])")]:
            result = run_pair(db, Query(a, columns=("result",)), Query(b, columns=("result",)), config["measurement"], check_only=True)
            assert result["status"] == "result_mismatch", name
            checks[name] = "passed"
        for name, query in [
            ("missing_binding", Query("g.V().has('uid',missing)", columns=("result",))),
            ("constant_binding_rejected", Query("g.inject(1).constant(p0)", {"p0": 1}, ("result",))),
        ]:
            try:
                db.execute(query, 10)
            except RuntimeError as error:
                checks[name] = {"status": "passed", "message": str(error)}
            else:
                raise AssertionError(name)
        query = Query("g.V().hasLabel('Root').has('uid',p0).out('L_HIGH').values('uid')", {"p0": 0}, ("result",))
        ordinary, _ = db.read(query, 10)
        profiled, _ = db.read(query, 10, profile=True)
        assert Counter(freeze(row) for row in ordinary["result"]) == Counter(freeze(row) for row in profiled["result"])
        plan = db.profile(query, 10)
        assert plan["plan"]["type"] == "GremlinExecutionPlan" and plan["plan"]["steps"]
        (output / "profile.json").write_text(json.dumps(plan, indent=2))
        checks["profile"] = checks["profile_preserves_results"] = "passed"
        long_query = Query("g.inject(1).repeat(__.identity()).emit().times(100000000)", columns=("result",))
        for name, operation in [("deadline_cancel", db.execute), ("profile_deadline_cancel", db.profile)]:
            started = perf_counter()
            try:
                operation(long_query, 1)
            except TimeoutError as error:
                assert error.timeout_method == "deadline_cancel"
                checks[name] = {"status": "passed", "observed_ms": (perf_counter()-started)*1000, **vars(error)}
            else:
                raise AssertionError(name)
            assert db.execute(Query("g.inject(1)", columns=("result",)), 10)[1] == [(("int", 1),)]
        checks["after_timeout"] = "passed"
        result = run_pair(db, long_query, long_query, {**config["measurement"], "timeout_seconds": 1}, check_only=True)
        assert result["status"] == "timeout" and result["error"]["timeout_method"] == "deadline_cancel"
        assert result["discovery"] == result["confirmation"] == []
        checks["timeout_record"] = result
        tag = "gsp-manual-" + uuid4().hex
        control = HTTPConnection(*db.address, timeout=10)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(db.request, "POST", "/api/v1/query/" + db.name,
                    {"language": "gremlin", "command": long_query.text, "limit": -1}, tag=tag)
                deadline = monotonic() + 3
                while monotonic() < deadline:
                    listing, _ = db.request("POST", "/api/v1/server", {"command": "list queries"}, connection=control)
                    if any(row.get("tag") == tag for row in listing["result"]):
                        break
                    sleep(.05)
                else:
                    raise AssertionError("Tagged Gremlin query was not observed")
                cancellation = db.cancel(tag)
                try:
                    pending.result(timeout=10)
                except InterruptedError:
                    checks["manual_cancel"] = {"status": "passed", "response": cancellation}
                else:
                    raise AssertionError("Manual cancellation did not interrupt the query")
            listing, _ = db.request("POST", "/api/v1/server", {"command": "list queries"}, connection=control)
            assert not any(row.get("tag") == tag for row in listing["result"])
        finally:
            control.close()
        assert db.execute(Query("g.inject(1)", columns=("result",)), 10)[1] == [(("int", 1),)]
        checks["after_cancellation"] = "passed"
        a = Query("g.V().hasLabel('Root').has('uid',p0).out('L_LOW').constant(1).limit(2)", {"p0": 0}, ("result",))
        b = Query(a.text.replace("L_LOW", "L_HIGH"), a.parameters, a.columns)
        result = run_pair(db, a, b, config["measurement"], check_only=True)
        assert result["status"] == "equivalent" and result["rows"] == 2
        save_case(output / "archive", Case("interface", 0, None, (), set()), graph, a, b, result, config, db.version)
        small = Graph(schema={"nodes": {"Root": {"uid": "int"}}, "relationships": {}})
        small.node(["Root"])
        db.load(small)
        assert db.execute(Query("g.V().count()", columns=("result",)), 10)[1] == [(("int", 1),)]
        assert db.execute(Query("g.E().count()", columns=("result",)), 10)[1] == [(("int", 0),)]
        checks["reload"] = "passed"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        db.close()
        (output / "report.json").write_text(json.dumps(report, indent=2))
    replay_config = {**config, "replay": {**config["replay"], "check_only": True}}
    result = replay_case(output / "archive", replay_config)
    assert result["status"] == "equivalent" and result["rows"] == 2
    checks["replay"] = "passed"
    (output / "report.json").write_text(json.dumps(report, indent=2))
    return report


def check_janusgraph(config, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    graph = fixture()
    db = select_database("janusgraph").Database(config["database"])
    report = {"system": "janusgraph", "version": db.version, "checks": {}}
    checks = report["checks"]
    try:
        db.load(graph)
        for name, text, params, expected in [
            ("nodes", "g.V().count()", {}, [9]),
            ("edges", "g.E().count()", {}, [13]),
            ("empty_label", "g.V().hasLabel('Empty')", {}, []),
            ("empty_string", "g.V().hasLabel('Item').has('uid',p0).values('text')", {"p0": 2}, [""]),
            ("missing_property", "g.V().hasLabel('Item').has('uid',p0).values('score')", {"p0": 2}, []),
            ("bindings", "g.inject(i,f,b,s,xs)", {"i": 1, "f": 1.5, "b": True, "s": "", "xs": [1,2,1]}, [1,1.5,True,"",[1,2,1]]),
            ("literals", "g.inject(1,1.0d,1.5f,9007199254740993L)", {}, [1,1.0,1.5,9007199254740993]),
            ("empty_list", "g.V().hasLabel('Empty').fold()", {}, [[]]),
            ("full_consumption", "g.inject(1).repeat(__.union(__.identity(),__.identity())).times(15)", {}, [1]*32768),
        ]:
            columns, rows, _ = db.execute(Query(text, params, ("result",)), 10)
            assert columns == ["result"] and Counter(rows) == Counter((freeze(v),) for v in expected), name
            checks[name] = {"status": "passed", "rows": len(rows)}
        nodes = db.execute(Query("g.V().hasLabel('Root')", columns=("result",)), 10)[1]
        edges = db.execute(Query("g.E().hasLabel('PARALLEL')", columns=("result",)), 10)[1]
        assert len(set(nodes)) == len(set(edges)) == 2
        assert all(row[0][0] == "node" for row in nodes) and all(row[0][0] == "relationship" for row in edges)
        checks["identity_and_parallel_edges"] = "passed"
        for name, a, b in [("bool_int", "g.inject(true)", "g.inject(1)"),
                           ("duplicates", "g.inject(1,1)", "g.inject(1)"),
                           ("list_order", "g.inject([1,2])", "g.inject([2,1])")]:
            result = run_pair(db, Query(a, columns=("result",)), Query(b, columns=("result",)), config["measurement"], check_only=True)
            assert result["status"] == "result_mismatch", name
            checks[name] = "passed"
        for name, text in [("missing_binding", "g.V().has('uid',missing)"), ("syntax_error", "g.V(")]:
            try:
                db.execute(Query(text, columns=("result",)), 10)
            except RuntimeError as error:
                checks[name] = {"status": "passed", "message": str(error)}
            else:
                raise AssertionError(name)
        for name, prefix, expected in [
            ("limit", "g.V().hasLabel('Root').has('uid',p0)", 2),
            ("duplicate_prefix", "g.V().hasLabel('Root').has('uid',p0).union(__.identity(),__.identity())", 2),
            ("empty_prefix", "g.V().hasLabel('Empty')", 0),
        ]:
            a = Query(prefix + ".out('L_LOW').constant(1).limit(2)", {"p0": 0}, ("result",))
            b = Query(a.text.replace("L_LOW", "L_HIGH"), a.parameters, a.columns)
            result = run_pair(db, a, b, config["measurement"], check_only=True)
            assert result["status"] == "equivalent" and result["rows"] == expected
            checks[name] = "passed"
        query = Query("g.V().hasLabel('Root').has('uid',p0).out('L_HIGH').values('uid')", {"p0": 0}, ("result",))
        assert Counter(db.execute(query, 10)[1]) == Counter([(("int", n),) for n in [5,6,7]])
        plan = db.profile(query, 10)
        assert plan["duration_unit"] == "ms" and plan["metrics"]["dur"] >= 0 and plan["metrics"]["metrics"]
        (output / "profile.json").write_text(json.dumps(plan, indent=2))
        checks["profile"] = "passed"
        indexes = db.command("""
            def mgmt = graph.openManagement()
            def names = mgmt.getGraphIndexes(org.apache.tinkerpop.gremlin.structure.Vertex.class).collect { it.name() }
            mgmt.rollback()
            names
        """)
        assert indexes == ["gsp_index_0"], indexes
        checks["only_requested_index"] = "passed"
        save_case(output / "archive", Case("interface", 0, None, (), set()), graph, query, query,
                  {"status": "equivalent"}, config, db.version)
        # Recreating schema must also permit a different type for the same property name.
        for kind, value in [("int", 1), ("string", "changed")]:
            small = Graph(schema={"nodes": {"Root": {"uid": "int", "p_prefix": kind}}, "relationships": {}})
            small.node(["Root"], p_prefix=value)
            db.load(small)
            assert db.execute(Query("g.V().values('p_prefix')", columns=("result",)), 10)[1] == [(freeze(value),)]
            assert db.execute(Query("g.E().count()", columns=("result",)), 10)[1] == [(("int", 0),)]
        checks["schema_reset"] = "passed"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        db.close()
        (output / "report.json").write_text(json.dumps(report, indent=2))
    replay = replay_case(output / "archive", {**config, "replay": {**config["replay"], "check_only": True}})
    assert replay["status"] == "equivalent" and replay["rows"] == 3
    checks["replay"] = "passed"
    (output / "report.json").write_text(json.dumps(report, indent=2))
    return report


def check_hugegraph(config, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    graph = fixture()
    graph.schema["relationships"]["EMPTY_REL"] = {"endpoints": [], "properties": {}}
    db = select_database("hugegraph").Database(config["database"])
    report = {"system": "hugegraph", "version": db.version, "checks": {}}
    checks = report["checks"]
    try:
        db.load(graph)
        report["physical_edge_labels"] = [list(key) + [value] for key, value in db.edge_labels.items()]
        ids = db.vertex_ids
        for name, text, params, expected in [
            ("nodes", "g.V().count()", {}, [9]),
            ("edges", "g.E().count()", {}, [13]),
            ("parent_two_hops", "g.V(p0).out('LINK').out('LINK').values('uid')", {"p0":ids[0]}, [7]),
            ("parent_reverse", "g.V(p0).in('LINK').in('LINK').values('uid')", {"p0":ids[7]}, [0]),
            ("parent_both", "g.V(p0).both('LINK').values('uid')", {"p0":ids[8]}, [0,7]),
            ("parent_global_edges", "g.E().hasLabel('LINK').count()", {}, [2]),
            ("parallel_rows", "g.V(p0).out('PARALLEL').values('uid')", {"p0":ids[0]}, [5,5]),
            ("empty_parent", "g.V(p0).out('EMPTY_REL').count()", {"p0":ids[0]}, [0]),
            ("empty_label", "g.V().hasLabel('Empty')", {}, []),
            ("empty_string", "g.V(p0).values('text')", {"p0":ids[2]}, [""]),
            ("missing_property", "g.V(p0).values('score')", {"p0":ids[2]}, []),
            ("bound_scalars", "g.inject(i,f,b,s,xs)", {"i":1,"f":1.5,"b":True,"s":"","xs":[1,2,1]}, [1,1.5,True,"",[1,2,1]]),
            ("literals", "g.inject(1,1.0d,1.5f,9007199254740993L)", {}, [1,1.0,1.5,9007199254740993]),
            ("empty_list", "g.V().hasLabel('Empty').fold()", {}, [[]]),
            ("full_consumption", "g.inject(1).repeat(__.union(__.identity(),__.identity())).times(15)", {}, [1]*32768),
        ]:
            columns, rows, _ = db.execute(Query(text, params, ("result",)), 10)
            assert columns == ["result"] and Counter(rows) == Counter((freeze(v),) for v in expected), (name, rows[:5])
            checks[name] = {"status":"passed", "rows":len(rows)}
        nodes = db.execute(Query("g.V().hasLabel('Root')", columns=("result",)), 10)[1]
        out_edges = db.execute(Query("g.V(p0).outE('PARALLEL')", {"p0":ids[0]}, ("result",)), 10)[1]
        in_edges = db.execute(Query("g.V(p0).inE('PARALLEL')", {"p0":ids[5]}, ("result",)), 10)[1]
        assert len(set(nodes)) == len(set(out_edges)) == 2 and Counter(out_edges) == Counter(in_edges)
        assert all(r[0][0]=="node" for r in nodes) and all(r[0][0]=="relationship" for r in out_edges)
        raw, _ = db.read(Query("g.V(p0).outE('PARALLEL')", {"p0":ids[0]}, ("result",)), 10)
        (output / "raw-edges.json").write_text(json.dumps(raw, indent=2))
        checks["entity_identity"] = "passed"
        for name, a, b in [("bool_int","g.inject(true)","g.inject(1)"),
                           ("duplicates","g.inject(1,1)","g.inject(1)"),
                           ("list_order","g.inject([1,2])","g.inject([2,1])")]:
            result = run_pair(db, Query(a,columns=("result",)), Query(b,columns=("result",)), config["measurement"], check_only=True)
            assert result["status"] == "result_mismatch", name
            checks[name] = "passed"
        rejected = Query("g.V().hasLabel('Item').has('text',p0).values('uid')", {"p0":"x"}, ("result",))
        good = Query("g.inject(1)", columns=("result",))
        for name, a, b, checked in [("rejected_a",rejected,good,[]), ("rejected_b",good,rejected,["a"])]:
            result = run_pair(db,a,b,config["measurement"],check_only=True)
            assert result["status"] == "backend_rejected" and result["reason"] == "missing_index"
            assert result["checked_queries"] == checked and not result.get("stop_run")
            checks[name] = result
            save_case(output / name, Case("interface",0,None,(),set()), graph,a,b,result,config,db.version)
        assert db.execute(Query("g.V().count()",columns=("result",)),10)[1] == [(("int",9),)]
        indexes = db.command("graph.schema().getIndexLabels().collect { it.name() }")
        assert indexes == ["gsp_index_0"], indexes
        checks["continued_without_added_indexes"] = "passed"
        for name, prefix, expected in [
            ("limit", "g.V().hasLabel('Root').has('uid',p0)", 2),
            ("duplicate_prefix", "g.V().hasLabel('Root').has('uid',p0).union(__.identity(),__.identity())", 2),
            ("empty_prefix", "g.V().hasLabel('Empty')", 0),
        ]:
            a = Query(prefix + ".out('L_LOW').constant(1).limit(2)", {"p0":0}, ("result",))
            b = Query(a.text.replace("L_LOW","L_HIGH"),a.parameters,a.columns)
            result = run_pair(db,a,b,config["measurement"],check_only=True)
            assert result["status"]=="equivalent" and result["rows"]==expected, (name,result)
            checks[name] = "passed"
        q = Query("g.V().hasLabel('Root').has('uid',p0).out('L_HIGH').values('uid')", {"p0":0}, ("result",))
        assert Counter(db.execute(q,10)[1]) == Counter([(("int",n),) for n in [5,6,7]])
        plan = db.profile(q,10)
        assert plan["metrics"]["dur"] >= 0 and plan["metrics"]["metrics"]
        (output / "profile.json").write_text(json.dumps(plan,indent=2))
        save_case(output / "archive",Case("interface",0,None,(),set()),graph,q,q,{"status":"equivalent"},config,db.version)
        checks["profile"] = "passed"
        # A second load must discard old children, properties, and indexes as well as instances.
        for kind,value in [("int",1),("string","changed")]:
            small = Graph(schema={"nodes":{"Root":{"uid":"int","p_prefix":kind}},"relationships":{}})
            small.node(["Root"],p_prefix=value)
            db.load(small)
            assert db.execute(Query("g.V().values('p_prefix')",columns=("result",)),10)[1] == [(freeze(value),)]
            assert db.execute(Query("g.E().count()",columns=("result",)),10)[1] == [(("int",0),)]
            assert db.command("graph.schema().getEdgeLabels().collect { it.name() }") == []
            assert db.command("graph.schema().getIndexLabels().collect { it.name() }") == []
        checks["clear_and_type_reset"] = "passed"
    except Exception as error:
        report["error"] = {"type":type(error).__name__,"message":str(error)}
        raise
    finally:
        db.close()
        (output / "report.json").write_text(json.dumps(report,indent=2))
    result = replay_case(output / "archive", {**config,"replay":{**config["replay"],"check_only":True}})
    assert result["status"]=="equivalent" and result["rows"]==3
    checks["replay"] = "passed"
    (output / "report.json").write_text(json.dumps(report,indent=2))
    return report


if __name__ == "__main__":
    import yaml
    settings = yaml.safe_load(Path("config.yaml").read_text())
    if settings["language"] != "gremlin":
        settings = load_config()
    print(json.dumps(check(settings, Path(settings["run"]["output"]) / "interfaces" / settings["database"]["system"]), indent=2))
