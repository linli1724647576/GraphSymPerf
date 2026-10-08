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
from statistics import median
from time import perf_counter, monotonic, sleep


DEFAULT_MEASUREMENT = {
    "discovery_warmups": 3, "discovery_pairs": 6, "screen_ratio": 2.0,
    "confirmation_sessions": 2, "confirmation_warmups": 5,
    "confirmation_pairs": 20, "control_pairs": 10, "ratio": 3.0,
    "ci_low": 2.0, "direction_consistency": 0.75, "bootstrap_samples": 10_000,
    "timeout_seconds": 10.0, "max_rows": 100_000,
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
    patterns = {f"P{i}" for i in range(1, 17)} | {f"S{i}" for i in range(1, 11)}
    if name == "neo4j":
        return patterns
    if name == "memgraph":
        return patterns - {"S3"}
    if name == "falkordb":
        # Keep EXISTS/COUNT subqueries intact: rewriting them as list creation
        # changes the optimization mechanism that this experiment measures.
        return patterns - {"P4", "P5", "P15", "S1", "S2", "S3", "S4", "S7", "S8", "S10"}
    raise ValueError(f"unsupported dbms: {name}")


def make_database(config, settings):
    name = config.get("dbms", "neo4j").lower()
    adapters = {"neo4j": Neo4j, "memgraph": Memgraph, "falkordb": FalkorDB}
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
        self.execute("MATCH (n) DETACH DELETE n", {}, timeout)
        for query in state["setup"]:
            self.execute(query, {}, timeout)
        self.execute("CALL db.awaitIndexes($seconds)", {"seconds": max(1, math.ceil(timeout))}, timeout)

    def close(self):
        self.driver.close()


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


def _result(db, query, parameters, settings):
    try:
        result = db.execute(query, parameters, settings["timeout_seconds"])
    except CaseFailure as exc:
        if exc.status == "timeout":
            exc.details["timed_out_query"] = query
        raise
    if not result.get("complete") or len(result["rows"]) > settings["max_rows"]:
        raise CaseFailure("incomplete_result", "result_row_limit_exceeded")
    elapsed = result.get("elapsed_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0:
        raise CaseFailure("measurement_error", "invalid_elapsed_time")
    return result, typed_bag(result["rows"])


def check_case(db, case: dict, settings: dict):
    """Check cost facts, a full independent reference when supplied, and A/B."""
    for index, check in enumerate(case.get("checks", [])):
        _, actual = _result(db, check["query"], case.get("parameters", {}), settings)
        if actual != typed_bag(check["expected_rows"]):
            raise CaseFailure("cost_check_failed", f"cost_check_{index}")
    parameters = case.get("parameters", {})
    reference = case.get("query_ref") or (case.get("query_b") if case.get("reference_side") == "b" else None)
    db._reference_query = reference
    db._reference_evidence = None
    if reference:
        reference_result, reference_bag = _result(db, reference, parameters, settings)
        if "expected_rows" in case and reference_bag != typed_bag(case["expected_rows"]):
            raise CaseFailure("reference_mismatch", "reference_differs_from_constructed_result")
        db._reference_evidence = {"elapsed_ms": reference_result["elapsed_ms"],
                                  "validated": "expected_rows" in case}
    left, baseline = _result(db, case["query_a"], parameters, settings)
    if reference == case["query_b"]:
        right_result, right = reference_result, reference_bag
    else:
        right_result, right = _result(db, case["query_b"], parameters, settings)
    if right != baseline:
        raise CaseFailure("result_mismatch", "query_a_and_b_differ", details={
            "rows_a": len(left["rows"]), "rows_b": len(right_result["rows"]),
            "sample_a": left["rows"][:10], "sample_b": right_result["rows"][:10]})
    if "expected_rows" in case and baseline != typed_bag(case["expected_rows"]):
        raise CaseFailure("reference_mismatch", "queries_differ_from_constructed_reference")
    if reference and reference_bag != baseline:
        raise CaseFailure("reference_mismatch", "queries_differ_from_reference_query")
    return baseline, len(left["rows"])


def _schedule(count, rng):
    orders = ["AB"] * ((count + 1) // 2) + ["BA"] * (count // 2)
    rng.shuffle(orders)
    return orders


def _measure(db, case, baseline, settings, rng, count, output, *, queries=None):
    queries = queries or (case["query_a"], case["query_b"])
    for order in _schedule(count, rng):
        sample = {"order": order, "a_ms": None, "b_ms": None, "a_status": "pending", "b_status": "pending"}
        output.append(sample)
        for side in order.lower():
            try:
                result, bag = _result(db, queries[0 if side == "a" else 1], case.get("parameters", {}), settings)
                if bag != baseline:
                    raise CaseFailure("result_mismatch", "result_changed_during_measurement")
                if queries[0 if side == "a" else 1] == getattr(db, "_reference_query", None):
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
    if not 0.5 < settings["direction_consistency"] <= 1 or settings["ci_low"] <= 1 or settings["ratio"] <= 1 or settings["screen_ratio"] <= 1:
        raise ValueError("ratio gates must exceed 1; direction_consistency must be in (0.5, 1]")
    return settings


def state_fingerprint(case, database):
    """Identify the actual graph/checks, not the pattern or random seed."""
    state = {"setup": case["setup"], "checks": case.get("checks", []),
             "target": {key: database.get(key) for key in ("dbms", "uri", "database", "user")}}
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _restore_state(db, case, fingerprint, cache, *, force=False):
    if hasattr(db, "prepare_state"):
        db.prepare_state(case)
    reused = not force and cache is not None and cache.get("fingerprint") == fingerprint and db.health()
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
            summary["median_delta_ms"] > summary["noise_p99_ms"])


def run_case(case: dict, database: dict, measurement: dict | None = None, *, state_cache: dict | None = None) -> dict:
    """Execute one case. Failures are reportable; stop_target forbids continuation."""
    report = {"status": "started", "reason": "", "stop_target": False, "dbms": database.get("dbms", "neo4j"),
              "discovery": {"warmups": [], "samples": []}, "confirmation_sessions": [], "summary": None,
              "reference_comparisons": {}}
    db = None
    phase = "configuration"
    try:
        settings = measurement_settings(measurement or {})
        report["measurement"] = settings
        for query in (case["query_a"], case["query_b"], *([case["query_ref"]] if case.get("query_ref") else [])):
            validate_read_query(query)
        if case.get("reference_side") not in (None, "b", "ref") or case.get("reference_side") == "ref" and not case.get("query_ref"):
            raise ValueError("reference_side must identify b or an existing query_ref")
        if case.get("pattern") and case["pattern"] not in supported_patterns(database):
            raise CaseFailure("unsupported_feature", f"unsupported_pattern:{case['pattern']}")
        for check in case.get("checks", []):
            validate_read_query(check["query"], schema_check=True)
        seed = case.get("seed", 0)
        if type(seed) is not int:
            raise ValueError("case seed must be an integer")
        fingerprint = state_fingerprint(case, database)
        phase = "discovery_setup"
        db = make_database(database, settings)
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
        _remember_state(db, fingerprint, state_cache)
        phase = "discovery"
        rng = random.Random(seed)
        comparisons = [("ab", (case["query_a"], case["query_b"]), report)]
        if case.get("query_ref"):
            for side in ("a", "b"):
                name = f"query_{side}_vs_ref"
                comparison = {"discovery": {"warmups": [], "samples": []}, "confirmation_sessions": [], "summary": None}
                report["reference_comparisons"][name] = comparison
                comparisons.append((name, (case[f"query_{side}"], case["query_ref"]), comparison))
        active = []
        for name, queries, comparison in comparisons:
            discovery = comparison["discovery"]
            _measure(db, case, baseline, settings, rng, settings["discovery_warmups"], discovery["warmups"], queries=queries)
            _measure(db, case, baseline, settings, rng, settings["discovery_pairs"], discovery["samples"], queries=queries)
            samples = discovery["samples"]
            slow = "a" if median(sample["a_ms"] - sample["b_ms"] for sample in samples) > 0 else "b"
            discovery["summary"] = summarize(samples, slow)
            reference_comparison = name != "ab" or case.get("reference_side") == "b"
            if (not reference_comparison or slow == "a") and (discovery["summary"]["median_ratio"] >= settings["screen_ratio"] and
                    discovery["summary"]["direction_consistency"] >= settings["direction_consistency"]):
                active.append((name, queries, comparison, slow, [], []))
            else:
                comparison["status"] = "no_candidate"
                if reference_comparison and slow != "a":
                    comparison["reference_direction"] = "reference_not_faster"
        db.close()
        db = None
        if not active:
            report.update(status="no_candidate", reason="discovery_gate_not_met")
            return report
        for number in range(settings["confirmation_sessions"]):
            phase = f"confirmation_{number + 1}_setup"
            db = make_database(database, settings)
            _restore_state(db, case, fingerprint, state_cache, force=True)
            fresh_baseline, _ = check_case(db, case, settings)
            if fresh_baseline != baseline:
                raise CaseFailure("result_mismatch", "result_changed_after_state_restore")
            _remember_state(db, fingerprint, state_cache)
            phase = f"confirmation_{number + 1}"
            rng = random.Random(seed + 104729 * (number + 1))
            for name, queries, comparison, slow, pooled, noise_floors in active:
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
            db.close()
            db = None
        candidates = []
        for name, queries, comparison, slow, pooled, noise_floors in active:
            summary = summarize(pooled, slow, bootstrap_samples=settings["bootstrap_samples"], seed=seed)
            summary["noise_p99_ms"] = max(noise_floors)
            comparison["summary"] = summary
            passed = _passed(summary, comparison["confirmation_sessions"], settings)
            comparison["status"] = "candidate" if passed else "not_confirmed"
            if passed:
                candidates.append((name, summary))
        report.update(status="candidate" if candidates else "not_confirmed", reason="performance_difference_confirmed" if candidates else "confirmation_gate_not_met")
        if candidates:
            report["candidate_comparison"], report["summary"] = max(candidates, key=lambda item: item[1]["median_ratio"])
        elif report["summary"] is None:
            report["summary"] = max((comparison["summary"] for _, _, comparison, *_ in active), key=lambda item: item["median_ratio"])
    except CaseFailure as exc:
        report.update(status=exc.status, reason=exc.reason, stop_target=exc.stop_target, failure_phase=phase)
        report.update(exc.details)
        reference = getattr(db, "_reference_evidence", None)
        if exc.status == "timeout" and reference and reference["validated"]:
            lower = exc.details.get("elapsed_lower_bound_ms")
            timed_query = exc.details.get("timed_out_query")
            if (timed_query in (case.get("query_a"), case.get("query_b")) and
                    timed_query != getattr(db, "_reference_query", None) and lower is not None and
                    lower / reference["elapsed_ms"] >= settings["ratio"]):
                report.update(status="timeout_candidate", reason="tested_query_timed_out_with_fast_reference",
                              reference_elapsed_ms=reference["elapsed_ms"], ratio_lower_bound=lower / reference["elapsed_ms"],
                              timed_out_side="a" if timed_query == case.get("query_a") else "b")
    except (ValueError, KeyError, TypeError) as exc:
        report.update(status="invalid_case" if phase == "configuration" else "environment_error", reason=type(exc).__name__, stop_target=phase != "configuration", failure_phase=phase)
    except Exception as exc:
        report.update(status="environment_error", reason=type(exc).__name__, stop_target=True, failure_phase=phase)
    finally:
        if state_cache is not None and report["status"] not in {"candidate", "no_candidate", "not_confirmed"}:
            state_cache.clear()
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                report.update(status="environment_error", reason=f"close_failed:{type(exc).__name__}", stop_target=True)
                if state_cache is not None:
                    state_cache.clear()
    return report
