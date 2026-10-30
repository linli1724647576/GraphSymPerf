from base64 import b64encode
from collections import defaultdict
from http.client import HTTPConnection, HTTPException
import json
import os
import struct
from threading import Timer
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4
from cypher.render import identifier
from data_structures.result import freeze


SUPPORT = {"subquery_call": True, "bodies": "full", "call": True, "conditional": False,
           "multi_label": True, "work": "count", "native_limit": False, "s7": False,
           "secondary_indexes": True, "persistent_imports": False}
GREMLIN_SUPPORT = {"multi_label": False, "secondary_indexes": True}
TYPES = {"int": "LONG", "float": "DOUBLE", "bool": "BOOLEAN", "string": "STRING"}


class Database:
    def __init__(self, config, *, language="cypher"):
        if language not in {"cypher", "gremlin"}:
            raise ValueError("ArcadeDB supports Cypher and Gremlin queries")
        self.language = language
        self.config = config
        url = urlsplit(config["url"])
        if url.scheme != "http":
            raise ValueError("This local ArcadeDB adapter requires an http URL")
        self.address = url.hostname, url.port or 80
        credentials = config["user"] + ":" + os.environ[config["password_env"]]
        self.headers = {"Authorization": "Basic " + b64encode(credentials.encode()).decode(),
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.connection = HTTPConnection(*self.address, timeout=config["http_timeout_seconds"])
        self.name = config["name"]
        self.timeout_ms = None
        self.cancellation = None
        self.version, _ = self.request("GET", "/api/v1/server?mode=basic")
        self.version["result_columns"] = "declared"
        self.version["timeout_method"] = "deadline_cancel" if language == "gremlin" else "native"

    def request(self, method, path, payload=None, *, tag=None, connection=None):
        connection = connection or self.connection
        headers = dict(self.headers)
        if tag:
            headers["X-ArcadeDB-Query-Tag"] = tag
        started = perf_counter()
        try:
            connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            raw = response.read()
        except (OSError, HTTPException) as error:
            # Socket timeouts also inherit TimeoutError; keep them out of performance samples.
            # socket 超时同样继承 TimeoutError，必须与数据库查询超时分开。
            connection.close()
            raise ConnectionError("HTTP request failed; query completion is unknown") from error
        except KeyboardInterrupt:
            if tag:
                try:
                    self.cancel(tag)
                finally:
                    connection.close()
            raise
        data = json.loads(raw)
        elapsed_ms = (perf_counter() - started) * 1000
        if response.status != 200:
            message = json.dumps({"http_status": response.status, **data})
            if data.get("exception") == "com.arcadedb.exception.TimeoutException":
                error = TimeoutError(message)
                error.timeout_method = "native"
                raise error
            if data.get("exception") == "com.arcadedb.exception.QueryTerminatedException":
                raise InterruptedError(message)
            raise RuntimeError(message)
        if data.get("truncated"):
            raise ValueError("ArcadeDB returned a truncated result")
        return data, elapsed_ms

    def cancel(self, tag):
        # The data connection still has a pending response; cancellation uses a separate connection.
        # 数据连接仍在等待响应，取消操作使用独立连接。
        control = HTTPConnection(*self.address, timeout=self.config["http_timeout_seconds"])
        try:
            self.cancellation, _ = self.request("POST", "/api/v1/server",
                {"command": "terminate queries tag " + tag, "wait": 5000}, connection=control)
        finally:
            control.close()
        return self.cancellation

    def command(self, language, text, parameters=None):
        return self.request("POST", "/api/v1/command/" + self.name,
                            {"language": language, "command": text, "params": parameters or {}, "limit": -1})[0]

    def load(self, graph):
        databases, _ = self.request("POST", "/api/v1/server", {"command": "list databases"})
        if self.name in databases["result"]:
            self.request("POST", "/api/v1/server", {"command": "drop database " + self.name})
        self.request("POST", "/api/v1/server", {"command": "create database " + self.name})
        self.timeout_ms = None
        for category, types in graph.schema.items():
            for name, declaration in types.items():
                kind = "VERTEX" if category == "nodes" else "EDGE"
                self.command("sql", f"CREATE {kind} TYPE {identifier(name)}")
                properties = declaration if category == "nodes" else declaration["properties"]
                for key, value_type in properties.items():
                    self.command("sql", f"CREATE PROPERTY {identifier(name)}.{identifier(key)} {TYPES[value_type]}")
        groups = defaultdict(list)
        for node in graph.nodes:
            groups[tuple(node["labels"])].append(node["properties"])
        for labels, records in groups.items():
            suffix = "".join(":" + identifier(label) for label in labels)
            self.write_batches("UNWIND $rows AS row CREATE (n" + suffix + ") SET n = row", records)
        # Native multi-label creation keeps one vertex; loading indexes use real labels.
        # 原生多标签创建保留单个顶点，装载索引使用实际标签。
        loading = {(label, "uid") for node in graph.nodes for label in node["labels"]}
        for label, key in sorted(loading):
            self.command("sql", f"CREATE INDEX ON {identifier(label)} ({identifier(key)}) NOTUNIQUE")
        groups = defaultdict(list)
        for edge in graph.relationships:
            source = graph.nodes[edge["start"]]["labels"][0]
            target = graph.nodes[edge["end"]]["labels"][0]
            groups[edge["type"], source, target].append(edge)
        for (kind, source, target), records in groups.items():
            self.write_batches("UNWIND $rows AS row "
                f"MATCH (s:{identifier(source)} {{uid:row.start}}), (t:{identifier(target)} {{uid:row.end}}) "
                f"CREATE (s)-[r:{identifier(kind)}]->(t) SET r = row.properties", records)
        requested = {tuple(index) for index in graph.indexes}
        for label, key in sorted(loading - requested):
            self.command("sql", "DROP INDEX " + identifier(f"{label}[{key}]"))
        for label, key in sorted(requested - loading):
            self.command("sql", f"CREATE INDEX ON {identifier(label)} ({identifier(key)}) NOTUNIQUE")

    def write_batches(self, text, records):
        for start in range(0, len(records), self.config["batch_size"]):
            self.command("cypher", text, {"rows": records[start:start + self.config["batch_size"]]})

    def read(self, query, timeout, profile=False):
        self.cancellation = None
        timeout_ms = int(timeout * 1000)
        if self.language == "cypher" and self.timeout_ms != timeout_ms:
            # A dedicated serial experiment can set the database deadline outside query timing.
            # 专用串行实验在查询计时之外设置数据库级时限。
            self.command("sql", f"ALTER DATABASE `arcadedb.command.timeout` {timeout_ms}")
            self.timeout_ms = timeout_ms
        payload = {"language": self.language, "command": query.text, "params": query.parameters,
                   "serializer": "record", "typeHints": True, "limit": -1}
        if profile:
            payload["profileExecution"] = "detailed"
        tag = "gsp-" + uuid4().hex
        if self.language == "gremlin":
            return self.read_gremlin(payload, tag, timeout)
        return self.request("POST", "/api/v1/query/" + self.name, payload, tag=tag)

    def read_gremlin(self, payload, tag, timeout):
        deadline = {}

        def cancel_at_deadline():
            # Java Gremlin iteration ignores command.timeout; cancel the tagged native query.
            # Java Gremlin 遍历不受 command.timeout 约束，改为取消带标签的原生查询。
            try:
                deadline["response"] = self.cancel(tag)
            except Exception as error:
                deadline["error"] = error

        timer = Timer(timeout, cancel_at_deadline)
        interrupted = None
        timer.start()
        try:
            response = self.request("POST", "/api/v1/query/" + self.name, payload, tag=tag)
        except InterruptedError as error:
            interrupted = error
        finally:
            # Keep the response socket open and join before starting another serial query.
            # 保持响应连接打开，等待取消线程结束后才进入下一条串行查询。
            timer.cancel()
            timer.join()
        if "error" in deadline:
            raise ConnectionError("Gremlin deadline cancellation failed") from deadline["error"]
        if interrupted is not None:
            if "response" not in deadline:
                raise interrupted
            if not any(row["status"] == "terminated" for row in deadline["response"]["result"]):
                raise ConnectionError("Gremlin cancellation was not confirmed") from interrupted
            error = TimeoutError(str(interrupted))
            error.timeout_method = "deadline_cancel"
            error.deadline_origin = "client_submission"
            error.cancellation = deadline["response"]
            raise error from interrupted
        # A complete response can win the cancellation race; retain its cancellation record.
        # 完整响应可能先于取消完成；保留取消记录并使用完整结果。
        return response

    def execute(self, query, timeout):
        if query.columns is None:
            raise ValueError("ArcadeDB HTTP queries require declared columns")
        if self.language == "gremlin" and query.columns != ("result",):
            raise ValueError("Gremlin queries require the single result column")
        data, elapsed_ms = self.read(query, timeout)
        rows = []
        for record in data["result"]:
            if record.get("@cat") in {"v", "e"} and "@rid" in record:
                if len(query.columns) != 1:
                    raise ValueError("Flattened entity requires one declared column")
                values, hints = (record,), {}
            else:
                if set(record) - {"@props"} != set(query.columns):
                    raise ValueError("Response fields differ from declared columns")
                values = tuple(record[column] for column in query.columns)
                hints = dict(item.split(":", 1) for item in record.get("@props", "").split(",") if item)
            rows.append(tuple(normalize(value, hints.get(column)) for column, value in zip(query.columns, values)))
        # Empty results and flattened entity aliases use declarations, not server column metadata.
        # 空结果和展开实体的别名来自声明，不冒充服务端列元数据。
        return list(query.columns), rows, elapsed_ms

    def profile(self, query, timeout):
        data, _ = self.read(query, timeout, profile=True)
        result = {"status": "ok", "format": "arcadedb_profile", "plan": data["explainPlan"],
                  "text": data["explain"], "metrics": data["profile"]}
        if self.cancellation is not None:
            result["cancellation"] = self.cancellation
        return result

    def close(self):
        self.connection.close()


def normalize(value, hint=None):
    if hint not in {None, "0", "1", "2", "3", "4", "5", "7", "9", "10", "11", "12"}:
        raise ValueError(f"Unsupported ArcadeDB type hint: {hint}")
    if value is None:
        return ("null",)
    if hint == "4":
        value = struct.unpack("!f", struct.pack("!f", value))[0]
    if isinstance(value, dict):
        if value.get("@cat") in {"v", "e"} and "@rid" in value:
            return ("node" if value["@cat"] == "v" else "relationship", value["@rid"])
        if value.get("_type") == "path":
            return ("path", tuple(node["@rid"] for node in value["nodes"]),
                    tuple(edge["@rid"] for edge in value["relationships"]))
    return freeze(value, normalize)
