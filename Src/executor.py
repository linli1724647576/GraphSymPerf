"""Small database adapters and paired measurements for reproducible cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import math
import hashlib
import json
import os
import random
import re
import zlib
from statistics import median
from time import perf_counter, monotonic, sleep


DEFAULT_MEASUREMENT = {
    "discovery_warmups": 3, "discovery_pairs": 6, "screen_ratio": 2.0,
    "confirmation_sessions": 2, "confirmation_warmups": 5,
    "confirmation_pairs": 20, "control_pairs": 10, "ratio": 3.0,
    "min_delta_ms": 0.0,
    "ci_low": 2.0, "direction_consistency": 0.75, "bootstrap_samples": 10_000,
    "timeout_seconds": 10.0, "max_rows": 100_000,
    "collect_plans": False,
}


class CaseFailure(Exception):
    def __init__(self, status: str, reason: str, *, stop_target: bool = False, details=None):
        super().__init__(reason)
        self.status, self.reason, self.stop_target = status, reason, stop_target
        self.details = details or {}


def typed_value(value):
    """Keep type and multiplicity; unsupported values cannot prove equivalence."""
    if value is None:
        return ("null",)
    if type(value) in (bool, int, str):
        return (type(value).__name__, value)
    if type(value) is float and math.isfinite(value):
        return ("float", value)
    if type(value) in (list, tuple):
        return ("list", tuple(typed_value(item) for item in value))
    if type(value).__module__.split(".")[0] in {"neo4j", "falkordb"}:
        raise CaseFailure("incomplete_result", f"unsupported_result_type:{type(value).__name__}")
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return ("map", tuple(sorted((key, typed_value(item)) for key, item in value.items())))
    raise CaseFailure("incomplete_result", f"unsupported_result_type:{type(value).__name__}")


def typed_bag(rows):
    return Counter(typed_value(row) for row in rows)


_IGNORED = re.compile(r"//[^\n]*|/\*.*?\*/|'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`", re.S)
_FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH|CALL|USE|SHOW|GRANT|ALTER|REVOKE)\b", re.I)
_CREATE_INDEX = re.compile(r"CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR \((\w+):(\w+)\) ON \(\1\.(\w+)\)", re.I)
_INDEX_CHECK = ("SHOW INDEXES YIELD name, state, labelsOrTypes, properties "
                "WHERE name = 'gsp_cost_lookup' RETURN name, state, labelsOrTypes, properties")


def supported_patterns(config):
    """Conservative syntax coverage; a server can still reject a newer feature."""
    name = config.get("dbms", "neo4j").lower()
    language = config.get("language", "cypher")
    if language == "gremlin" and name in {"janusgraph", "hugegraph"}:
        patterns = {f"P{i}" for i in range(1, 16)} - {"P3", "P7", "P13"}
        if name == "hugegraph":
            patterns -= {"P12"}
        return patterns
    if language != "cypher":
        return set()
    patterns = {f"P{i}" for i in range(1, 17)} | {f"S{i}" for i in range(1, 11)}
    if name == "neo4j":
        return patterns
    if name == "memgraph":
        return patterns - {"S3"}
    if name == "falkordb":
        # Keep EXISTS/COUNT subqueries intact: rewriting them as list creation
        # changes the optimization mechanism that this experiment measures.
        return patterns - {"P4", "P5", "P15", "S1", "S2", "S3", "S4", "S7", "S8", "S10"}
    if name in {"arcadedb", "age", "ladybug"}:
        return patterns - {"P7", "S3"}
    raise ValueError(f"unsupported dbms: {name}")


def make_database(config, settings):
    name = config.get("dbms", "neo4j").lower()
    adapters = {"neo4j": Neo4j, "memgraph": Memgraph, "falkordb": FalkorDB,
                "arcadedb": ArcadeDB, "age": ApacheAGE, "ladybug": LadybugDB,
                "janusgraph": JanusGraph, "hugegraph": ApacheHugeGraph}
    if name not in adapters:
        raise CaseFailure("configuration_error", "unsupported_dbms", stop_target=True)
    return adapters[name](config, settings)


def _target_name(config):
    if config.get("allow_reset") is not True or config.get("dedicated") is not True:
        raise CaseFailure("configuration_error", "dedicated_database_and_allow_reset_required", stop_target=True)
    name = config.get("database")
    if not isinstance(name, str) or not name.strip() or name.lower() == "system":
        raise CaseFailure("configuration_error", "explicit_non_system_database_required", stop_target=True)
    return name


def _password(config, *, optional=False):
    name = config.get("password_env")
    if optional and not name:
        return None
    if not name or name not in os.environ:
        raise CaseFailure("configuration_error", "database_password_env_missing", stop_target=True)
    return os.environ[name]


def _index_spec(state):
    indexes = []
    for statement in state["setup"]:
        if re.match(r"^CREATE\s+INDEX\b", statement, re.I):
            match = _CREATE_INDEX.fullmatch(statement.strip().rstrip(";"))
            if not match:
                raise CaseFailure("unsupported_feature", "unsupported_index_definition")
            indexes.append((match[2], match[3]))
    if len(indexes) > 1:
        raise CaseFailure("unsupported_feature", "only_one_cost_index_supported")
    return indexes[0] if indexes else None


def _canonical_index(spec):
    return {"name": "gsp_cost_lookup", "state": "ONLINE", "labelsOrTypes": [spec[0]], "properties": [spec[1]]}


def _syntax_error(code, message):
    memgraph_parse_error = (code.startswith("Memgraph.ClientError.") and
                            "The underlying parsing error is no viable alternative at input" in message)
    return (code.endswith(".SyntaxError") or memgraph_parse_error or
            bool(re.search(r"\bnot yet implemented\b|\bis not implemented yet\b|\bsyntax error\b", message, re.I)))


def validate_read_query(query, *, schema_check=False):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("read query must be non-empty text")
    code = _IGNORED.sub(" ", query).strip().rstrip(";").strip()
    if re.match(r"^CYPHER\s+25\s+WHEN\b", code, re.I):
        code = re.sub(r"^CYPHER\s+25\s+", "", code, count=1, flags=re.I)
        if ";" in code or _FORBIDDEN.search(code):
            raise ValueError("conditional query must remain read-only")
        return
    if schema_check and re.match(r"^SHOW\s+INDEXES\b", code, re.I):
        code = re.sub(r"^SHOW\s+INDEXES\b", "RETURN", code, count=1, flags=re.I)
    if ";" in code or _FORBIDDEN.search(code) or not re.match(r"^(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|RETURN)\b", code, re.I):
        raise ValueError("query is outside the supported read-only fragment")


class Neo4j:
    """One connected driver; destructive setup is limited to an explicit test DB."""

    def __init__(self, config: dict, settings: dict):
        name = _target_name(config)
        unauthenticated = isinstance(self, Memgraph) and not config.get("user") and not config.get("password_env")
        password = _password(config, optional=unauthenticated)
        from neo4j import GraphDatabase, Query
        from neo4j.exceptions import Neo4jError
        self.database, self.settings = name, settings
        self._query, self._server_error = Query, Neo4jError
        self.driver = GraphDatabase.driver(config["uri"], auth=None if unauthenticated else (config["user"], password),
                                          connection_timeout=settings["timeout_seconds"],
                                          connection_acquisition_timeout=settings["timeout_seconds"])
        try:
            self.driver.verify_connectivity()
            self.version = self.driver.get_server_info().agent
            # Establish this database's connection before any query timer starts.
            with self.driver.session(database=self.database) as session:
                session.run("RETURN 1 AS connected").consume()
        except Exception:
            self.driver.close()
            raise

    def _execute(self, query: str, parameters: dict, timeout: float) -> dict:
        rows = []
        try:
            with self.driver.session(database=self.database) as session:
                started = perf_counter()
                cursor = session.run(self._query(query, timeout=timeout), parameters)
                complete = True
                for record in cursor:
                    if len(rows) == self.settings["max_rows"]:
                        complete = False
                        break
                    rows.append(dict(record))
                cursor.consume()
                elapsed = (perf_counter() - started) * 1000
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            identity = code or type(exc).__name__
            message = str(exc)
            acknowledged = isinstance(exc, self._server_error) and (
                "TransactionTimedOut" in code or
                isinstance(self, Memgraph) and "Transaction was asked to abort because of transaction timeout." in message)
            if acknowledged or isinstance(exc, TimeoutError) or re.search(r"timeout|timedout", identity, re.I):
                # A network timeout is not proof that the server cancelled SQL.
                # A database timeout reply is an acknowledgement; additionally
                # require a new successful query before permitting continuation.
                elapsed = max(0.0, (perf_counter() - started) * 1000) if "started" in locals() else 0.0
                healthy = acknowledged and self.health()
                raise CaseFailure("timeout", identity, stop_target=not healthy, details={
                    "cancellation_verified": bool(healthy),
                    "elapsed_lower_bound_ms": min(timeout * 1000, elapsed) if acknowledged else None}) from exc
            if isinstance(exc, self._server_error):
                if _syntax_error(code, message):
                    raise CaseFailure("unsupported_feature", identity, details={"server_message": message[:1000]}) from exc
                raise CaseFailure("query_error", identity, details={"server_message": message[:1000]}) from exc
            raise CaseFailure("environment_error", type(exc).__name__, stop_target=True) from exc
        return {"rows": rows, "complete": complete, "elapsed_ms": elapsed}

    def execute(self, query: str, parameters: dict, timeout: float) -> dict:
        return self._execute(query, parameters, timeout)

    def health(self):
        try:
            # Bypass execute overrides and error recovery to avoid recursion.
            with self.driver.session(database=self.database) as session:
                rows = [dict(record) for record in session.run(self._query("RETURN 1 AS connected", timeout=self.settings["timeout_seconds"]))]
            return rows == [{"connected": 1}]
        except Exception:
            return False

    def restore(self, state: dict):
        timeout = self.settings["timeout_seconds"]
        indexes = self.execute("SHOW INDEXES YIELD name, type RETURN name, type", {}, timeout)
        constraints = self.execute("SHOW CONSTRAINTS YIELD name RETURN name", {}, timeout)
        if not indexes["complete"] or not constraints["complete"]:
            raise CaseFailure("environment_error", "schema_inspection_incomplete", stop_target=True)
        if constraints["rows"] or any(row["type"] != "LOOKUP" and row["name"] != "gsp_cost_lookup" for row in indexes["rows"]):
            raise CaseFailure("environment_error", "unexpected_schema_in_test_database", stop_target=True)
        self.execute("DROP INDEX gsp_cost_lookup IF EXISTS", {}, timeout)
        # Delete relationships before nodes, in bounded transactions. A dense
        # root can otherwise make DETACH DELETE exceed the transaction pool
        # while rebuilding a perfectly valid large experiment state.
        for query in ("MATCH ()-[r]->() WITH r LIMIT 10000 DELETE r RETURN count(*) AS removed",
                      "MATCH (n) WITH n LIMIT 10000 DELETE n RETURN count(*) AS removed"):
            while True:
                result = self.execute(query, {}, timeout)
                rows = result["rows"]
                if (not result["complete"] or len(rows) != 1 or type(rows[0].get("removed")) is not int
                        or not 0 <= rows[0]["removed"] <= 10000):
                    raise CaseFailure("environment_error", "invalid_reset_batch_result", stop_target=True)
                if rows[0]["removed"] == 0:
                    break
        for query in state["setup"]:
            self.execute(query, {}, timeout)
        self.execute("CALL db.awaitIndexes($seconds)", {"seconds": max(1, math.ceil(timeout))}, timeout)
        # Rebuilding a graph can leave a cached plan with cardinalities from
        # its previous state. Refresh outside query timers before classifying
        # a performance difference as an optimizer candidate.
        self.execute("CALL db.prepareForReplanning()", {}, timeout)

    def close(self):
        self.driver.close()

    def explain(self, query, parameters, analyze=True):
        with self.driver.session(database=self.database) as session:
            cursor = session.run(self._query(("PROFILE " if analyze else "EXPLAIN ") + query,
                                            timeout=self.settings["timeout_seconds"]), parameters)
            rows = [dict(row) for row in cursor] if isinstance(self, Memgraph) else []
            summary = cursor.consume()
            return {"format": "native", "analyze": analyze,
                    "plan": summary.profile if analyze else summary.plan, "rows": rows}


class Memgraph(Neo4j):
    """Bolt execution, with Memgraph's own version and label-property indexes."""

    def __init__(self, config, settings):
        super().__init__(config, settings)
        try:
            version = self._execute("SHOW VERSION", {}, settings["timeout_seconds"])
            if not version["complete"] or len(version["rows"]) != 1:
                raise CaseFailure("environment_error", "memgraph_version_unavailable", stop_target=True)
            self.version = "Memgraph/" + str(next(iter(version["rows"][0].values())))
            self._index_spec = None
        except Exception:
            self.close()
            raise

    def prepare_state(self, state):
        self._index_spec = _index_spec(state)

    @staticmethod
    def _index_row(row):
        label, prop = row.get("label"), row.get("property")
        kind = row.get("index type", row.get("type"))
        if isinstance(prop, list):
            if not prop:
                prop = None
            elif len(prop) == 1 and isinstance(prop[0], str):
                prop = prop[0]
            else:
                raise CaseFailure("environment_error", "unknown_memgraph_index_metadata", stop_target=True)
        if (kind not in {"label", "label+property"} or not isinstance(label, str) or
                prop is not None and not isinstance(prop, str) or (kind == "label") != (prop is None)):
            raise CaseFailure("environment_error", "unknown_memgraph_index_metadata", stop_target=True)
        return label, prop

    def execute(self, query, parameters, timeout):
        if query == _INDEX_CHECK:
            result = self._execute("SHOW INDEX INFO", {}, timeout)
            present = self._index_spec in [self._index_row(row) for row in result["rows"]]
            return {**result, "rows": [_canonical_index(self._index_spec)] if present else []}
        code = _IGNORED.sub(" ", query)
        if re.match(r"^CYPHER\s+25\b", code, re.I) or re.match(r"^SHOW\s+INDEXES\b", code, re.I):
            raise CaseFailure("unsupported_feature", "unsupported_memgraph_syntax")
        if re.match(r"^CREATE\s+INDEX\b", code, re.I):
            match = _CREATE_INDEX.fullmatch(query.strip().rstrip(";"))
            if not match:
                raise CaseFailure("unsupported_feature", "unsupported_index_definition")
            query = f"CREATE INDEX ON :{match[2]}({match[3]})"
        return self._execute(query, parameters, timeout)

    def restore(self, state):
        self.prepare_state(state)
        timeout = self.settings["timeout_seconds"]
        indexes = self._execute("SHOW INDEX INFO", {}, timeout)
        constraints = self._execute("SHOW CONSTRAINT INFO", {}, timeout)
        if not indexes["complete"] or not constraints["complete"] or constraints["rows"]:
            raise CaseFailure("environment_error", "unexpected_schema_in_test_database", stop_target=True)
        for row in indexes["rows"]:
            label, prop = self._index_row(row)
            if not re.fullmatch(r"(?:Gsp|Scor)\w+", label) or prop not in {None, "id", "uid"}:
                raise CaseFailure("environment_error", "unexpected_schema_in_test_database", stop_target=True)
            self._execute(f"DROP INDEX ON :{label}" + (f"({prop})" if prop else ""), {}, timeout)
        self._execute("MATCH (n) DETACH DELETE n", {}, timeout)
        for query in state["setup"]:
            self.execute(query, {}, timeout)
        # Memgraph creates these indexes synchronously; check_case inspects the
        # actual label-property entry before measuring any query.
        # Analyze after loading so the planner sees current cardinalities and
        # invalidates plans cached for a previous graph, outside query timers.
        statistics = self._execute("ANALYZE GRAPH", {}, timeout)
        if not statistics["complete"]:
            raise CaseFailure("environment_error", "statistics_preparation_incomplete", stop_target=True)
        self.statistics_evidence = statistics["rows"]


