from .gremlin_http import GremlinHTTP, decode, normalize


GREMLIN_SUPPORT = {"multi_label": False, "secondary_indexes": True, "groovy": True}


class Database:
    def __init__(self, config):
        self.config = config
        self.client = GremlinHTTP(config["url"], config["http_timeout_seconds"])
        self.aliases = {"graph": config["name"], "g": config["traversal_source"]}
        # A top-level Map is iterated as entries by HTTP; wrap metadata as one result row.
        self.version = self.command("""
            [[janusgraph: org.janusgraph.graphdb.configuration.JanusGraphConstants.VERSION,
             tinkerpop: org.apache.tinkerpop.gremlin.util.Gremlin.version(),
             java: System.getProperty('java.version')]]
        """, aliases={})[0]
        self.version.update(storage_backend="berkeleyje", result_columns="declared",
                            serializer="graphson3", timeout_method="native")

    def command(self, script, bindings=None, aliases=None):
        data, _ = self.client.submit(script, bindings or {}, self.aliases if aliases is None else aliases)
        return decode(data["result"]["data"])

    def load(self, graph):
        self.command("""
            def manager = org.janusgraph.graphdb.management.JanusGraphManager.getInstance()
            manager.removeTraversalSource(source_name)
            def oldGraph = manager.removeGraph(graph_name)
            org.janusgraph.core.JanusGraphFactory.drop(oldGraph)
            def fresh = org.janusgraph.core.JanusGraphFactory.open(config_path)
            manager.putGraph(graph_name, fresh)
            manager.putTraversalSource(source_name, fresh.traversal())
            true
        """, {"graph_name": self.config["name"], "source_name": self.config["traversal_source"],
              "config_path": self.config["graph_config"]}, aliases={})
        properties = {}
        declarations = list(graph.schema["nodes"].values()) + [r["properties"] for r in graph.schema["relationships"].values()]
        for declaration in declarations:
            for key, kind in declaration.items():
                if key in properties and properties[key] != kind:
                    raise ValueError(f"JanusGraph property type conflict: {key}")
                properties[key] = kind
        # New keys, labels, and indexes share one schema transaction; no reindex is needed.
        states = self.command("""
            graph.tx().rollback()
            def mgmt = graph.openManagement()
            def types = [int:Long.class, float:Double.class, bool:Boolean.class, string:String.class]
            properties.each { key, kind ->
                mgmt.makePropertyKey(key).dataType(types[kind])
                    .cardinality(org.janusgraph.core.Cardinality.SINGLE).make()
            }
            labels.each { mgmt.makeVertexLabel(it).make() }
            relations.each {
                mgmt.makeEdgeLabel(it).multiplicity(org.janusgraph.core.Multiplicity.MULTI).make()
            }
            indexes.eachWithIndex { index, i ->
                mgmt.buildIndex('gsp_index_' + i, org.apache.tinkerpop.gremlin.structure.Vertex.class)
                    .addKey(mgmt.getPropertyKey(index[1])).indexOnly(mgmt.getVertexLabel(index[0]))
                    .buildCompositeIndex()
            }
            mgmt.commit()
            def check = graph.openManagement()
            def states = indexes.withIndex().collect { index, i ->
                check.getGraphIndex('gsp_index_' + i).getIndexStatus(check.getPropertyKey(index[1])).name()
            }
            check.rollback()
            states
        """, {"properties": properties, "labels": list(graph.schema["nodes"]),
              "relations": list(graph.schema["relationships"]), "indexes": graph.indexes})
        if any(state != "ENABLED" for state in states):
            raise RuntimeError(f"Fresh JanusGraph indexes are not enabled: {states}")
        ids = {}
        batch_size = self.config["batch_size"]
        for start in range(0, len(graph.nodes), batch_size):
            rows = [{"label": node["labels"][0], "properties": node["properties"]}
                    for node in graph.nodes[start:start+batch_size]]
            pairs = self.command("""
                def created = []
                rows.each { row ->
                    def vertex = graph.addVertex(T.label, row.label)
                    row.properties.each { key, value -> if (value != null) vertex.property(key, value) }
                    created.add([row.properties.uid, vertex])
                }
                graph.tx().commit()
                created.collect { pair -> [pair[0], pair[1].id()] }
            """, {"rows": rows})
            ids.update(pairs)
        # Endpoints use committed native IDs, without an extra loading index on uid.
        for start in range(0, len(graph.relationships), batch_size):
            rows = [{"start_id": ids[edge["start"]], "end_id": ids[edge["end"]],
                     "type": edge["type"], "properties": edge["properties"]}
                    for edge in graph.relationships[start:start+batch_size]]
            self.command("""
                rows.each { row ->
                    def source = graph.vertices(row.start_id).next()
                    def target = graph.vertices(row.end_id).next()
                    def edge = source.addEdge(row.type, target)
                    row.properties.each { key, value -> if (value != null) edge.property(key, value) }
                }
                graph.tx().commit()
                rows.size()
            """, {"rows": rows})

    def read(self, query, timeout, profile=False):
        # The native option covers evaluation and iterator consumption, but cannot prove termination.
        script = f"g.with('evaluationTimeout',{int(timeout*1000)})" + query.text[1:]
        if profile:
            script += ".profile()"
        return self.client.submit(script, query.parameters, self.aliases)

    def execute(self, query, timeout):
        if query.columns != ("result",):
            raise ValueError("Gremlin queries require the single result column")
        data, elapsed = self.read(query, timeout)
        return ["result"], [(normalize(value),) for value in decode(data["result"]["data"])], elapsed

    def profile(self, query, timeout):
        data, _ = self.read(query, timeout, profile=True)
        raw = data["result"]["data"]
        return {"status": "ok", "format": "tinkerpop_profile", "duration_unit": "ms",
                "metrics": decode(raw)[0], "raw": raw}

    def close(self):
        self.client.close()
