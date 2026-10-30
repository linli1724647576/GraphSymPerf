from http.client import HTTPConnection, HTTPException
import json
import struct
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4
from data_structures.result import freeze
from data_structures.query import QueryRejected


class GremlinHTTP:
    def __init__(self, url, timeout):
        address = urlsplit(url)
        if address.scheme != "http":
            raise ValueError("The local Gremlin adapter requires an http URL")
        self.connection = HTTPConnection(address.hostname, address.port or 80, timeout=timeout)
        self.path = address.path or "/"
        self.headers = {"Content-Type": "application/json", "Accept": "application/vnd.gremlin-v3.0+json"}

    def submit(self, script, bindings, aliases):
        request_id = str(uuid4())
        payload = {"requestId": request_id, "gremlin": script, "bindings": bindings,
                   "language": "gremlin-groovy", "aliases": aliases}
        started = perf_counter()
        try:
            self.connection.request("POST", self.path, json.dumps(payload), self.headers)
            response = self.connection.getresponse()
            data = json.loads(response.read())
        except (OSError, HTTPException) as error:
            # A socket timeout does not establish a native query timeout or termination.
            # socket 等待超时不能证明原生查询超时或已经终止。
            self.connection.close()
            raise ConnectionError(f"Gremlin HTTP failed; completion is unknown (request {request_id})") from error
        elapsed = (perf_counter() - started) * 1000
        if response.status != 200:
            message = json.dumps({"http_status": response.status, **data})
            if "java.util.concurrent.TimeoutException" in data.get("exceptions", []):
                error = TimeoutError(message)
                error.timeout_method = "native"
                error.termination_confirmed = False
                error.stop_run = True
                error.request_id = data.get("requestId")
                error.client_request_id = request_id
                error.observed_ms = elapsed
                error.server_error = data
                raise error
            if "org.apache.hugegraph.exception.NoIndexException" in data.get("exceptions", []):
                raise QueryRejected("missing_index", {"http_status": response.status,
                    "client_request_id": request_id, "server_error": data})
            raise RuntimeError(message)
        if data["status"]["code"] != 200:
            raise RuntimeError(json.dumps(data))
        return data, elapsed

    def close(self):
        self.connection.close()


def decode(value):
    """Decode only the GraphSON types used by queries, loading, and PROFILE."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "@type" not in value:
        return {key: decode(item) for key, item in value.items()}
    kind, item = value["@type"], value["@value"]
    if kind in {"g:Int32", "g:Int64"}:
        return int(item)
    if kind in {"g:Double", "g:Float"}:
        number = float(item)
        return struct.unpack("!f", struct.pack("!f", number))[0] if kind == "g:Float" else number
    if kind == "g:List":
        return [decode(part) for part in item]
    if kind == "g:Map":
        return {decode(item[i]): decode(item[i+1]) for i in range(0, len(item), 2)}
    if kind in {"g:Vertex", "g:Edge", "hugegraph:HugeVertex"}:
        return {"@entity": "relationship" if kind == "g:Edge" else "node", "id": decode(item["id"])}
    if kind in {"hugegraph:LongId", "hugegraph:StringId", "hugegraph:EdgeId"}:
        return decode(item)
    if kind == "janusgraph:RelationIdentifier":
        return decode(item)["relationId"]
    if kind in {"g:Metrics", "g:TraversalMetrics"}:
        return decode(item)
    raise ValueError(f"Unsupported GraphSON type: {kind}")


def normalize(value):
    if isinstance(value, dict) and "@entity" in value:
        return value["@entity"], value["id"]
    return freeze(value, normalize)
