from collections import defaultdict
from time import perf_counter
import os
from neo4j import GraphDatabase, Query
from ast_nodes.base import identifier


class Neo4j:
    def __init__(self, config):
        password = os.environ[config["password_env"]]
        self.driver = GraphDatabase.driver(config["uri"], auth=(config["user"], password), notifications_min_severity="OFF")
        self.session = self.driver.session(database=config["name"])
        self.batch_size = config["batch_size"]
        self.version = self.session.run(
            "CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition"
        ).data()

    def load(self, graph):
        # Reset the dedicated database once per case, before either query is measured.
        # 每个用例只在测量前重建一次专用数据库，不在查询对之间重建。
        indexes = self.session.run("SHOW INDEXES YIELD name WHERE name STARTS WITH 'gsp_' RETURN name").value()
        for name in indexes:
            self.session.run("DROP INDEX " + identifier(name)).consume()
        self.session.run(
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS"
        ).consume()
        self.session.run("CREATE INDEX gsp_load FOR (n:__GSP) ON (n.uid)").consume()
        # The temporary index makes relationship endpoint lookup efficient during bulk loading.
        # 临时索引用于批量装载关系时快速查找两端节点。
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
        # 删除装载辅助结构，再为两条查询准备同一套实验索引。
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
        # 自动提交避免驱动的托管事务重试混入一次计时样本。
        start = perf_counter()
        result = self.session.run(Query("CYPHER 25\n" + query, timeout=timeout))
        columns = list(result.keys())
        rows = [tuple(record.values()) for record in result]
        # Stop timing after full consumption; result normalization happens in the executor.
        # 完整读取结果后结束计时；结果规范化由执行器在计时之外完成。
        result.consume()
        return columns, rows, (perf_counter() - start) * 1000

    def profile(self, query, timeout):
        result = self.session.run(Query("CYPHER 25 PROFILE\n" + query, timeout=timeout))
        for _ in result:
            pass
        return result.consume().profile

    def close(self):
        self.session.close()
        self.driver.close()
