from http.client import HTTPConnection, HTTPException
import json
from urllib.parse import quote, urlsplit
from .gremlin_http import GremlinHTTP, decode, normalize


GREMLIN_SUPPORT = {"multi_label": False, "secondary_indexes": True, "groovy": True}


class Database:
    def __init__(self, config):
        self.config = config
        self.client = GremlinHTTP(config["url"], config["http_timeout_seconds"])
        address = urlsplit(config["rest_url"])
        if address.scheme != "http":
            raise ValueError("The local HugeGraph adapter requires an http REST URL")
        self.rest = HTTPConnection(address.hostname, address.port or 80, timeout=config["http_timeout_seconds"])
        self.path = f"/graphspaces/{quote(config['graphspace'], safe='')}/graphs/{quote(config['name'], safe='')}"
        self.aliases = {"graph": config["graph_alias"], "g": config["traversal_source"]}
        self.version = self.command("""
            [[hugegraph: org.apache.hugegraph.version.CoreVersion.VERSION.toString(),
              tinkerpop: org.apache.tinkerpop.gremlin.util.Gremlin.version()]]
        """, aliases={})[0]
        self.version.update(storage_backend="rocksdb", serializer="graphson3", result_columns="declared",
                            timeout_method="native", relationship_encoding="parent_child", label_indexes=True)

    def request(self, method, suffix, payload=None):
        try:
            self.rest.request(method, self.path + suffix, json.dumps(payload) if payload is not None else None,
                              {"Content-Type": "application/json", "Accept": "application/json"})
            response = self.rest.getresponse()
            raw = response.read()
        except (OSError, HTTPException) as error:
            self.rest.close()
            raise ConnectionError("HugeGraph REST failed; loading completion is unknown") from error
        data = json.loads(raw) if raw else None
        if response.status not in {200, 201, 204}:
            raise RuntimeError(json.dumps({"http_status": response.status, "response": data}))
        return data

    def command(self, script, bindings=None, aliases=None):
        data, _ = self.client.submit(script, bindings or {}, self.aliases if aliases is None else aliases)
        return decode(data["result"]["data"])

    def load(self, graph):
        # Timing uses only the Gremlin port, so the server may close the idle REST keep-alive meanwhile;
        # start each load on a fresh connection instead of retrying a request.
        self.rest.close()
        self.request("DELETE", "/clear?confirm_message=" + quote("I'm sure to delete all data", safe=""))
        properties = {"_gsp_eid": "int"}
        declarations = list(graph.schema["nodes"].values()) + [r["properties"] for r in graph.schema["relationships"].values()]
        for declaration in declarations:
            for key, kind in declaration.items():
                if key in properties and properties[key] != kind:
                    raise ValueError(f"HugeGraph property type conflict: {key}")
                properties[key] = kind
        relations = []
        self.edge_labels = {}
        for name, declaration in graph.schema["relationships"].items():
            children = []
            for i, (source, target) in enumerate(sorted(declaration["endpoints"])):
                child = f"{name}__{i}"
                self.edge_labels[name, source, target] = child
                children.append({"name": child, "source": source, "target": target})
            relations.append({"name": name, "keys": list(declaration["properties"]) + ["_gsp_eid"], "children": children})
        # Queries retain logical parent labels; only stored edges use endpoint-specific children.
        states = self.command("""
            def schema = graph.schema()
            def methods = [int:'asLong', float:'asDouble', bool:'asBoolean', string:'asText']
            properties.each { key, kind ->
                schema.propertyKey(key)."${methods[kind]}"().valueSingle().create()
            }
            labels.each { name, declaration ->
                def keys = declaration.keySet().toList()
                schema.vertexLabel(name).useAutomaticId().properties(*keys)
                    .nullableKeys(*(keys - ['uid'])).enableLabelIndex(true).create()
            }
            relations.each { relation ->
                def keys = relation.keys
                schema.edgeLabel(relation.name).asBase().properties(*keys)
                    .nullableKeys(*(keys - ['_gsp_eid'])).multiTimes().sortKeys('_gsp_eid')
                    .enableLabelIndex(true).create()
                relation.children.each { child ->
                    schema.edgeLabel(child.name).withBase(relation.name).link(child.source, child.target)
                        .properties(*keys).nullableKeys(*(keys - ['_gsp_eid']))
                        .multiTimes().sortKeys('_gsp_eid').enableLabelIndex(true).create()
                }
            }
            indexes.eachWithIndex { index, i ->
                schema.indexLabel('gsp_index_' + i).onV(index[0]).by(index[1]).secondary().create()
            }
            indexes.withIndex().collect { index, i -> schema.getIndexLabel('gsp_index_' + i).status().name() }
        """, {"properties": properties, "labels": graph.schema["nodes"], "relations": relations, "indexes": graph.indexes})
        # HugeGraph calls a usable index CREATED after its synchronous create has completed.
        if any(state != "CREATED" for state in states):
            raise RuntimeError(f"HugeGraph indexes are not ready: {states}")
        self.vertex_ids = {}
        batch_size = self.config["batch_size"]
        for start in range(0, len(graph.nodes), batch_size):
            nodes = graph.nodes[start:start+batch_size]
            rows = [{"label": node["labels"][0], "properties": {k: v for k, v in node["properties"].items() if v is not None}}
                    for node in nodes]
            ids = self.request("POST", "/graph/vertices/batch", rows)
            if len(ids) != len(rows):
                raise ValueError("HugeGraph vertex batch returned an incomplete ID list")
            self.vertex_ids.update((node["properties"]["uid"], native) for node, native in zip(nodes, ids))
        for start in range(0, len(graph.relationships), batch_size):
            rows = []
            for eid, edge in enumerate(graph.relationships[start:start+batch_size], start):
                source = graph.nodes[edge["start"]]["labels"][0]
                target = graph.nodes[edge["end"]]["labels"][0]
                # The archived edge position distinguishes parallel edges across batch boundaries.
                rows.append({"label": self.edge_labels[edge["type"], source, target],
                    "outV": self.vertex_ids[edge["start"]], "outVLabel": source,
                    "inV": self.vertex_ids[edge["end"]], "inVLabel": target,
                    "properties": {**{k: v for k, v in edge["properties"].items() if v is not None}, "_gsp_eid": eid}})
            ids = self.request("POST", "/graph/edges/batch?check_vertex=true", rows)
            if len(ids) != len(rows):
                raise ValueError("HugeGraph edge batch returned an incomplete ID list")

    def read(self, query, timeout, profile=False):
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
        self.rest.close()
