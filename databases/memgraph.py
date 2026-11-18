from collections import defaultdict
from importlib.metadata import version
from time import perf_counter
import os
from cypher.render import identifier
from data_structures.result import freeze


SUPPORT = {"subquery_call": False, "subquery_unwind": False, "bodies": "full", "call": True, "conditional": False, "multi_label": True,
           "work": "count", "native_limit": False, "s7": False, "secondary_indexes": True,
           # Memgraph rejects a mandatory MATCH after OPTIONAL MATCH in one query part.
           "match_after_optional": False}
TIMEOUT_CODE = "Memgraph.TransientError.MemgraphError.MemgraphError"
TIMEOUT_MESSAGE = "Transaction was asked to abort because of transaction timeout."


class Database:
    def __init__(self, config):
        from neo4j import GraphDatabase, Query
        from neo4j.exceptions import Neo4jError
        from neo4j.graph import Node, Relationship, Path
        self.query_type = Query
        self.error_type = Neo4jError
        self.entity_types = Node, Relationship, Path
        auth = (config["user"], os.environ[config["password_env"]]) if config["password_env"] else None
        self.driver = GraphDatabase.driver(config["uri"], auth=auth)
        self.session = self.driver.session(database=config["name"])
        self.batch_size = config["batch_size"]
        server = self.driver.get_server_info()
        self.version = {"driver": version("neo4j"), "server": server.agent,
                        "protocol": list(server.protocol_version),
                        "engine": self.session.run("SHOW VERSION").single()["version"]}
        # A global limit would shorten the per-query deadline; validate the experiment once.
        settings = dict(self.session.run("SHOW DATABASE SETTINGS").values())
        self.version["query_timeout"] = settings["query.timeout"]
        storage = dict(self.session.run("SHOW STORAGE INFO").values())
        flags = {row["name"]: row["current_value"] for row in self.session.run("SHOW CONFIG")}
        automatic = {name: flags[name] for name in (
            "storage_automatic_label_index_creation_enabled", "storage_automatic_edge_type_index_creation_enabled")}
        self.version.update(storage_mode=storage["global_storage_mode"], automatic_indexes=automatic)
        if (float(settings["query.timeout"]) != 0 or storage["global_storage_mode"] != "IN_MEMORY_TRANSACTIONAL"
                or any(value != "false" for value in automatic.values())):
            self.close()
            raise ValueError("Memgraph requires query.timeout=0, IN_MEMORY_TRANSACTIONAL, and automatic indexes disabled")

    def load(self, graph):
        self.session.run("DROP ALL INDEXES").consume()
        self.session.run("MATCH (n) DETACH DELETE n").consume()
        nodes = defaultdict(list)
        for node in graph.nodes:
            nodes[tuple(node["labels"])].append(node["properties"])
        for labels, records in nodes.items():
            suffix = "".join(":" + identifier(label) for label in labels)
            self.write_batches("UNWIND $rows AS row CREATE (n:__GSP" + suffix + ") SET n = row", records)
        # uid is unique within the case; CREATE preserves every parallel relationship.
        self.session.run("CREATE INDEX ON :__GSP(uid)").consume()
        edges = defaultdict(list)
        for edge in graph.relationships:
            edges[edge["type"]].append(edge)
        for kind, records in edges.items():
            self.write_batches(
                "UNWIND $rows AS row MATCH (s:__GSP {uid:row.start}), (t:__GSP {uid:row.end}) "
                "CREATE (s)-[r:" + identifier(kind) + "]->(t) SET r = row.properties", records)
        self.session.run("DROP INDEX ON :__GSP(uid)").consume()
        self.session.run("MATCH (n:__GSP) REMOVE n:__GSP").consume()
        for label, key in graph.indexes:
            self.session.run(f"CREATE INDEX ON :{identifier(label)}({identifier(key)})").consume()
        # Analyze only after loading and index creation; neither query changes these conditions.
        self.session.run("ANALYZE GRAPH").consume()

    def write_batches(self, query, records):
        for start in range(0, len(records), self.batch_size):
            self.session.run(query, rows=records[start:start + self.batch_size]).consume()

    def read(self, query, timeout, profile=False):
        text = ("PROFILE\n" if profile else "") + query.text
        started = perf_counter()
        try:
            result = self.session.run(self.query_type(text, timeout=timeout), query.parameters)
            columns = list(result.keys())
            rows = [tuple(record.values()) for record in result]
            result.consume()
        except self.error_type as error:
            # The generic transient code alone also covers failures other than timeout.
            if error.code == TIMEOUT_CODE and error.message == TIMEOUT_MESSAGE:
                raise TimeoutError(f"{error.code}: {error.message}") from error
            raise
        return columns, rows, (perf_counter() - started) * 1000

    def execute(self, query, timeout):
        columns, rows, elapsed_ms = self.read(query, timeout)
        # Normalize after complete consumption and timing, including nested entities.
        rows = [tuple(self.normalize(value) for value in row) for row in rows]
        return columns, rows, elapsed_ms

    def profile(self, query, timeout):
        columns, rows, _ = self.read(query, timeout, profile=True)
        return {"status": "ok", "format": "memgraph_profile", "columns": columns, "rows": rows}

    def normalize(self, value):
        node_type, relationship_type, path_type = self.entity_types
        if isinstance(value, node_type):
            return ("node", value.element_id)
        if isinstance(value, relationship_type):
            return ("relationship", value.element_id)
        if isinstance(value, path_type):
            return ("path", tuple(node.element_id for node in value.nodes),
                    tuple(edge.element_id for edge in value.relationships))
        return freeze(value, self.normalize)

    def close(self):
        self.session.close()
        self.driver.close()
