from collections import defaultdict
from time import perf_counter
import os
from cypher.render import identifier
from data_structures.result import freeze


SUPPORT = {"subquery_call": True, "bodies": "full", "call": True, "conditional": True, "multi_label": True,
           "work": "count", "native_limit": False, "s7": True, "secondary_indexes": True}


class Database:
    def __init__(self, config):
        # Load the driver only when opening a connection; generation needs no driver.
        from neo4j import GraphDatabase, Query
        from neo4j.exceptions import Neo4jError
        from neo4j.graph import Node, Relationship, Path
        self.query_type = Query
        self.error_type = Neo4jError
        self.entity_types = Node, Relationship, Path
        password = os.environ[config["password_env"]]
        self.driver = GraphDatabase.driver(config["uri"], auth=(config["user"], password), notifications_min_severity="OFF")
        self.session = self.driver.session(database=config["name"])
        self.batch_size = config["batch_size"]
        self.version = self.session.run(
            "CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition"
        ).data()

    def load(self, graph):
        # Reset the dedicated database once per case, before either query is measured.
        indexes = self.session.run("SHOW INDEXES YIELD name WHERE name STARTS WITH 'gsp_' RETURN name").value()
        for name in indexes:
            self.session.run("DROP INDEX " + identifier(name)).consume()
        self.session.run(
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS"
        ).consume()
        self.session.run("CREATE INDEX gsp_load FOR (n:__GSP) ON (n.uid)").consume()
        # The temporary index makes relationship endpoint lookup efficient during bulk loading.
        self.session.run("CALL db.awaitIndexes(120)").consume()

        node_groups = defaultdict(list)
        for node in graph.nodes:
            node_groups[tuple(node["labels"])].append(node["properties"])
        for labels, nodes in node_groups.items():
            suffix = "".join(":" + identifier(label) for label in labels)
            query = "UNWIND $rows AS row CREATE (n:__GSP" + suffix + ") SET n = row"
            self.write_batches(query, nodes)

        edge_groups = defaultdict(list)
        for edge in graph.relationships:
            edge_groups[edge["type"]].append(edge)
        for relation_type, edges in edge_groups.items():
            query = (
                "UNWIND $rows AS row "
                "MATCH (s:__GSP {uid: row.start}), (t:__GSP {uid: row.end}) "
                "CREATE (s)-[r:" + identifier(relation_type) + "]->(t) SET r = row.properties"
            )
            self.write_batches(query, edges)
        # Remove loading aids and prepare the same experiment indexes for both queries.
        self.session.run("DROP INDEX gsp_load").consume()
        self.session.run("MATCH (n:__GSP) REMOVE n:__GSP").consume()
        for i, (label, key) in enumerate(graph.indexes):
            self.session.run(
                f"CREATE INDEX gsp_case_{i} FOR (n:{identifier(label)}) ON (n.{identifier(key)})"
            ).consume()
        self.session.run("CALL db.awaitIndexes(120)").consume()
        self.session.run("CALL db.prepareForReplanning()").consume()

    def write_batches(self, query, records):
        for start in range(0, len(records), self.batch_size):
            self.session.run(query, rows=records[start:start + self.batch_size]).consume()

    def execute(self, query, timeout):
        # Auto-commit avoids managed transaction retries inside a timing sample.
        start = perf_counter()
        try:
            result = self.session.run(self.query_type("CYPHER 25\n" + query.text, timeout=timeout), query.parameters)
            columns = list(result.keys())
            rows = [tuple(record.values()) for record in result]
            result.consume()
        except self.error_type as error:
            if "TimedOut" in (error.code or "") or "TransactionTimeout" in (error.code or ""):
                raise TimeoutError(str(error)) from error
            raise
        # Normalize after full consumption and timing, preserving nested entity identities.
        elapsed = (perf_counter() - start) * 1000
        rows = [tuple(self.normalize(value) for value in row) for row in rows]
        return columns, rows, elapsed

    def profile(self, query, timeout):
        try:
            result = self.session.run(self.query_type("CYPHER 25 PROFILE\n" + query.text, timeout=timeout), query.parameters)
            for _ in result:
                pass
            plan = result.consume().profile
        except self.error_type as error:
            if "TimedOut" in (error.code or "") or "TransactionTimeout" in (error.code or ""):
                raise TimeoutError(str(error)) from error
            raise
        return {"status": "ok", "format": "neo4j_profile", "plan": plan}

    def normalize(self, value):
        node_type, relationship_type, path_type = self.entity_types
        if isinstance(value, node_type):
            return ("node", value.element_id)
        if isinstance(value, relationship_type):
            return ("relationship", value.element_id)
        if isinstance(value, path_type):
            # Path order is observable; never fold a path together with its reflection.
            return ("path", tuple(node.element_id for node in value.nodes),
                    tuple(edge.element_id for edge in value.relationships))
        return freeze(value, self.normalize)

    def close(self):
        self.session.close()
        self.driver.close()
