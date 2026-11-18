from collections import defaultdict
from importlib.metadata import version
import os
from time import perf_counter, monotonic, sleep
from cypher.render import identifier
from data_structures.result import freeze


SUPPORT = {"subquery_call": False, "bodies": "pattern_list", "call": False, "conditional": False, "multi_label": True,
           "work": "pattern_list", "native_limit": True, "s7": False, "secondary_indexes": True}


class Database:
    def __init__(self, config):
        from falkordb import FalkorDB
        from falkordb.node import Node
        from falkordb.edge import Edge
        from falkordb.path import Path
        from redis import ResponseError
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        self.entity_types = Node, Edge, Path
        self.error_type = ResponseError
        password = os.environ[config["password_env"]] if config.get("password_env") else None
        # A sample is one request; transport failures must not cause hidden retries.
        self.client = FalkorDB(host=config["host"], port=config["port"], password=password,
                              retry=Retry(NoBackoff(), 0))
        self.name = config["name"]
        self.graph = self.client.select_graph(self.name)
        self.batch_size = config["batch_size"]
        self.version = {"driver": version("falkordb"), "redis_driver": version("redis"),
                        "modules": self.client.connection.module_list(),
                        "resultset_size": self.client.config_get("RESULTSET_SIZE"),
                        "default_timeout_ms": self.client.config_get("TIMEOUT")}
        if self.version["resultset_size"] != -1:
            self.client.close()
            raise ValueError("FalkorDB experiments require RESULTSET_SIZE=-1")

    def load(self, graph):
        # Replace only this experiment graph; never clear the Redis instance.
        if self.client.connection.exists(self.name):
            self.graph.delete()
        self.graph = self.client.select_graph(self.name)
        groups = defaultdict(list)
        for node in graph.nodes:
            groups[tuple(node["labels"])].append(node["properties"])
        for labels, records in groups.items():
            keys = sorted({key for label in labels for key in graph.schema["nodes"][label]})
            suffix = "".join(":" + identifier(label) for label in labels)
            properties = ", ".join(f"{identifier(key)}: row.{identifier(key)}" for key in keys)
            self.write_batches(f"UNWIND $rows AS row CREATE (n{suffix} {{{properties}}})", records)

        # uid indexes support loading; only requested indexes survive into measurement.
        loading = {(label, "uid") for node in graph.nodes for label in node["labels"]}
        for label, key in sorted(loading):
            self.graph.query(f"CREATE INDEX FOR (n:{identifier(label)}) ON (n.{identifier(key)})")
        self.await_indexes()
        groups = defaultdict(list)
        for edge in graph.relationships:
            source = graph.nodes[edge["start"]]["labels"][0]
            target = graph.nodes[edge["end"]]["labels"][0]
            groups[edge["type"], source, target].append(
                {**edge["properties"], "start": edge["start"], "end": edge["end"]})
        for (name, source, target), records in groups.items():
            keys = graph.schema["relationships"][name]["properties"]
            properties = ", ".join(f"{identifier(key)}: row.{identifier(key)}" for key in keys)
            self.write_batches(
                f"UNWIND $rows AS row MATCH (s:{identifier(source)} {{uid:row.start}}), "
                f"(t:{identifier(target)} {{uid:row.end}}) "
                f"CREATE (s)-[:{identifier(name)} {{{properties}}}]->(t)", records)
        requested = {tuple(index) for index in graph.indexes}
        for label, key in sorted(loading - requested):
            self.graph.query(f"DROP INDEX ON :{identifier(label)}({identifier(key)})")
        for label, key in sorted(requested - loading):
            self.graph.query(f"CREATE INDEX FOR (n:{identifier(label)}) ON (n.{identifier(key)})")
        self.await_indexes()

    def write_batches(self, query, records):
        for start in range(0, len(records), self.batch_size):
            self.graph.query(query, params={"rows": records[start:start + self.batch_size]})

    def await_indexes(self):
        deadline = monotonic() + 120
        while True:
            states = self.graph.ro_query("CALL db.indexes() YIELD status RETURN status").result_set
            if all(row[0] == "OPERATIONAL" for row in states):
                return
            if monotonic() >= deadline:
                raise RuntimeError("FalkorDB indexes did not become operational")
            sleep(0.05)

    def execute(self, query, timeout):
        started = perf_counter()
        try:
            result = self.graph.ro_query(query.text, params=query.parameters, timeout=int(timeout * 1000))
        except self.error_type as error:
            if "Query timed out" in str(error):
                raise TimeoutError(str(error)) from error
            raise
        columns = [column[1] for column in result.header]
        raw_rows = result.result_set
        elapsed = (perf_counter() - started) * 1000
        return columns, [tuple(self.normalize(value) for value in row) for row in raw_rows], elapsed

    def profile(self, query, timeout):
        # The pinned SDK encodes bindings but omits PROFILE's server-side timeout argument.
        text = self.graph._build_params_header(query.parameters) + query.text
        try:
            lines = self.graph.execute_command("GRAPH.PROFILE", self.name, text, "timeout", int(timeout * 1000))
        except self.error_type as error:
            if "Query timed out" in str(error):
                raise TimeoutError(str(error)) from error
            raise
        return {"status": "ok", "format": "falkordb_profile", "lines": lines}

    def normalize(self, value):
        node_type, edge_type, path_type = self.entity_types
        if isinstance(value, node_type):
            return ("node", value.id)
        if isinstance(value, edge_type):
            return ("relationship", value.id)
        if isinstance(value, path_type):
            return ("path", tuple(node.id for node in value.nodes()), tuple(edge.id for edge in value.edges()))
        return freeze(value, self.normalize)

    def close(self):
        self.client.close()