class FalkorDB:
    """One named dedicated graph; never flush the shared Redis instance."""

    def __init__(self, config, settings):
        self.database, self.settings = _target_name(config), settings
        password = _password(config, optional=True)
        from falkordb import FalkorDB as Client
        from redis.exceptions import ResponseError, TimeoutError as RedisTimeoutError
        self._response_error, self._timeout_error = ResponseError, RedisTimeoutError
        self.client = Client.from_url(config["uri"], password=password,
                                      socket_connect_timeout=settings["timeout_seconds"],
                                      socket_timeout=settings["timeout_seconds"] + 5)
        try:
            # FalkorDB may silently truncate at this server-wide cap. Do not
            # change a shared setting; require an uncapped result or a cap high
            # enough that our own max_rows + 1 observation detects truncation.
            self.server_result_limit = int(self.client.config_get("RESULTSET_SIZE"))
            if self.server_result_limit != -1 and self.server_result_limit < settings["max_rows"] + 1:
                raise CaseFailure("configuration_error", "falkordb_resultset_size_too_small", stop_target=True,
                                  details={"server_resultset_size": self.server_result_limit,
                                           "required_minimum": settings["max_rows"] + 1})
            modules = self.client.execute_command("MODULE", "LIST")
            versions = []
            for entry in modules:
                fields = entry if isinstance(entry, dict) else dict(zip(entry[::2], entry[1::2]))
                fields = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}
                name = fields.get("name", "")
                if isinstance(name, bytes):
                    name = name.decode()
                if name.lower() in {"graph", "falkordb"}:
                    versions.append(fields["ver"])
            if len(versions) != 1:
                raise CaseFailure("environment_error", "falkordb_module_version_unavailable", stop_target=True)
            self.version = f"FalkorDB/module_version={versions[0]}"
            self.graph = self.client.select_graph(self.database)
            self._index_spec = None
        except Exception:
            self.close()
            raise

    def prepare_state(self, state):
        self._index_spec = _index_spec(state)

    @staticmethod
    def _rows(result):
        # The official client returns [column_type, column_name] headers and
        # Python scalar/list/map values. Reject ambiguous duplicate columns.
        names = [column[1] for column in result.header]
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise CaseFailure("incomplete_result", "ambiguous_result_columns")
        if any(len(row) != len(names) for row in result.result_set):
            raise CaseFailure("incomplete_result", "invalid_result_width")
        return [dict(zip(names, row)) for row in result.result_set]

    def _execute(self, query, parameters, timeout):
        started = perf_counter()
        try:
            result = self.graph.query(query, params=parameters or None, timeout=max(1, math.ceil(timeout * 1000)))
            rows = self._rows(result)
            elapsed = (perf_counter() - started) * 1000
        except self._response_error as exc:
            message = str(exc)
            if message.strip() == "Query timed out":
                elapsed = max(0.0, (perf_counter() - started) * 1000)
                healthy = self.health()
                raise CaseFailure("timeout", "Query timed out", stop_target=not healthy, details={
                    "cancellation_verified": healthy, "elapsed_lower_bound_ms": min(timeout * 1000, elapsed)}) from exc
            if _syntax_error("", message):
                raise CaseFailure("unsupported_feature", "falkordb_syntax_error") from exc
            raise CaseFailure("query_error", message) from exc
        except self._timeout_error as exc:
            raise CaseFailure("timeout", "client_timeout", stop_target=True,
                              details={"cancellation_verified": False, "elapsed_lower_bound_ms": None}) from exc
        except CaseFailure:
            raise
        except Exception as exc:
            raise CaseFailure("environment_error", type(exc).__name__, stop_target=True) from exc
        complete = len(rows) <= self.settings["max_rows"]
        return {"rows": rows[:self.settings["max_rows"]], "complete": complete, "elapsed_ms": elapsed}

    def health(self):
        try:
            result = self.graph.query("RETURN 1 AS connected", timeout=max(1, math.ceil(self.settings["timeout_seconds"] * 1000)))
            return self._rows(result) == [{"connected": 1}]
        except Exception:
            return False

    def execute(self, query, parameters, timeout):
        if query == _INDEX_CHECK:
            result = self._execute("CALL db.indexes() YIELD label, properties, status RETURN label, properties, status", {}, timeout)
            rows = [row for row in result["rows"] if self._index_spec is not None and
                    row.get("label") == self._index_spec[0] and row.get("properties") == [self._index_spec[1]] and
                    row.get("status") == "OPERATIONAL"]
            return {**result, "rows": [_canonical_index(self._index_spec)] if rows else []}
        code = _IGNORED.sub(" ", query)
        if re.search(r"\b(?:COUNT|EXISTS)\s*\{", code, re.I) or re.match(r"^CYPHER\s+25\b|^SHOW\b", code, re.I):
            raise CaseFailure("unsupported_feature", "unsupported_falkordb_syntax")
        if re.match(r"^CREATE\s+INDEX\b", code, re.I):
            match = _CREATE_INDEX.fullmatch(query.strip().rstrip(";"))
            if not match:
                raise CaseFailure("unsupported_feature", "unsupported_index_definition")
            query = f"CREATE INDEX FOR (n:{match[2]}) ON (n.{match[3]})"
        return self._execute(query, parameters, timeout)

    def restore(self, state):
        self.prepare_state(state)
        if self.database in self.client.list_graphs():
            self.graph.delete()
        self.graph = self.client.select_graph(self.database)
        timeout = self.settings["timeout_seconds"]
        for query in state["setup"]:
            self.execute(query, {}, timeout)
        if self._index_spec is not None:
            deadline = monotonic() + timeout
            while not self.execute(_INDEX_CHECK, {}, timeout)["rows"]:
                if monotonic() >= deadline:
                    raise CaseFailure("environment_error", "index_not_operational", stop_target=True)
                sleep(0.05)

    def close(self):
        self.client.close()

    def explain(self, query, parameters, analyze=True):
        command = "GRAPH.PROFILE" if analyze else "GRAPH.EXPLAIN"
        text = self.graph._build_params_header(parameters or None) + query
        arguments = [command, self.database, text]
        if analyze:
            arguments.extend(("TIMEOUT", max(1, math.ceil(self.settings["timeout_seconds"] * 1000))))
        rows = self.graph.execute_command(*arguments)
        return {"format": "native", "analyze": analyze,
                "operators": [row.decode() if isinstance(row, bytes) else row for row in rows]}


def _query_input(case, side):
    bindings = case.get("query_bindings", {}).get(side)
    return {"text": case[f"query_{side}"], "bindings": bindings} if bindings else case[f"query_{side}"]


def _same_query(left, right):
    """Request identity includes scalar binding types, unlike Python equality."""
    left_text, left_bindings = (left["text"], left["bindings"]) if isinstance(left, dict) else (left, {})
    right_text, right_bindings = (right["text"], right["bindings"]) if isinstance(right, dict) else (right, {})
    return left_text == right_text and typed_value(left_bindings) == typed_value(right_bindings)


def _result(db, query, parameters, settings):
    if isinstance(query, dict):
        parameters = {**parameters, **query["bindings"]}
        query = query["text"]
    try:
        result = db.execute(query, parameters, settings["timeout_seconds"])
    except CaseFailure as exc:
        if exc.status == "timeout":
            exc.details["timed_out_query"] = query
        raise
    if not result.get("complete") or len(result["rows"]) > settings["max_rows"]:
        raise CaseFailure("incomplete_result", "result_row_limit_exceeded")
    columns = getattr(db, "_output_columns", {}).get(query)
    if columns:
        physical = {item[0] for item in columns}
        if any(set(row) != physical for row in result["rows"]):
            raise CaseFailure("incomplete_result", "compiled_projection_columns_differ")
        result = {**result, "rows": [{logical: row[source] for source, logical in columns} for row in result["rows"]]}
    elapsed = result.get("elapsed_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0:
        raise CaseFailure("measurement_error", "invalid_elapsed_time")
    return result, typed_bag(result["rows"])


def check_case(db, case: dict, settings: dict):
    """Check cost facts, a full independent reference when supplied, and A/B."""
    if hasattr(db, "check_state"):
        evidence = db.check_state(case)
        db._state_evidence = {"checks": evidence, "indexes": getattr(db, "index_evidence", [])}
    else:
        for index, check in enumerate(case.get("checks", [])):
            _, actual = _result(db, check["query"], case.get("parameters", {}), settings)
            if actual != typed_bag(check["expected_rows"]):
                raise CaseFailure("cost_check_failed", f"cost_check_{index}")
    parameters = case.get("parameters", {})
    reference = (_query_input(case, "ref") if case.get("query_ref") else
                 _query_input(case, "b") if case.get("reference_side") == "b" else None)
    db._reference_query = reference
    db._reference_evidence = None
    if reference:
        reference_result, reference_bag = _result(db, reference, parameters, settings)
        if "expected_rows" in case and reference_bag != typed_bag(case["expected_rows"]):
            raise CaseFailure("reference_mismatch", "reference_differs_from_constructed_result", details={
                "actual_reference_rows": reference_result["rows"][:10], "expected_rows": case["expected_rows"][:10]})
        db._reference_evidence = {"elapsed_ms": reference_result["elapsed_ms"],
                                  "validated": "expected_rows" in case}
    left, baseline = _result(db, _query_input(case, "a"), parameters, settings)
    if _same_query(reference, _query_input(case, "b")):
        right_result, right = reference_result, reference_bag
    else:
        right_result, right = _result(db, _query_input(case, "b"), parameters, settings)
    if right != baseline:
        raise CaseFailure("result_mismatch", "query_a_and_b_differ", details={
            "rows_a": len(left["rows"]), "rows_b": len(right_result["rows"]),
            "sample_a": left["rows"][:10], "sample_b": right_result["rows"][:10]})
    if "expected_rows" in case and baseline != typed_bag(case["expected_rows"]):
        raise CaseFailure("reference_mismatch", "queries_differ_from_constructed_reference", details={
            "actual_rows": left["rows"][:10], "expected_rows": case["expected_rows"][:10]})
    if reference and reference_bag != baseline:
        raise CaseFailure("reference_mismatch", "queries_differ_from_reference_query")
    return baseline, len(left["rows"])


def _schedule(count, rng):
    orders = ["AB"] * ((count + 1) // 2) + ["BA"] * (count // 2)
    rng.shuffle(orders)
    return orders


def _measure(db, case, baseline, settings, rng, count, output, *, queries=None):
    queries = queries or (_query_input(case, "a"), _query_input(case, "b"))
    for order in _schedule(count, rng):
        sample = {"order": order, "a_ms": None, "b_ms": None, "a_status": "pending", "b_status": "pending"}
        output.append(sample)
        for side in order.lower():
            try:
                result, bag = _result(db, queries[0 if side == "a" else 1], case.get("parameters", {}), settings)
                if bag != baseline:
                    raise CaseFailure("result_mismatch", "result_changed_during_measurement")
                if _same_query(queries[0 if side == "a" else 1], getattr(db, "_reference_query", None)):
                    db._reference_evidence = {"elapsed_ms": result["elapsed_ms"],
                                              "validated": getattr(db, "_reference_evidence", {}).get("validated", False)}
                sample[f"{side}_ms"] = result["elapsed_ms"]
                sample[f"{side}_status"] = "ok"
            except CaseFailure as exc:
                sample[f"{side}_status"] = exc.status
                raise


def _quantile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def summarize(samples, slow_side, *, bootstrap_samples=0, seed=0):
    fast_side = "b" if slow_side == "a" else "a"
    pairs = [(sample[f"{fast_side}_ms"], sample[f"{slow_side}_ms"]) for sample in samples]
    ratios = [slow / fast for fast, slow in pairs]
    deltas = [slow - fast for fast, slow in pairs]
    result = {"slow_side": slow_side, "median_ratio": median(ratios), "median_delta_ms": median(deltas),
              "direction_consistency": sum(delta > 0 for delta in deltas) / len(deltas)}
    if bootstrap_samples:
        rng = random.Random(seed)
        draws = [median(ratios[rng.randrange(len(ratios))] for _ in ratios) for _ in range(bootstrap_samples)]
        result.update(ratio_ci_low=_quantile(draws, 0.025), ratio_ci_high=_quantile(draws, 0.975))
    return result


def measurement_settings(measurement):
    unknown = set(measurement) - set(DEFAULT_MEASUREMENT)
    if unknown:
        raise ValueError(f"unknown measurement settings: {sorted(unknown)}")
    settings = {**DEFAULT_MEASUREMENT, **measurement}
    if type(settings["collect_plans"]) is not bool:
        raise ValueError("collect_plans must be boolean")
    for key in ("discovery_warmups", "discovery_pairs", "confirmation_sessions", "confirmation_warmups", "confirmation_pairs", "control_pairs", "bootstrap_samples", "max_rows"):
        value = settings[key]
        if type(value) is not int or value < (0 if "warmups" in key else 1):
            raise ValueError(f"{key} must be a positive integer (warmups may be zero)")
    if settings["confirmation_sessions"] != 2:
        raise ValueError("confirmation_sessions must be 2")
    for key in ("discovery_pairs", "confirmation_pairs", "control_pairs"):
        if settings[key] % 2:
            raise ValueError(f"{key} must be even for balanced AB/BA")
    for key in ("screen_ratio", "ratio", "ci_low", "direction_consistency", "timeout_seconds"):
        value = settings[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid {key}")
    delta = settings["min_delta_ms"]
    if type(delta) not in (float, int) or not math.isfinite(delta) or delta < 0:
        raise ValueError("min_delta_ms must be finite and nonnegative")
    if not 0.5 < settings["direction_consistency"] <= 1 or settings["ci_low"] <= 1 or settings["ratio"] <= 1 or settings["screen_ratio"] <= 1:
        raise ValueError("ratio gates must exceed 1; direction_consistency must be in (0.5, 1]")
    return settings


def state_fingerprint(case, database):
    """Identify the actual graph/checks, not the pattern or random seed."""
    state = {"setup": case["setup"], "checks": case.get("checks", []),
             "graph_spec": case.get("graph_spec"), "state_checks": case.get("state_checks"),
             "target": {key: database.get(key) for key in ("dbms", "uri", "path", "database", "graph", "graph_binding", "traversal_source", "user", "language")}}
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _restore_state(db, case, fingerprint, cache, *, force=False):
    if hasattr(db, "prepare_state"):
        db.prepare_state(case)
    reused = (not force and getattr(db, "persistent_state", True) and cache is not None
              and cache.get("fingerprint") == fingerprint and db.health())
    if not reused:
        if cache is not None:
            cache.clear()
        db.restore(case)
    return reused


def _remember_state(db, fingerprint, cache):
    if cache is not None:
        if not db.health():
            raise CaseFailure("environment_error", "state_health_check_failed", stop_target=True)
        cache["fingerprint"] = fingerprint


def _passed(summary, sessions, settings):
    stable = all(session["summary"]["direction_consistency"] >= settings["direction_consistency"] and
                 session["summary"]["median_delta_ms"] > 0 for session in sessions)
    return (stable and summary["median_ratio"] >= settings["ratio"] and
            summary["ratio_ci_low"] >= settings["ci_low"] and
            summary["median_delta_ms"] > settings["min_delta_ms"] and
            summary["median_delta_ms"] > summary["noise_p99_ms"])


def _bind_outputs(db, case):
    db._output_columns = {}
    for side, columns in case.get("output_columns", {}).items():
        text = case[f"query_{side}"]
        if text in db._output_columns and db._output_columns[text] != columns:
            raise CaseFailure("invalid_case", "same_query_has_conflicting_output_columns")
        db._output_columns[text] = columns
    if hasattr(db, "_columns"):
        db._columns.update({query: tuple(item[0] for item in columns)
                            for query, columns in db._output_columns.items()})


def _collect_plans(db, case, report):
    """Native diagnostics run after timing and do not change candidate gates."""
    plans = {}
    for side in ("a", "b", "ref"):
        if not case.get(f"query_{side}"):
            continue
        try:
            parameters = {**case.get("parameters", {}), **case.get("query_bindings", {}).get(side, {})}
            plans[side] = db.explain(case[f"query_{side}"], parameters, analyze=True)
        except Exception as exc:
            plans[side] = {"status": "unavailable", "reason": type(exc).__name__}
            if isinstance(exc, CaseFailure):
                plans[side].update(reason=exc.reason, **exc.details)
                if exc.stop_target:
                    report["stop_target"] = True
                    break
            elif (isinstance(exc, (TimeoutError, ConnectionError, OSError))
                  or re.search(r"timeout|timedout|serviceunavailable|sessionexpired|connection|transport|socket|pipe",
                               str(getattr(exc, "code", type(exc).__name__)), re.I)):
                # Diagnostic failure must not leave an unknown running query
                # while the following case starts measuring.
                report["stop_target"] = True
                break
    report["plans"] = plans
    if hasattr(db, "index_evidence"):
        report["index_evidence"] = db.index_evidence


def run_case(case: dict, database: dict, measurement: dict | None = None, *, state_cache: dict | None = None) -> dict:
    """Execute one case. Failures are reportable; stop_target forbids continuation."""
    report = {"status": "started", "reason": "", "stop_target": False, "dbms": database.get("dbms", "neo4j"),
              "language": database.get("language", "cypher"),
              "discovery": {"warmups": [], "samples": []}, "confirmation_sessions": [], "summary": None,
              "reference_comparisons": {}, "comparisons": {}}
    # Keep the old top-level sample fields, but never use the aggregate report
    # as the AB comparison itself: a reference winner must not overwrite AB.
    reference_pair = case.get("reference_side") == "b" or case.get("pattern") in tuple(f"S{i}" for i in range(1, 11))
    traversal_mode = database.get("language", "cypher") == "gremlin"
    ab = {"pair_kind": "explicit_traversal" if traversal_mode else "reference" if reference_pair else "symmetry", "status": "started",
          "discovery": report["discovery"], "confirmation_sessions": report["confirmation_sessions"], "summary": None}
    report["pair_kind"] = ab["pair_kind"]
    report["comparisons"]["ab"] = ab
    db = None
    current_comparison = None
    comparison_failure_status = None
    phase = "configuration"
    try:
        settings = measurement_settings(measurement or {})
        report["measurement"] = settings
        if case.get("compile_error"):
            raise CaseFailure(case["compile_error"]["status"], case["compile_error"]["reason"])
        if case.get("query_ir"):
            from ir import from_data
            from languages import UnsupportedFeature, UnsupportedLanguage, validate_language
            try:
                for value in case["query_ir"].values():
                    validate_language(from_data(value), database.get("dbms", "neo4j"),
                                      database.get("language", "cypher"),
                                      verified_features=database.get("verified_features", ()))
            except (UnsupportedFeature, UnsupportedLanguage) as exc:
                raise CaseFailure("unsupported_feature", str(exc)) from exc
        if case.get("graph_spec"):
            from state import GraphSpec
            spec = GraphSpec.from_dict(case["graph_spec"])
            if (case.get("state_checks", spec.state_checks()) != spec.state_checks()
                    or case.get("index_requirements", spec.indexes) != spec.indexes):
                raise CaseFailure("invalid_case", "graph_recipe_and_cost_metadata_differ")
        if database.get("language", "cypher") not in {"cypher", "gremlin"}:
            raise CaseFailure("unsupported_feature", "language_generator_not_implemented")
        if traversal_mode and not case.get("query_ir"):
            raise CaseFailure("invalid_case", "gremlin_requires_checked_query_ir")
        if (case.get("target_dbms") and case["target_dbms"] != database.get("dbms", "neo4j")
                and not {case["target_dbms"], database.get("dbms", "neo4j")} <= {"neo4j", "memgraph", "falkordb"}):
            raise CaseFailure("invalid_case", "query_compiled_for_different_database")
        for side in ("a", "b", *(("ref",) if case.get("query_ref") else ())):
            if traversal_mode:
                from languages import generate
                compiled = generate(from_data(case["query_ir"][side]), database["dbms"], "gremlin",
                                    verified_features=database.get("verified_features", ()))
                if (compiled.text != case[f"query_{side}"] or
                        typed_value(compiled.bindings) != typed_value(case.get("query_bindings", {}).get(side, {}))):
                    raise CaseFailure("invalid_case", "gremlin_text_or_bindings_differ_from_ir")
            else:
                validate_read_query(case[f"query_{side}"])
        if case.get("reference_side") not in (None, "b", "ref") or case.get("reference_side") == "ref" and not case.get("query_ref"):
            raise ValueError("reference_side must identify b or an existing query_ref")
        guard = case.get("bindings", {}).get("scor", {})
        if guard.get("guard_facts"):
            if any(guard.get(name) not in case.get("checks", [])
                   for name in ("guard_check", "guard_cardinality_check")):
                raise ValueError("dynamic guard requires its value and cardinality checks")
        if case.get("pattern") and case["pattern"] not in supported_patterns(database):
            raise CaseFailure("unsupported_feature", f"unsupported_pattern:{case['pattern']}")
        if not traversal_mode:
            for check in case.get("checks", []):
                validate_read_query(check["query"], schema_check=True)
        seed = case.get("seed", 0)
        if type(seed) is not int:
            raise ValueError("case seed must be an integer")
        fingerprint = state_fingerprint(case, database)
        phase = "discovery_setup"
        db = make_database(database, settings)
        _bind_outputs(db, case)
        report["database_version"] = getattr(db, "version", "unknown")
        if hasattr(db, "server_result_limit"):
            report["server_resultset_size"] = db.server_result_limit
        if case.get("required_cypher") == 25:
            try:
                db.execute("CYPHER 25 RETURN 1 AS supported", {}, settings["timeout_seconds"])
            except CaseFailure as exc:
                if exc.status == "query_error":
                    raise CaseFailure("unsupported_feature", "Cypher_25_required") from exc
                raise
        report["state_reused"] = _restore_state(db, case, fingerprint, state_cache)
        baseline, report["result_rows"] = check_case(db, case, settings)
        if hasattr(db, "statistics_evidence"):
            report.setdefault("statistics_evidence", []).append({"phase": phase, "rows": db.statistics_evidence})
        if hasattr(db, "_state_evidence"):
            report["state_evidence"] = db._state_evidence
        if hasattr(db, "query_engine"):
            report["query_engine"] = db.query_engine
        _remember_state(db, fingerprint, state_cache)
        phase = "discovery"
        rng = random.Random(seed)
        comparisons = [("ab", (_query_input(case, "a"), _query_input(case, "b")), ab)]
        if case.get("query_ref"):
            for side in ("a", "b"):
                name = f"query_{side}_vs_ref"
                comparison = {"pair_kind": "explicit_traversal" if traversal_mode else "reference", "status": "started",
                              "discovery": {"warmups": [], "samples": []}, "confirmation_sessions": [], "summary": None}
                report["reference_comparisons"][name] = comparison
                comparisons.append((name, (_query_input(case, side), _query_input(case, "ref")), comparison))
        active = []
        for name, queries, comparison in comparisons:
            current_comparison = comparison
            discovery = comparison["discovery"]
            _measure(db, case, baseline, settings, rng, settings["discovery_warmups"], discovery["warmups"], queries=queries)
            _measure(db, case, baseline, settings, rng, settings["discovery_pairs"], discovery["samples"], queries=queries)
            samples = discovery["samples"]
            slow = "a" if median(sample["a_ms"] - sample["b_ms"] for sample in samples) > 0 else "b"
            discovery["summary"] = summarize(samples, slow)
            reference_comparison = comparison["pair_kind"] == "reference"
            if (not reference_comparison or slow == "a") and (discovery["summary"]["median_ratio"] >= settings["screen_ratio"] and
                    discovery["summary"]["direction_consistency"] >= settings["direction_consistency"]):
                active.append((name, queries, comparison, slow, [], []))
            else:
                comparison["status"] = "no_candidate"
                if reference_comparison and slow != "a":
                    comparison["reference_direction"] = "reference_not_faster"
        current_comparison = None
        if "reference_direction" in ab:
            report["reference_direction"] = ab["reference_direction"]
        if not active:
            report.update(status="no_candidate", reason="discovery_gate_not_met")
            return report
        db.close()
        db = None
        for number in range(settings["confirmation_sessions"]):
            phase = f"confirmation_{number + 1}_setup"
            db = make_database(database, settings)
            _bind_outputs(db, case)
            _restore_state(db, case, fingerprint, state_cache, force=True)
            fresh_baseline, _ = check_case(db, case, settings)
            if fresh_baseline != baseline:
                raise CaseFailure("result_mismatch", "result_changed_after_state_restore")
            if hasattr(db, "statistics_evidence"):
                report.setdefault("statistics_evidence", []).append({"phase": phase, "rows": db.statistics_evidence})
            _remember_state(db, fingerprint, state_cache)
            phase = f"confirmation_{number + 1}"
            rng = random.Random(seed + 104729 * (number + 1))
            for name, queries, comparison, slow, pooled, noise_floors in active:
                current_comparison = comparison
                session = {"session": number + 1, "warmups": [], "samples": [], "control_a": [], "control_b": []}
                comparison["confirmation_sessions"].append(session)
                _measure(db, case, baseline, settings, rng, settings["confirmation_warmups"], session["warmups"], queries=queries)
                _measure(db, case, baseline, settings, rng, settings["confirmation_pairs"], session["samples"], queries=queries)
                for side, query in zip(("a", "b"), queries):
                    _measure(db, case, baseline, settings, rng, settings["control_pairs"], session[f"control_{side}"], queries=(query, query))
                session["summary"] = summarize(session["samples"], slow)
                session["noise_p99_ms"] = max(_quantile([abs(sample["a_ms"] - sample["b_ms"]) for sample in session[f"control_{side}"]], 0.99) for side in ("a", "b"))
                noise_floors.append(session["noise_p99_ms"])
                pooled.extend(session["samples"])
            current_comparison = None
            if number < settings["confirmation_sessions"] - 1:
                db.close()
                db = None
        candidates = []
        for name, queries, comparison, slow, pooled, noise_floors in active:
            summary = summarize(pooled, slow, bootstrap_samples=settings["bootstrap_samples"], seed=seed)
            summary["noise_p99_ms"] = max(noise_floors)
            comparison["summary"] = summary
            passed = _passed(summary, comparison["confirmation_sessions"], settings)
            comparison["status"] = "timing_observation" if passed and traversal_mode else "candidate" if passed else "not_confirmed"
            if passed:
                candidates.append((name, summary))
        report.update(status="timing_observation" if candidates and traversal_mode else "candidate" if candidates else "not_confirmed",
                      reason="explicit_traversal_work_difference" if candidates and traversal_mode else "performance_difference_confirmed" if candidates else "confirmation_gate_not_met")
        if candidates:
            report["candidate_comparison"], report["summary"] = max(candidates, key=lambda item: item[1]["median_ratio"])
        else:
            report["summary"] = ab["summary"] or max((comparison["summary"] for _, _, comparison, *_ in active), key=lambda item: item["median_ratio"])
    except CaseFailure as exc:
        comparison_failure_status = exc.status
        report.update(status=exc.status, reason=exc.reason, stop_target=exc.stop_target, failure_phase=phase)
        report.update(exc.details)
        reference = getattr(db, "_reference_evidence", None)
        if not traversal_mode and exc.status == "timeout" and reference and reference["validated"]:
            lower = exc.details.get("elapsed_lower_bound_ms")
            timed_query = exc.details.get("timed_out_query")
            if (timed_query in (case.get("query_a"), case.get("query_b")) and
                    timed_query != getattr(db, "_reference_query", None) and lower is not None and
                    lower / reference["elapsed_ms"] >= settings["ratio"]):
                report.update(status="timeout_candidate", reason="tested_query_timed_out_with_fast_reference",
                              reference_elapsed_ms=reference["elapsed_ms"], ratio_lower_bound=lower / reference["elapsed_ms"],
                              timed_out_side="a" if timed_query == case.get("query_a") else "b")
                if case.get("query_ref"):
                    # A fast independent Qref says nothing about Q/Qsym's
                    # timing gap when the counterpart has not completed.
                    name = f"query_{report['timed_out_side']}_vs_ref"
                    comparison = report["reference_comparisons"].setdefault(name, {
                        "pair_kind": "reference", "status": "started", "summary": None,
                        "discovery": {"warmups": [], "samples": []}, "confirmation_sessions": []})
                    ab.update(status="timeout", failure_phase=phase)
                else:
                    # check_case's only other registered reference is query_b.
                    # Keep SCor's AB pair labelled reference, never symmetry.
                    name, comparison = "ab", ab
                comparison.update(status="timeout_candidate", failure_phase=phase,
                                  reference_elapsed_ms=reference["elapsed_ms"], ratio_lower_bound=lower / reference["elapsed_ms"],
                                  elapsed_lower_bound_ms=lower, cancellation_verified=exc.details.get("cancellation_verified", False))
                report["candidate_comparison"] = name
    except (ValueError, KeyError, TypeError) as exc:
        report.update(status="invalid_case" if phase == "configuration" else "environment_error", reason=type(exc).__name__, stop_target=phase != "configuration", failure_phase=phase)
    except Exception as exc:
        report.update(status="environment_error", reason=type(exc).__name__, stop_target=True, failure_phase=phase)
    finally:
        if (db is not None and report["status"] in {"candidate", "no_candidate", "not_confirmed", "timing_observation"}
                and (report.get("measurement", {}).get("collect_plans") or report["status"] in {"candidate", "timing_observation"})):
            _collect_plans(db, case, report)
        if (current_comparison is not None and report["status"] not in {"candidate", "no_candidate", "not_confirmed", "timing_observation"}
                and current_comparison["status"] != "timeout_candidate"):
            current_comparison.update(status=comparison_failure_status or report["status"], failure_phase=phase)
        elif ab["status"] == "started" and report["status"] not in {"candidate", "no_candidate", "not_confirmed", "timing_observation"}:
            ab.update(status=comparison_failure_status or report["status"], failure_phase=phase)
        # Reference raw samples remain in their legacy field. Include compact
        # independent results in the authoritative comparison map without
        # serializing those same samples twice.
        for name, comparison in report["reference_comparisons"].items():
            report["comparisons"][name] = {key: comparison[key] for key in ("pair_kind", "status", "summary")}
            for key in ("ratio_lower_bound", "reference_elapsed_ms", "elapsed_lower_bound_ms", "cancellation_verified", "failure_phase"):
                if key in comparison:
                    report["comparisons"][name][key] = comparison[key]
        if state_cache is not None and (report.get("stop_target") or report["status"] not in {"candidate", "no_candidate", "not_confirmed", "timing_observation"}):
            state_cache.clear()
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                report.update(status="environment_error", reason=f"close_failed:{type(exc).__name__}", stop_target=True)
                if state_cache is not None:
                    state_cache.clear()
    return report




import base64
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from io import BytesIO
from collections import defaultdict
import json
import math
import re
import socket
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit



def _native_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise CaseFailure("unsupported_feature", "unsupported_schema_identifier")
    return value


def _recipe(state):
    from state import GraphSpec
    if "graph_spec" not in state:
        raise CaseFailure("unsupported_feature", "structured_graph_recipe_required")
    spec = GraphSpec.from_dict(state["graph_spec"])
    # AGE labels and ArcadeDB record types are not Neo4j multi-label sets.
    if spec.pattern == "P7":
        raise CaseFailure("unsupported_feature", "multi_label_conjunction_not_supported")
    return spec


def _verify_rendered_checks(db, spec):
    from state import render_state_checks
    evidence = []
    for number, check in enumerate(render_state_checks(spec.state_checks())):
        if hasattr(db, "_columns"):
            db._columns[check["query"]] = tuple(check["expected_rows"][0])
        actual = db.execute(check["query"], {}, db.settings["timeout_seconds"])
        if not actual["complete"] or typed_bag(actual["rows"]) != typed_bag(check["expected_rows"]):
            raise CaseFailure("cost_check_failed", f"structured_cost_check_{number}")
        evidence.append({"query": check["query"], "rows": actual["rows"]})
    return evidence


def _cypher_data(value):
    """GraphSpec data has only integer/boolean/null properties and collections."""
    if value is None:
        return "null"
    if type(value) is bool:
        return str(value).lower()
    if type(value) is int:
        return str(value)
    if type(value) is list:
        return "[" + ", ".join(_cypher_data(item) for item in value) + "]"
    if type(value) is dict:
        return "{" + ", ".join(f"{_native_identifier(key)}: {_cypher_data(item)}" for key, item in value.items()) + "}"
    raise CaseFailure("unsupported_feature", "unsupported_graph_property_type")




class ArcadeDB:
    """Native HTTP Cypher reads and typed JSONL graph loading."""

    def __init__(self, config, settings):
        self.database, self.settings = _target_name(config), settings
        self.uri = config["uri"].rstrip("/")
        parsed = urlsplit(self.uri)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise CaseFailure("configuration_error", "arcadedb_http_uri_required", stop_target=True)
        self._authorization = "Basic " + base64.b64encode(
            f"{config['user']}:{_password(config)}".encode()).decode()
        connection = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        self._connection = connection(parsed.hostname, parsed.port, timeout=settings["timeout_seconds"])
        self._base_path = parsed.path.rstrip("/")
        self._spec = None
        try:
            info = self._request("/api/v1/server?mode=basic", None, settings["timeout_seconds"])
            if not isinstance(info.get("version"), str) or not info["version"]:
                raise CaseFailure("environment_error", "arcadedb_version_unavailable", stop_target=True)
            self.version = "ArcadeDB/" + info["version"]
            probe = self.execute("RETURN 1 AS connected", {}, settings["timeout_seconds"])
            if not probe["complete"] or probe["rows"] != [{"connected": 1}]:
                raise CaseFailure("environment_error", "arcadedb_connection_probe_failed", stop_target=True)
        except Exception:
            self.close()
            raise


    def _request(self, path, payload, timeout, *, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else (None if payload is None else json.dumps(payload, allow_nan=False).encode())
        headers = {"Authorization": self._authorization, "Content-Type": content_type,
                   "Accept": "application/json"}
        try:
            self._connection.timeout = timeout
            if self._connection.sock is None:
                self._connection.connect()
            else:
                self._connection.sock.settimeout(timeout)
            # TCP/TLS establishment is outside the query timer. A server that
            # closes an idle socket may fail this request; never retry an
            # already submitted statement whose execution state is unknown.
            started = perf_counter()
            self._connection.request("GET" if data is None else "POST", self._base_path + path,
                                     body=data, headers=headers)
            response = self._connection.getresponse()
            body = response.read()
            if response.status >= 300:
                # http.client never follows redirects. Preserve the existing
                # HTTP status classification without forwarding credentials.
                raise HTTPError(self.uri + path, response.status, response.reason,
                                response.headers, BytesIO(body))
            result = json.loads(body)
            elapsed = (perf_counter() - started) * 1000
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:1000]
            if "timeout" in message.lower() or "timed out" in message.lower():
                raise CaseFailure("timeout", "arcadedb_server_timeout", stop_target=True,
                                  details={"cancellation_verified": False, "elapsed_lower_bound_ms": None}) from exc
            status = "configuration_error" if exc.code in {401, 403} else (
                "unsupported_feature" if _syntax_error("", message) or re.search(r"parse|not supported|unsupported|syntax", message, re.I) else "query_error")
            raise CaseFailure(status, f"arcadedb_http_{exc.code}", stop_target=status == "configuration_error",
                              details={"server_message": message}) from exc
        except (TimeoutError, socket.timeout) as exc:
            self._connection.close()
            raise CaseFailure("timeout", "arcadedb_client_timeout", stop_target=True,
                              details={"cancellation_verified": False, "elapsed_lower_bound_ms": None}) from exc
        except (OSError, HTTPException) as exc:
            self._connection.close()
            raise CaseFailure("environment_error", "arcadedb_transport_error", stop_target=True) from exc
        except (ValueError, TypeError) as exc:
            self._connection.close()
            raise CaseFailure("environment_error", "arcadedb_invalid_json", stop_target=True) from exc
        if not isinstance(result, dict) or result.get("error"):
            raise CaseFailure("query_error", "arcadedb_invalid_response", stop_target=True)
        result["_client_elapsed_ms"] = elapsed
        return result


    def _query(self, text, parameters, timeout, *, language="opencypher", command=False):
        endpoint = "command" if command else "query"
        return self._request(f"/api/v1/{endpoint}/{quote(self.database, safe='')}", {
            "language": language, "command": text, "params": parameters,
            "limit": self.settings["max_rows"] + 1, "serializer": "record"}, timeout)

    def _sql(self, text, parameters=None, *, command=False):
        result = self._query(text, parameters or {}, self.settings["timeout_seconds"], language="sql", command=command)
        rows = result.get("result")
        if not isinstance(rows, list) or result.get("truncated") or len(rows) > self.settings["max_rows"]:
            raise CaseFailure("environment_error", "arcadedb_native_result_incomplete", stop_target=True)
        return rows

    def execute(self, query, parameters, timeout):
        result = self._query(query, parameters, timeout)
        rows = result.get("result")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CaseFailure("incomplete_result", "arcadedb_invalid_result_rows")
        complete = len(rows) <= self.settings["max_rows"] and result.get("truncated") is not True
        if "limit" in result and result["limit"] > 0 and result["limit"] < self.settings["max_rows"] + 1:
            raise CaseFailure("configuration_error", "arcadedb_server_result_cap_too_small", stop_target=True)
        return {"rows": rows[:self.settings["max_rows"]], "complete": complete,
                "elapsed_ms": result["_client_elapsed_ms"]}


    def explain(self, query, parameters, analyze=True):
        result = self._query(("PROFILE " if analyze else "EXPLAIN ") + query,
                             parameters, self.settings["timeout_seconds"])
        fields = {key: result[key] for key in ("explain", "explainPlan", "profile") if key in result}
        if not fields:
            # Some Cypher versions return a plan as a result row.
            rows = result.get("result", [])
            if rows and any(key.lower() in {"plan", "executionplan", "profile"} for key in rows[0]):
                fields = {"result": [rows[0]]}
        if not fields:
            raise CaseFailure("unsupported_feature", "arcadedb_execution_plan_unavailable")
        return {"format": "arcadedb_native", "analyze": analyze, "plan": fields}

    def health(self):
        try:
            result = self.execute("RETURN 1 AS connected", {}, self.settings["timeout_seconds"])
            return result["complete"] and result["rows"] == [{"connected": 1}]
        except Exception:
            return False

    def _batch(self, records):
        payload = "".join(json.dumps(record, allow_nan=False) + "\n" for record in records).encode()
        return self._request(f"/api/v1/batch/{quote(self.database, safe='')}?wal=true&lightEdges=false",
                             payload, self.settings["timeout_seconds"], content_type="application/x-ndjson")

    def restore(self, state):
        spec = _recipe(state)
        schema = spec.schema()
        current = self._sql("SELECT name, type FROM schema:types")
        if any(row.get("type") not in {"vertex", "edge"} for row in current):
            raise CaseFailure("environment_error", "unexpected_schema_in_test_database", stop_target=True)
        self._sql("DROP INDEX gsp_cost_lookup IF EXISTS", command=True)
        # All records belong to the explicitly resettable test database. Drop
        # edge types first; no connectivity is retained across graph states.
        for row in sorted(current, key=lambda row: row["type"] != "edge"):
            self._sql(f"DROP TYPE {_native_identifier(row['name'])} UNSAFE", command=True)
        for kind, entries in (("VERTEX", schema["nodes"]), ("EDGE", schema["relationships"])):
            for entry in entries:
                name = _native_identifier(entry["label" if kind == "VERTEX" else "type"])
                self._sql(f"CREATE {kind} TYPE {name}", command=True)
                for key, type_name in entry["properties"].items():
                    native_type = {"integer": "LONG", "boolean": "BOOLEAN"}[type_name]
                    self._sql(f"CREATE PROPERTY {name}.{_native_identifier(key)} {native_type}", command=True)
        identities = {}
        for batch in spec.iter_batches("nodes", 1000):
            records = [{"@type": "vertex", "@class": row["labels"][0],
                        "@id": f"{row['labels'][0]}:{row['uid']}", **row["properties"]} for row in batch]
            if any(len(row["labels"]) != 1 for row in batch):
                raise CaseFailure("unsupported_feature", "multi_label_conjunction_not_supported")
            loaded = self._batch(records)
            mapping = loaded.get("idMapping", {})
            if (loaded.get("verticesCreated") != len(records) or loaded.get("edgesCreated") != 0
                    or set(mapping) != {row["@id"] for row in records}
                    or any(not isinstance(rid, str) or not re.fullmatch(r"#\d+:\d+", rid) for rid in mapping.values())):
                raise CaseFailure("environment_error", "arcadedb_vertex_batch_incomplete", stop_target=True)
            identities.update(mapping)
        for batch in spec.iter_batches("edges", 1000):
            records = [{"@type": "edge", "@class": row["type"],
                        "@from": identities[f"{row['source_label']}:{row['source']}"],
                        "@to": identities[f"{row['target_label']}:{row['target']}"],
                        **row["properties"]} for row in batch]
            loaded = self._batch(records)
            if loaded.get("edgesCreated") != len(records) or loaded.get("verticesCreated") != 0:
                raise CaseFailure("environment_error", "arcadedb_edge_batch_incomplete", stop_target=True)
        for index in spec.indexes:
            self._sql(f"CREATE INDEX {_native_identifier(index['name'])} ON "
                      f"{_native_identifier(index['label'])} ({_native_identifier(index['property'])}) NOTUNIQUE", command=True)
        # Schema DDL resets this database's plans; counts come from the new
        # physical types. No nonexistent ANALYZE/cache procedure is invented.
        self._spec = spec
        self._check_indexes(spec)

    def _check_indexes(self, spec):
        types = self._sql("SELECT name, indexes FROM schema:types")
        indexes = [index for row in types for index in row.get("indexes", [])]
        physical = self._sql("SELECT typeName, properties, valid FROM schema:indexes")
        for required in spec.indexes:
            if not any(row.get("name") == required["name"] and row.get("typeName") == required["label"]
                       and row.get("properties") == [required["property"]] for row in indexes):
                raise CaseFailure("cost_check_failed", "arcadedb_cost_index_missing")
            matches = [row for row in physical if row.get("typeName") == required["label"]
                       and row.get("properties") in ([required["property"]], [[required["property"]]])]
            if not matches or any(row.get("valid") is not True for row in matches):
                raise CaseFailure("cost_check_failed", "arcadedb_cost_index_invalid")
        return {"kind": "LSM_TREE_property", "includes_whole_property_map": False,
                "indexes": indexes, "physical_indexes": physical}

    def check_state(self, state):
        spec = _recipe(state)
        return {"indexes": self._check_indexes(spec), "checks": _verify_rendered_checks(self, spec),
                "preparation": "fresh_schema_and_native_indexes"}

    def close(self):
        self._connection.close()



def _agtype_value(value):
    """Decode only AGE's scalar/list/map fragment; never erase entity types."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise CaseFailure("incomplete_result", "unexpected_age_driver_value")
    try:
        parsed = json.loads(value, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
        # Graph entities and numeric annotations are deliberately unsupported;
        # accepting their JSON prefix would silently discard semantic types.
        typed_bag([{ "value": parsed }])
        return parsed
    except (ValueError, TypeError) as exc:
        raise CaseFailure("incomplete_result", "unsupported_agtype_value") from exc


class ApacheAGE:
    """One psycopg session; native graph labels, property indexes and plans."""

    def __init__(self, config, settings):
        self.database, self.settings = _target_name(config), settings
        self.graph = _native_identifier(config.get("graph_name", "gsp_cases"))
        from psycopg import connect, sql
        self._sql_module = sql
        self.connection = connect(config["uri"], dbname=self.database,
                                  user=config["user"], password=_password(config), autocommit=True,
                                  prepare_threshold=None,
                                  connect_timeout=max(1, math.ceil(settings["timeout_seconds"])))
        self._columns, self._prepared, self._spec = {}, {}, None
        try:
            self._sql("LOAD 'age'")
            self._sql('SET search_path = ag_catalog, "$user", public')
            rows = self._sql("SELECT extversion FROM pg_extension WHERE extname = 'age'")
            if len(rows) != 1:
                raise CaseFailure("environment_error", "age_extension_unavailable", stop_target=True)
            pg = self._sql("SHOW server_version")[0][0]
            self.version = f"Apache AGE/{rows[0][0]} PostgreSQL/{pg}"
            self._sql("SELECT 1")
        except Exception:
            self.close()
            raise

    def _sql(self, statement, parameters=None):
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall() if cursor.description else []

    def _signature(self, query):
        if query not in self._columns:
            from query import parse_query, projection_names
            try:
                self._columns[query] = projection_names(parse_query(query))
            except ValueError as exc:
                raise CaseFailure("unsupported_feature", "age_requires_known_output_columns") from exc
        return self._columns[query]

    def _cypher_sql(self, query, columns, *, parameters=False):
        sql = self._sql_module
        signature = sql.SQL(", ").join(sql.SQL("{} agtype").format(sql.Identifier(name)) for name in columns)
        if not columns:
            signature = sql.SQL("unused agtype")
        # AGE requires its second argument to be dollar-quoted, rather than
        # just any SQL string constant. Choose a delimiter absent from the
        # complete input, so embedded quotes/SQL cannot escape this literal.
        tag = "$gsp$"
        while tag in query:
            tag = tag[:-1] + "_" + "$"
        text = sql.SQL(tag + query + tag)
        return sql.SQL("SELECT * FROM ag_catalog.cypher({}, {}{}) AS ({})").format(
            sql.Literal(self.graph), text, sql.SQL(", $1") if parameters else sql.SQL(""), signature)

    def _statement(self, query, parameters, columns):
        sql = self._sql_module
        if not parameters:
            return self._cypher_sql(query, columns)
        key = (query, columns)
        if key not in self._prepared:
            name = "gsp_query_" + str(len(self._prepared))
            self._sql(sql.SQL("PREPARE {} (agtype) AS {}").format(
                sql.Identifier(name), self._cypher_sql(query, columns, parameters=True)))
            self._prepared[key] = name
        return sql.SQL("EXECUTE {} ({})").format(
            sql.Identifier(self._prepared[key]), sql.Literal(json.dumps(parameters, allow_nan=False)))

    def _run(self, query, parameters, timeout, columns, *, decode=True, prefix=None):
        sql = self._sql_module
        self._sql(sql.SQL("SET statement_timeout = {}").format(sql.Literal(max(1, math.ceil(timeout * 1000)))))
        started = perf_counter()
        try:
            statement = self._statement(query, parameters, columns)
            if prefix:
                statement = sql.SQL(prefix + " ") + statement
            started = perf_counter()
            with self.connection.cursor() as cursor:
                cursor.execute(statement)
                raw = cursor.fetchmany(self.settings["max_rows"] + 1)
                if decode and any(len(row) != len(columns) for row in raw):
                    raise CaseFailure("incomplete_result", "age_result_column_width_mismatch")
                rows = ([dict(zip(columns, (_agtype_value(value) for value in row))) for row in raw]
                        if decode else raw)
            elapsed = (perf_counter() - started) * 1000
        except CaseFailure:
            raise
        except Exception as exc:
            identity = getattr(exc, "sqlstate", None) or type(exc).__name__
            message = str(exc)[:1000]
            if identity == "57014":
                # PostgreSQL has acknowledged cancellation. Autocommit leaves
                # no failed transaction; require a subsequent successful query.
                healthy = self.health()
                elapsed = (perf_counter() - started) * 1000
                raise CaseFailure("timeout", "age_statement_timeout", stop_target=not healthy, details={
                    "cancellation_verified": healthy,
                    "elapsed_lower_bound_ms": min(timeout * 1000, elapsed)}) from exc
            if identity in {"42601", "0A000", "42883"} or _syntax_error("", message):
                raise CaseFailure("unsupported_feature", identity, details={"server_message": message}) from exc
            if str(identity).startswith("08") or getattr(self.connection, "closed", False):
                raise CaseFailure("environment_error", identity, stop_target=True) from exc
            raise CaseFailure("query_error", identity, details={"server_message": message}) from exc
        return {"rows": rows[:self.settings["max_rows"]], "complete": len(raw) <= self.settings["max_rows"],
                "elapsed_ms": elapsed}

    def execute(self, query, parameters, timeout):
        return self._run(query, parameters, timeout, self._signature(query))

    def explain(self, query, parameters, analyze=True):
        prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" if analyze else "EXPLAIN (FORMAT JSON)"
        result = self._run(query, parameters, self.settings["timeout_seconds"], self._signature(query),
                           decode=False, prefix=prefix)
        if not result["complete"] or len(result["rows"]) != 1:
            raise CaseFailure("environment_error", "age_plan_incomplete", stop_target=True)
        return {"format": "postgresql_json", "analyze": analyze, "plan": result["rows"][0][0]}

    def health(self):
        try:
            return self._sql("SELECT 1") == [(1,)]
        except Exception:
            return False

    def _build(self, text):
        # Graph setup is generated from GraphSpec, not accepted as arbitrary
        # imported Cypher. A no-RETURN AGE command has one dummy agtype column.
        self._run(text, {}, self.settings["timeout_seconds"], (), decode=False)

    def restore(self, state):
        spec = _recipe(state)
        sql = self._sql_module
        self._sql("DEALLOCATE ALL")
        self._prepared.clear()
        existing = self._sql("SELECT name FROM ag_catalog.ag_graph WHERE name = %s", (self.graph,))
        if existing:
            self._sql("SELECT ag_catalog.drop_graph(%s, true)", (self.graph,))
        self._sql("SELECT ag_catalog.create_graph(%s)", (self.graph,))
        schema = spec.schema()
        for node in schema["nodes"]:
            self._sql("SELECT ag_catalog.create_vlabel(%s, %s)", (self.graph, node["label"]))
        for edge in schema["relationships"]:
            self._sql("SELECT ag_catalog.create_elabel(%s, %s)", (self.graph, edge["type"]))
        # Temporary GIN indexes make batched UID endpoint lookup practical.
        # Required label GIN indexes remain; all helper indexes are removed
        # before measurements, so noise labels do not gain hidden indexes.
        required = {index["label"]: index for index in spec.indexes}
        helpers = []
        for number, node in enumerate(schema["nodes"]):
            name = required[node["label"]]["name"] if node["label"] in required else f"gsp_load_{number}"
            self._sql(sql.SQL("CREATE INDEX {} ON {}.{} USING gin (properties)").format(
                sql.Identifier(name), sql.Identifier(self.graph), sql.Identifier(node["label"])))
            if node["label"] not in required:
                helpers.append(name)
        for index in spec.indexes:
            # Inline property maps lower to containment (GIN); WHERE property
            # equality lowers to agtype_access_operator (BTree). Both target
            # the same low-cost role, without indexing noise labels.
            self._sql(sql.SQL("CREATE INDEX {} ON {}.{} ((ag_catalog.agtype_access_operator("
                              "VARIADIC ARRAY[properties, {}::ag_catalog.agtype])))").format(
                sql.Identifier(index["name"] + "_scalar"), sql.Identifier(self.graph),
                sql.Identifier(index["label"]), sql.Literal(json.dumps(index["property"]))))
        for batch in spec.iter_batches("nodes", 1000):
            groups = defaultdict(list)
            for row in batch:
                if len(row["labels"]) != 1:
                    raise CaseFailure("unsupported_feature", "multi_label_conjunction_not_supported")
                groups[row["labels"][0]].append(row["properties"])
            for label, rows in groups.items():
                self._build(f"UNWIND {_cypher_data(rows)} AS props CREATE (n:{_native_identifier(label)}) SET n = props")
        for batch in spec.iter_batches("edges", 1000):
            groups = defaultdict(list)
            for row in batch:
                groups[(row["source_label"], row["target_label"], row["type"])].append(
                    {"source": row["source"], "target": row["target"], **row["properties"]})
            for (source, target, kind), rows in groups.items():
                keys = sorted(set().union(*(set(row) - {"source", "target"} for row in rows)))
                properties = " {" + ", ".join(f"{_native_identifier(key)}: row.{key}" for key in keys) + "}" if keys else ""
                self._build(f"UNWIND {_cypher_data(rows)} AS row MATCH "
                            f"(s:{_native_identifier(source)} {{uid: row.source}}), "
                            f"(t:{_native_identifier(target)} {{uid: row.target}}) "
                            f"CREATE (s)-[e:{_native_identifier(kind)}{properties}]->(t)")
        for name in helpers:
            self._sql(sql.SQL("DROP INDEX {}.{}").format(sql.Identifier(self.graph), sql.Identifier(name)))
        for label in [node["label"] for node in schema["nodes"]] + [edge["type"] for edge in schema["relationships"]]:
            # Flush GIN's pending list from graph loading as well as updating
            # planner statistics. With ANALYZE alone, the pending entries made
            # an 8,001-node low-label scan cheaper than the ready GIN lookup.
            self._sql(sql.SQL("VACUUM (ANALYZE) {}.{}").format(sql.Identifier(self.graph), sql.Identifier(label)))
        self._sql("DISCARD PLANS")
        self._spec = spec
        self._check_indexes(spec)

    def _check_indexes(self, spec):
        evidence = []
        for index in spec.indexes:
            for name, kind, token in ((index["name"], "GIN_property_map", "USING gin (properties)"),
                                       (index["name"] + "_scalar", "BTREE_property_expression", "USING btree")):
                rows = self._sql("SELECT i.indisvalid, i.indisready, t.relname, pg_get_indexdef(i.indexrelid) "
                                 "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
                                 "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                                 "WHERE n.nspname = %s AND x.relname = %s", (self.graph, name))
                if len(rows) != 1 or rows[0][0:3] != (True, True, index["label"]) or token not in rows[0][3]:
                    raise CaseFailure("cost_check_failed", "age_cost_index_missing_or_invalid")
                if kind == "BTREE_property_expression" and ("agtype_access_operator" not in rows[0][3]
                                                             or json.dumps(index["property"]) not in rows[0][3]):
                    raise CaseFailure("cost_check_failed", "age_cost_index_wrong_property")
                evidence.append({"name": name, "label": index["label"], "property": index["property"],
                                 "definition": rows[0][3], "kind": kind,
                                 "includes_whole_property_map": kind == "GIN_property_map", "valid": True, "ready": True})
        return evidence

    def check_state(self, state):
        spec = _recipe(state)
        return {"indexes": self._check_indexes(spec), "checks": _verify_rendered_checks(self, spec),
                "preparation": "fresh_graph_vacuum_analyze_discard_plans"}

    def close(self):
        self.connection.close()

class LadybugDB:
    persistent_state = False
    """Embedded typed graph; each rebuild owns only a fresh test subdirectory."""

    def __init__(self, config, settings):
        from pathlib import Path
        import ladybug

        self.database, self.settings = _target_name(config), settings
        directory = config.get("path")
        if not isinstance(directory, str) or not directory.strip() or directory == ":memory:":
            raise CaseFailure("configuration_error", "ladybug_explicit_test_directory_required", stop_target=True)
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._module, self.config = ladybug, config
        self._work, self.db, self.connection = None, None, None
        self.version = "LadybugDB/" + ladybug.Database.get_version()
        self.index_evidence = []
        self._new_database()

    def _new_database(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        self.close()
        self._work = TemporaryDirectory(prefix="gsp-", dir=self.directory)
        try:
            self.db = self._module.Database(str(Path(self._work.name) / "state.lbdb"),
                                           buffer_pool_size=self.config.get("buffer_pool_size", 268435456),
                                           max_num_threads=self.config.get("threads", 1),
                                           backend=self.config.get("backend", "auto"))
            self.connection = self._module.Connection(self.db, num_threads=self.config.get("threads", 1))
            if not self.health():
                raise CaseFailure("environment_error", "ladybug_connection_unhealthy", stop_target=True)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
            raise CaseFailure("invalid_case", "invalid_graph_identifier")
        return "`" + value + "`"

    def execute(self, query, parameters, timeout):
        # The official C-API wrapper can reject this shape without executing it.
        # Such a reply must not become lower-bound performance evidence.
        if (not self.connection._using_pybind_backend() and timeout > 0
                and len(re.findall(r"\bUNWIND\s+RANGE\s*\(", query, re.I)) >= 2):
            raise CaseFailure("unsupported_feature", "ladybug_capi_nested_range_timeout_guard")
        cursor = None
        self.connection.set_query_timeout(max(1, math.ceil(timeout * 1000)))
        started = perf_counter()
        try:
            cursor = self.connection.execute(query, parameters)
            if isinstance(cursor, list):
                for result in cursor:
                    result.close()
                cursor = None
                raise CaseFailure("invalid_case", "one_statement_required")
            names, types = cursor.get_column_names(), cursor.get_column_data_types()
            if len(names) != len(set(names)):
                raise CaseFailure("incomplete_result", "duplicate_output_aliases")
            if any(re.search(r"\b(NODE|REL|RECURSIVE_REL)\b", kind, re.I) for kind in types):
                raise CaseFailure("incomplete_result", "graph_object_projection_requires_scalar_properties")
            rows = []
            while cursor.has_next() and len(rows) < self.settings["max_rows"]:
                values = cursor.get_next()
                if len(values) != len(names):
                    raise CaseFailure("incomplete_result", "invalid_result_row_width")
                rows.append(dict(zip(names, values)))
            complete = not cursor.has_next()
            elapsed = (perf_counter() - started) * 1000
            return {"rows": rows, "complete": complete, "elapsed_ms": elapsed}
        except CaseFailure:
            raise
        except RuntimeError as exc:
            message = str(exc)
            if re.search(r"\binterrupted\b|timed?\s*out|timeout", message, re.I):
                self.connection.interrupt()
                healthy = self.health()
                elapsed = (perf_counter() - started) * 1000
                raise CaseFailure("timeout", "ladybug_query_interrupted", stop_target=not healthy, details={
                    "cancellation_verified": healthy, "server_message": message[:1000],
                    "elapsed_lower_bound_ms": min(timeout * 1000, elapsed) if healthy else None}) from exc
            status = "unsupported_feature" if re.search(r"Parser exception|not (?:yet )?implemented|not supported", message, re.I) else "query_error"
            raise CaseFailure(status, "ladybug_query_error", details={"server_message": message[:1000]}) from exc
        finally:
            if cursor is not None:
                cursor.close()

    def health(self):
        cursor = None
        try:
            self.connection.set_query_timeout(max(1, math.ceil(self.settings["timeout_seconds"] * 1000)))
            cursor = self.connection.execute("RETURN 1 AS connected")
            return (not isinstance(cursor, list) and cursor.get_next() == [1] and not cursor.has_next())
        except Exception:
            return False
        finally:
            if cursor is not None and not isinstance(cursor, list):
                cursor.close()

    def restore(self, case):
        import csv
        from contextlib import ExitStack
        from pathlib import Path
        from state import GraphSpec

        if "graph_spec" not in case:
            raise CaseFailure("unsupported_feature", "ladybug_restore_requires_graph_spec")
        spec = GraphSpec.from_dict(case["graph_spec"])
        if spec.pattern == "P7":
            raise CaseFailure("unsupported_feature", "ladybug_has_single_label_nodes")
        schema = spec.schema()
        node_tables = {table["label"]: table for table in schema["nodes"]}
        edge_tables = {table["type"]: table for table in schema["relationships"]}
        primary = {label: "uid" for label in node_tables}
        for index in spec.indexes:
            if primary[index["label"]] != "uid" and primary[index["label"]] != index["property"]:
                raise CaseFailure("unsupported_feature", "ladybug_one_primary_index_per_table")
            primary[index["label"]] = index["property"]
        sql_types = {"integer": "INT64", "boolean": "BOOLEAN"}
        timeout = self.settings["timeout_seconds"]
        self._new_database()
        directory = Path(self._work.name)
        for label, table in node_tables.items():
            columns = [self._identifier(key) + " " + sql_types[kind] + (" PRIMARY KEY" if key == primary[label] else "")
                       for key, kind in table["properties"].items()]
            self.execute(f"CREATE NODE TABLE {self._identifier(label)}({', '.join(columns)})", {}, timeout)
        for kind, table in edge_tables.items():
            columns = [f"FROM {self._identifier(pair['source_label'])} TO {self._identifier(pair['target_label'])}"
                       for pair in table["endpoints"]]
            columns.extend(self._identifier(key) + " " + sql_types[value] for key, value in table["properties"].items())
            self.execute(f"CREATE REL TABLE {self._identifier(kind)}({', '.join(columns)})", {}, timeout)

        # COPY expects endpoint primary keys. Keep an identity map only where
        # that key differs from uid; uid remains scoped by its node table.
        foreign_keys, files = {}, {}
        def cell(value):
            return str(value).lower() if type(value) is bool else value
        with ExitStack() as stack:
            writers = {}
            def writer(key):
                if key not in writers:
                    path = directory / ("import-" + str(len(files)) + ".csv")
                    files[key] = path
                    handle = stack.enter_context(path.open("w", newline="", encoding="utf-8"))
                    writers[key] = csv.writer(handle)
                return writers[key]
            for batch in spec.iter_batches("nodes"):
                for node in batch:
                    if len(node["labels"]) != 1:
                        raise CaseFailure("unsupported_feature", "ladybug_has_single_label_nodes")
                    label, props = node["labels"][0], node["properties"]
                    if primary[label] != "uid":
                        foreign_keys[label, node["uid"]] = props[primary[label]]
                    writer(("node", label)).writerow([cell(props.get(key)) for key in node_tables[label]["properties"]])
            for batch in spec.iter_batches("edges"):
                for edge in batch:
                    source, target = edge["source_label"], edge["target_label"]
                    source_key = foreign_keys[source, edge["source"]] if primary[source] != "uid" else edge["source"]
                    target_key = foreign_keys[target, edge["target"]] if primary[target] != "uid" else edge["target"]
                    writer(("edge", edge["type"], source, target)).writerow([source_key, target_key] +
                        [cell(edge["properties"].get(key)) for key in edge_tables[edge["type"]]["properties"]])
        for key, path in files.items():
            literal = "'" + str(path).replace("\\", "\\\\").replace("'", "\\'") + "'"
            options = "header=false, auto_detect=false, ignore_errors=false"
            if key[0] == "edge":
                options += f", from='{key[2]}', to='{key[3]}'"
            self.execute(f"COPY {self._identifier(key[1])} FROM {literal} ({options})", {}, timeout)
            path.unlink()
        self.index_evidence = []
        for index in spec.indexes:
            result = self.execute(f"CALL table_info('{index['label']}') RETURN *", {}, timeout)
            key = index["property"]
            if not result["complete"] or not any(row.get("name") == key and row.get("primary key") is True for row in result["rows"]):
                raise CaseFailure("environment_error", "ladybug_primary_index_not_operational", stop_target=True)
            self.index_evidence.append({**index, "kind": "primary_key_hash", "verified": True})

    def explain(self, query, parameters, analyze=True):
        result = self.execute(("PROFILE " if analyze else "EXPLAIN ") + query, parameters, self.settings["timeout_seconds"])
        return {"dbms": "ladybugdb", "format": "native", "analyze": analyze,
                "rows": result["rows"], "complete": result["complete"]}

    def check_state(self, state):
        spec = _recipe(state)
        return _verify_rendered_checks(self, spec)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._work is not None:
            self._work.cleanup()
            self._work = None


"""Gremlin HTTP adapters for JanusGraph and Apache HugeGraph.

Sources: TinkerPop reference #connecting-via-http; JanusGraph index-management;
HugeGraph Gremlin API and native SchemaManager (version-tag source).
"""

from http.client import HTTPConnection, HTTPSConnection, HTTPException
from time import perf_counter
from urllib.parse import urlsplit
import base64
import json
import math
import re
import socket

def _graphson_value(value):
    """Decode the scalar/list/map fragment, never stringify graph entities."""
    if type(value) in (str, bool, int) or value is None:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_graphson_value(item) for item in value]
    if type(value) is not dict:
        raise CaseFailure("incomplete_result", "unsupported_graphson_value")
    if set(value) == {"@type", "@value"}:
        kind, raw = value["@type"], value["@value"]
        if kind in {"g:Int32", "g:Int64", "gx:BigInteger"} and type(raw) is int:
            return raw
        if kind in {"g:Float", "g:Double"} and type(raw) in (int, float) and math.isfinite(raw):
            return float(raw)
        if kind == "g:List" and type(raw) is list:
            return [_graphson_value(item) for item in raw]
        if kind == "g:Map" and type(raw) is list and len(raw) % 2 == 0:
            result = {}
            for key, item in zip(raw[::2], raw[1::2]):
                key = _graphson_value(key)
                if not isinstance(key, str) or key in result:
                    raise CaseFailure("incomplete_result", "graphson_map_keys_not_unique_strings")
                result[key] = _graphson_value(item)
            return result
        raise CaseFailure("incomplete_result", f"unsupported_graphson_type:{kind}")
    if not all(type(key) is str for key in value):
        raise CaseFailure("incomplete_result", "graphson_map_keys_not_strings")
    return {key: _graphson_value(item) for key, item in value.items()}


def _gremlin_data(envelope, *, require_list=True, max_items=None):
    """Return decoded items; expand only top-level traversal bulk."""
    data = envelope.get("result", {}).get("data")
    if isinstance(data, dict) and data.get("@type") == "g:List":
        data = data.get("@value")
    if type(data) is not list:
        if require_list:
            raise CaseFailure("incomplete_result", "gremlin_response_data_not_a_list")
        data = [data]
    items = []
    for value in data:
        if isinstance(value, dict) and value.get("@type") == "g:Traverser":
            raw = value.get("@value", {})
            bulk = _graphson_value(raw.get("bulk"))
            if type(bulk) is not int or not 0 < bulk <= 10_000_001:
                raise CaseFailure("incomplete_result", "invalid_gremlin_bulk")
            items.extend([_graphson_value(raw.get("value"))] * (min(bulk, max_items - len(items)) if max_items else bulk))
        else:
            items.append(_graphson_value(value))
        if max_items and len(items) >= max_items:
            break
    return items


def _g_literal(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _g_properties(prefix, properties):
    for key, value in properties.items():
        _native_identifier(key)
        prefix += (f".hasNot({_g_literal(key)})" if value is None else
                   f".has({_g_literal(key)}, {_g_literal(value)})")
    return prefix


def _gremlin_state_checks(checks, *, scan_edges=False):
    """Render the finite structural vocabulary directly as Gremlin."""
    result = []
    def edge_start(label):
        if scan_edges:
            # HugeGraph's label index materializes batches of edge IDs and
            # serializes the full IdQuery as an off-heap cache key. The key
            # has a 16 KiB limit. Untimed population checks can scan instead,
            # keeping experiment traversals and physical indexes unchanged.
            return f"g.E().filter{{ t -> t.get().label() == {_g_literal(label)} }}"
        return f"g.E().hasLabel({_g_literal(label)})"

    def count_query(text, expected):
        result.append({"query": text + '.count().project("count").by(__.identity())',
                       "expected_rows": [{"count": expected}]})
    for check in checks:
        kind = check["kind"]
        if kind == "node_count":
            query = "g.V()"
            for label in check["labels"]:
                query += f".hasLabel({_g_literal(label)})"
            count_query(_g_properties(query, check["properties"]), check["expected"])
        elif kind == "edge_count":
            query = edge_start(check["type"])
            for side, step in (("source", "outV"), ("target", "inV")):
                condition = f"__.{step}()"
                if check.get(side + "_label"):
                    condition += f".hasLabel({_g_literal(check[side + '_label'])})"
                condition = _g_properties(condition, check.get(side + "_properties", {}))
                if condition != f"__.{step}()":
                    query += f".filter({condition})"
            count_query(query, check["expected"])
        elif kind == "node_properties":
            query = f"g.V().hasLabel({_g_literal(check['label'])}).has(\"uid\", {check['uid']})"
            keys = list(check["properties"])
            query += ".project(" + ", ".join(_g_literal(key) for key in keys) + ")"
            for key in keys:
                query += f".by(__.coalesce(__.values({_g_literal(key)}), __.constant(null)))"
            result.append({"query": query, "expected_rows": [check["properties"]]})
        elif kind == "distinct_values":
            query = (f"g.V().hasLabel({_g_literal(check['label'])})" if "label" in check else
                     edge_start(check["type"]))
            count_query(query + f".values({_g_literal(check['property'])}).dedup()", check["expected"])
        elif kind == "inverse_edges":
            key = _g_literal(check["pair_key"])
            query = (edge_start(check["forward"]) + ".filter{ t -> def f=t.get(); "
                     f"g.V(f.inVertex().id()).outE({_g_literal(check['reverse'])})"
                     ".filter{ b -> b.get().inVertex().id() == f.outVertex().id() }"
                     f".has({key}, f.value({key})).count().next() != 1 }}")
            count_query(query, check["expected"])
        elif kind == "reference_edges":
            for first, second, guarded in ((check["reference"], check["original"], False),
                                           (check["original"], check["reference"], True)):
                source = f"__.outV().hasLabel({_g_literal(check['source_label'])})"
                if guarded:
                    source += (f".has({_g_literal(check['guard_property'])}, true)" if check["guard_value"] else
                               f".not(__.has({_g_literal(check['guard_property'])}, true))")
                query = (edge_start(first) + f".filter({source})"
                         f".filter(__.inV().hasLabel({_g_literal(check['target_label'])}))"
                         f".filter{{ t -> def e=t.get(); g.V(e.outVertex().id()).outE({_g_literal(second)})"
                         ".filter{ other -> other.get().inVertex().id() == e.inVertex().id() }.count().next() == 0 }")
                count_query(query, check["expected"])
        elif kind == "literal":
            result.append({"query": f"g.inject({_g_literal(check['value'])}).project(\"value\").by(__.identity())",
                           "expected_rows": [{"value": check["expected"]}]})
        else:
            raise CaseFailure("unsupported_feature", f"unsupported_gremlin_state_check:{kind}")
    return result


class GremlinHTTP:
    """One persistent HTTP connection; generated traversal results only."""
    backend = "gremlin"
    mime = "application/vnd.gremlin-v3.0+json"

    def __init__(self, config, settings):
        self.database, self.settings = _target_name(config), settings
        self.config = config
        parsed = urlsplit(config["uri"])
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise CaseFailure("configuration_error", "gremlin_http_uri_required", stop_target=True)
        self._endpoint = parsed.path.rstrip("/") or "/"
        self._connection = (HTTPSConnection if parsed.scheme == "https" else HTTPConnection)(
            parsed.hostname, parsed.port, timeout=settings["timeout_seconds"])
        self._headers = {"Content-Type": "application/json", "Accept": self.mime}
        if config.get("user"):
            self._headers["Authorization"] = "Basic " + base64.b64encode(
                f"{config['user']}:{_password(config)}".encode()).decode()
        self._graph_binding = config.get("graph_binding", "graph")
        self._traversal_source = config.get("traversal_source", "g")
        if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_\-]+", name)
                   for name in (self._graph_binding, self._traversal_source)):
            raise CaseFailure("configuration_error", "invalid_gremlin_binding", stop_target=True)
        self._spec = None
        self.query_engine = "Gremlin-Groovy HTTP"
        try:
            if not self.health():
                raise CaseFailure("environment_error", "gremlin_connection_probe_failed", stop_target=True)
            values, _ = self._script(self._version_script(), {}, settings["timeout_seconds"])
            if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
                raise CaseFailure("environment_error", "gremlin_version_unavailable", stop_target=True)
            self.version = self.backend + "/" + values[0]
        except Exception:
            self.close()
            raise

    def _request(self, script, parameters, timeout):
        payload = {"gremlin": script, "language": "gremlin-groovy", "bindings": parameters,
                   "aliases": {"g": self._traversal_source, "_gsp_graph": self._graph_binding}}
        data = json.dumps(payload, allow_nan=False).encode()
        started = None
        try:
            self._connection.timeout = timeout
            if self._connection.sock is None:
                self._connection.connect()
            else:
                self._connection.sock.settimeout(timeout)
            started = perf_counter()
            self._connection.request("POST", self._endpoint, body=data, headers=self._headers)
            response = self._connection.getresponse()
            body = response.read()
            if response.getheader("Content-Encoding", "").lower() == "gzip":
                import gzip
                body = gzip.decompress(body)
            envelope = json.loads(body)
            status = envelope.get("status", {})
            code = status.get("code")
            if response.status >= 300 or code not in (200, 204):
                message = str(status.get("message") or envelope.get("message") or response.reason)[:1000]
                if code == 598 or re.search(r"timed? ?out|timeout", message, re.I):
                    raise CaseFailure("timeout", f"{self.backend}_server_timeout", stop_target=True,
                                      details={"cancellation_verified": False, "elapsed_lower_bound_ms": None})
                category = "configuration_error" if response.status in (401, 403) or code in (401, 403) else (
                    "unsupported_feature" if re.search(r"syntax|not supported|unsupported|No signature|unable to resolve class", message, re.I)
                    else "query_error")
                raise CaseFailure(category, f"{self.backend}_server_{code or response.status}",
                                  stop_target=category == "configuration_error", details={"server_message": message})
            if code == 204:
                envelope["result"] = {"data": [], "meta": {}}
            return envelope, (perf_counter() - started) * 1000
        except CaseFailure:
            raise
        except (TimeoutError, socket.timeout) as exc:
            self._connection.close()
            raise CaseFailure("timeout", f"{self.backend}_network_timeout", stop_target=True,
                              details={"cancellation_verified": False, "elapsed_lower_bound_ms": None}) from exc
        except (HTTPException, OSError, ValueError, TypeError, EOFError, zlib.error) as exc:
            self._connection.close()
            raise CaseFailure("environment_error", f"{self.backend}_{type(exc).__name__}", stop_target=True) from exc

    def _script(self, script, parameters=None, timeout=None, *, require_list=False, max_items=None):
        envelope, elapsed = self._request(script, parameters or {}, timeout or self.settings["timeout_seconds"])
        started = perf_counter()
        values = _gremlin_data(envelope, require_list=require_list, max_items=max_items)
        return values, elapsed + (perf_counter() - started) * 1000

    def execute(self, query, parameters, timeout):
        # The compiler emits one traversal, not a script with terminal steps.
        # Consume max_rows + 1 on the server so truncation is visible and HTTP
        # never attempts to materialize a multi-million-row output.
        values, elapsed = self._script(f"({query}).limit({self.settings['max_rows'] + 1}).toList()", parameters, timeout,
                                      require_list=True, max_items=self.settings["max_rows"] + 1)
        if any(not isinstance(row, dict) for row in values):
            raise CaseFailure("incomplete_result", "gremlin_projection_must_be_a_map")
        typed_bag(values)
        return {"rows": values[:self.settings["max_rows"]], "complete": len(values) <= self.settings["max_rows"],
                "elapsed_ms": elapsed}

    def health(self):
        try:
            values, _ = self._script("g.inject(1).project('connected').by(__.identity()).toList()")
            return values == [{"connected": 1}]
        except Exception:
            return False

    def explain(self, query, parameters, *, analyze=False):
        suffix = ".profile().next()" if analyze else ".explain()"
        envelope, _ = self._request(f"({query}){suffix}", parameters, self.settings["timeout_seconds"])
        if not envelope.get("result", {}).get("data"):
            raise CaseFailure("unsupported_feature", "gremlin_execution_plan_unavailable")
        # Preserve native GraphSON metrics and units, not Cypher DB Hits.
        return {"format": self.backend + "_gremlin_graphson", "analyze": analyze,
                "plan": envelope["result"]["data"]}

    def _recipe(self, case):
        from state import GraphSpec
        if "graph_spec" not in case:
            raise CaseFailure("unsupported_feature", "structured_graph_recipe_required")
        spec = GraphSpec.from_dict(case["graph_spec"])
        if spec.pattern == "P7":
            raise CaseFailure("unsupported_feature", "gremlin_single_vertex_label")
        return spec

    def _reset(self):
        # Bounded data deletion in the explicitly resettable test graph.
        for kind in ("E", "V"):
            while True:
                values, _ = self._script(f"def n=g.{kind}().limit(1000).count().next(); "
                                         f"g.{kind}().limit(1000).drop().iterate(); n")
                if len(values) != 1 or type(values[0]) is not int or not 0 <= values[0] <= 1000:
                    raise CaseFailure("environment_error", "gremlin_reset_batch_invalid", stop_target=True)
                if values[0] == 0:
                    break

    def restore(self, case):
        spec = self._recipe(case)
        self._reset()
        self._prepare_schema(spec)
        for batch in spec.iter_batches("nodes", 250):
            if any(len(row["labels"]) != 1 for row in batch):
                raise CaseFailure("unsupported_feature", "gremlin_single_vertex_label")
            values, _ = self._script(self._vertex_script(), {"gsp_rows": batch})
            if values != [len(batch)]:
                raise CaseFailure("environment_error", "gremlin_vertex_batch_incomplete", stop_target=True)
        offset = 0
        for batch in spec.iter_batches("edges", 250):
            for number, row in enumerate(batch, offset):
                row["ordinal"] = number
            values, _ = self._script(self._edge_script(), {"gsp_rows": batch})
            if values != [len(batch)]:
                raise CaseFailure("environment_error", "gremlin_edge_batch_incomplete", stop_target=True)
            offset += len(batch)
        self._spec = spec
        self.index_evidence = self._check_indexes(spec)

    def check_state(self, case):
        spec = self._recipe(case)
        checks = []
        for number, check in enumerate(_gremlin_state_checks(spec.state_checks())):
            actual = self.execute(check["query"], {}, self.settings["timeout_seconds"])
            if not actual["complete"] or typed_bag(actual["rows"]) != typed_bag(check["expected_rows"]):
                raise CaseFailure("cost_check_failed", f"gremlin_structured_cost_check_{number}")
            checks.append({"query": check["query"], "rows": actual["rows"]})
        self.index_evidence = self._check_indexes(spec)
        return {"indexes": self.index_evidence, "checks": checks,
                "preparation": "bounded_data_reset_native_schema_and_indexes"}

    def close(self):
        self._connection.close()


class JanusGraph(GremlinHTTP):
    backend = "JanusGraph"

    def _version_script(self):
        return "org.janusgraph.graphdb.configuration.JanusGraphConstants.VERSION"

    def _prepare_schema(self, spec):
        schema = spec.schema()
        keys = {"_gsp_identity": "string"}
        for entry in schema["nodes"] + schema["relationships"]:
            keys.update(entry["properties"])
        indexes = [{"name": "gsp_internal_identity", "property": "_gsp_identity"}] + [
            {**index, "name": index["name"] + "_" + index["label"] + "_" + index["property"]} for index in spec.indexes]
        statements = ["_gsp_graph.tx().rollback(); def m=_gsp_graph.openManagement()"]
        for key, kind in keys.items():
            native = {"integer": "Long", "boolean": "Boolean", "string": "String"}[kind]
            statements.append(f"if(m.getPropertyKey({_g_literal(key)})==null) "
                              f"m.makePropertyKey({_g_literal(key)}).dataType({native}.class).cardinality(org.janusgraph.core.Cardinality.SINGLE).make()")
        for node in schema["nodes"]:
            label = _g_literal(node["label"])
            statements.append(f"if(m.getVertexLabel({label})==null) m.makeVertexLabel({label}).make()")
        for edge in schema["relationships"]:
            label = _g_literal(edge["type"])
            statements.append(f"if(m.getEdgeLabel({label})==null) m.makeEdgeLabel({label}).multiplicity(org.janusgraph.core.Multiplicity.MULTI).make()")
        for index in indexes:
            builder = (f"m.buildIndex({_g_literal(index['name'])}, org.apache.tinkerpop.gremlin.structure.Vertex.class)"
                       f".addKey(m.getPropertyKey({_g_literal(index['property'])}))")
            if index.get("label"):
                builder += f".indexOnly(m.getVertexLabel({_g_literal(index['label'])}))"
            else:
                builder += ".unique()"
            statements.append(f"if(m.getGraphIndex({_g_literal(index['name'])})==null) {builder}.buildCompositeIndex()")
        statements += ["m.commit()", "true"]
        self._script("; ".join(statements))
        # New keys are enabled immediately. Existing schema keys may need the
        # registered -> enabled lifecycle even after the data graph is empty.
        for index in indexes:
            name = _g_literal(index["name"])
            wait = (f"org.janusgraph.graphdb.database.management.ManagementSystem.awaitGraphIndexStatus(_gsp_graph, {name})"
                    ".status(org.janusgraph.core.schema.SchemaStatus.REGISTERED, org.janusgraph.core.schema.SchemaStatus.ENABLED)"
                    f".timeout({max(1, math.ceil(self.settings['timeout_seconds']))}, java.time.temporal.ChronoUnit.SECONDS).call().getSucceeded()")
            values, _ = self._script(wait)
            if values != [True]:
                raise CaseFailure("cost_check_failed", "janusgraph_index_registration_timeout")
            self._script(f"def m=_gsp_graph.openManagement(); def i=m.getGraphIndex({name}); "
                         "if(i.getFieldKeys().any{ i.getIndexStatus(it)!=org.janusgraph.core.schema.SchemaStatus.ENABLED }) "
                         "m.updateIndex(i, org.janusgraph.core.schema.SchemaAction.ENABLE_INDEX).get(); m.commit(); true")

    def _vertex_script(self):
        return ("gsp_rows.each{ r -> def v=g.addV(r.labels[0]).property('_gsp_identity', r.labels[0]+':'+r.uid).next(); "
                "r.properties.each{ k,x -> if(x!=null) v.property(k, x instanceof Number ? x.longValue() : x) } }; gsp_rows.size()")

    def _edge_script(self):
        return ("gsp_rows.each{ r -> def a=g.V().has('_gsp_identity', r.source_label+':'+r.source).next(); "
                "def b=g.V().has('_gsp_identity', r.target_label+':'+r.target).next(); def e=a.addEdge(r.type,b); "
                "r.properties.each{ k,x -> if(x!=null) e.property(k, x instanceof Number ? x.longValue() : x) } }; gsp_rows.size()")

    def _check_indexes(self, spec):
        evidence = []
        for index in spec.indexes:
            name = index["name"] + "_" + index["label"] + "_" + index["property"]
            values, _ = self._script(
                f"_gsp_graph.tx().rollback(); def m=_gsp_graph.openManagement(); def i=m.getGraphIndex({_g_literal(name)}); "
                "def r=i==null ? null : [name:i.name(), keys:i.getFieldKeys().collect{it.name()}, "
                "state:i.getFieldKeys().collect{i.getIndexStatus(it).toString()}, "
                "label:i.getSchemaTypeConstraint()==null ? null : i.getSchemaTypeConstraint().name(), composite:i.isCompositeIndex()]; m.rollback(); [r]")
            if (len(values) != 1 or not isinstance(values[0], dict) or values[0].get("keys") != [index["property"]]
                    or values[0].get("label") != index["label"] or values[0].get("state") != ["ENABLED"]
                    or values[0].get("composite") is not True):
                raise CaseFailure("cost_check_failed", "janusgraph_cost_index_not_enabled")
            evidence.append({"kind": "label_scoped_composite", **values[0]})
        return evidence


class ApacheHugeGraph(GremlinHTTP):
    backend = "ApacheHugeGraph"
    # HugeGraph's REST proxy exposes JSON scalars/maps and keeps graph entities
    # in a separate native shape. Experiment queries project properties only.
    mime = "application/json"

    def _version_script(self):
        return "org.apache.hugegraph.version.CoreVersion.VERSION.toString()"

    def _recipe(self, case):
        spec = super()._recipe(case)
        if any(len(edge["endpoints"]) != 1 for edge in spec.schema()["relationships"]):
            # HugeGraph's ordinary edge label has one source/target label pair.
            # Parent/subtype encoding would alter the logical relation labels.
            raise CaseFailure("unsupported_feature", "hugegraph_multi_endpoint_relationship_type")
        return spec

    def _prepare_schema(self, spec):
        schema = spec.schema()
        keys = {"_gsp_edge_ordinal": "integer"}
        for entry in schema["nodes"] + schema["relationships"]:
            keys.update(entry["properties"])
        # Only experiment labels/indexes are rebuilt; graph and server remain.
        # Emptying the graph does not permit mutating immutable edge endpoints.
        seconds = max(1, math.ceil(self.settings["timeout_seconds"]))
        reset = ("def s=_gsp_graph.schema(); def names=s.getEdgeLabels().collect{it.name()}; "
                 "if(names.any{ !it.startsWith('GSP_') }) throw new IllegalStateException('unexpected edge schema'); "
                 "names.each{ n -> def id=s.edgeLabel(n).remove(); if(id!=null && "
                 f"!_gsp_graph.taskScheduler().waitUntilTaskCompleted(id,{seconds}).success()) "
                 "throw new IllegalStateException('edge schema removal failed') }; "
                 "s.getIndexLabels().findAll{it.name().startsWith('gsp_cost_lookup_')}.each{ i -> "
                 "def id=s.indexLabel(i.name()).remove(); if(id!=null && "
                 f"!_gsp_graph.taskScheduler().waitUntilTaskCompleted(id,{seconds}).success()) "
                 "throw new IllegalStateException('index removal failed') }; true")
        self._script(reset)
        statements = ["def s=_gsp_graph.schema()"]
        for key, kind in keys.items():
            statements.append(f"s.propertyKey({_g_literal(key)}).{ {'integer': 'asLong', 'boolean': 'asBoolean'}[kind] }().ifNotExist().create()")
        for node in schema["nodes"]:
            label, properties = _g_literal(node["label"]), list(node["properties"])
            args = ", ".join(_g_literal(key) for key in properties)
            statements.append(f"if(s.getVertexLabels().any{{it.name()=={label}}}) "
                              f"s.vertexLabel({label}).properties({args}).nullableKeys({args}).append(); "
                              f"else s.vertexLabel({label}).useCustomizeStringId().properties({args}).nullableKeys({args}).create()")
        for edge in schema["relationships"]:
            label = _g_literal(edge["type"])
            args = ", ".join(_g_literal(key) for key in [*edge["properties"], "_gsp_edge_ordinal"])
            endpoint = edge["endpoints"][0]
            statements.append(f"s.edgeLabel({label}).link({_g_literal(endpoint['source_label'])}, "
                              f"{_g_literal(endpoint['target_label'])}).multiTimes().properties({args})"
                              ".sortKeys('_gsp_edge_ordinal').create()")
        for index in spec.indexes:
            name = index["name"] + "_" + index["label"] + "_" + index["property"]
            statements.append(f"s.indexLabel({_g_literal(name)}).onV({_g_literal(index['label'])})"
                              f".by({_g_literal(index['property'])}).secondary().create()")
        statements.append("true")
        self._script("; ".join(statements))

    def _vertex_script(self):
        return ("gsp_rows.each{ r -> def v=g.addV(r.labels[0]).property(org.apache.tinkerpop.gremlin.structure.T.id, "
                "r.labels[0]+':'+r.uid).next(); r.properties.each{ k,x -> if(x!=null) "
                "v.property(k, x instanceof Number ? x.longValue() : x) } }; gsp_rows.size()")

    def _edge_script(self):
        return ("gsp_rows.each{ r -> def a=g.V(r.source_label+':'+r.source).next(); "
                "def b=g.V(r.target_label+':'+r.target).next(); "
                "def props=['_gsp_edge_ordinal', r.ordinal.longValue()]; "
                "r.properties.each{ k,x -> if(x!=null){ props.add(k); props.add(x instanceof Number ? x.longValue() : x) } }; "
                "a.addEdge(r.type,b,props.toArray()) }; gsp_rows.size()")

    def check_state(self, case):
        spec = self._recipe(case)
        rendered = []
        for check in spec.state_checks():
            if check["kind"] == "node_count":
                query = "g.V()"
                for label in check["labels"]:
                    query += f".hasLabel({_g_literal(label)})"
                # HugeGraph rejects a root has(property) without an index.
                # These are population checks, not timed experiment queries;
                # scan and inspect their properties without inventing indexes.
                predicates = []
                for key, value in check["properties"].items():
                    present = f"v.property({_g_literal(key)}).isPresent()"
                    predicates.append(f"!{present}" if value is None else
                                      f"({present} && v.value({_g_literal(key)}) == {_g_literal(value)})")
                if predicates:
                    query += ".filter{ t -> def v=t.get(); " + " && ".join(predicates) + " }"
                rendered.append({"query": query + '.count().project("count").by(__.identity())',
                                 "expected_rows": [{"count": check["expected"]}]})
            elif check["kind"] == "node_properties":
                identity = f"{check['label']}:{check['uid']}"
                keys = list(check["properties"])
                query = f"g.V({_g_literal(identity)}).project(" + ", ".join(_g_literal(key) for key in keys) + ")"
                for key in keys:
                    query += f".by(__.coalesce(__.values({_g_literal(key)}), __.constant(null)))"
                rendered.append({"query": query, "expected_rows": [check["properties"]]})
            else:
                rendered.extend(_gremlin_state_checks([check], scan_edges=True))
        evidence = []
        for number, check in enumerate(rendered):
            actual = self.execute(check["query"], {}, self.settings["timeout_seconds"])
            if not actual["complete"] or typed_bag(actual["rows"]) != typed_bag(check["expected_rows"]):
                raise CaseFailure("cost_check_failed", f"hugegraph_structured_cost_check_{number}")
            evidence.append({"query": check["query"], "rows": actual["rows"]})
        self.index_evidence = self._check_indexes(spec)
        return {"indexes": self.index_evidence, "checks": evidence,
                "preparation": "bounded_data_reset_native_schema_and_indexes"}

    def _check_indexes(self, spec):
        evidence = []
        for index in spec.indexes:
            name = index["name"] + "_" + index["label"] + "_" + index["property"]
            values, _ = self._script(f"def s=_gsp_graph.schema(); def i=s.getIndexLabel({_g_literal(name)}); "
                                    "[[name:i.name(), status:i.status().toString(), kind:i.indexType().toString(), "
                                    "label:_gsp_graph.vertexLabel(i.baseValue()).name(), "
                                    "keys:i.indexFields().collect{_gsp_graph.propertyKey(it).name()}]]")
            if (len(values) != 1 or not isinstance(values[0], dict) or values[0].get("keys") != [index["property"]]
                    or values[0].get("label") != index["label"] or values[0].get("status") != "CREATED"
                    or values[0].get("kind") != "SECONDARY"):
                raise CaseFailure("cost_check_failed", "hugegraph_cost_index_not_ready")
            evidence.append({"kind": "label_scoped_secondary", **values[0]})
        return evidence
