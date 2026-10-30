from collections import defaultdict
import csv
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from cypher.render import identifier, literal
from data_structures.result import freeze


TYPES = {"int": "INT64", "float": "DOUBLE", "bool": "BOOL", "string": "STRING"}
SUPPORT = {"subquery_call": False, "subquery_unwind": False, "bodies": "match", "call": False, "conditional": False, "multi_label": False,
           "work": "match_count", "native_limit": True, "s7": False, "secondary_indexes": False}


class Database:
    def __init__(self, config):
        import ladybug
        self.driver = ladybug
        self.config = config
        self.connection = self.database = self.directory = None
        self.version = {"package": version("ladybug"), "backend": "pybind"}

    def load(self, graph):
        if not graph.schema["nodes"]:
            raise ValueError("Ladybug loading requires explicit schema declarations")
        if any(len(node["labels"]) != 1 for node in graph.nodes):
            raise ValueError("Ladybug nodes must belong to one declared table")
        if any(key != "uid" for _, key in graph.indexes):
            raise ValueError("This Ladybug adapter supports only implicit uid primary-key indexes")
        self.close()
        # Each case owns its temporary database; both queries use this same connection.
        # 每个用例独占一个临时数据库，两条查询始终使用同一连接。
        work = Path(self.config["directory"])
        work.mkdir(parents=True, exist_ok=True)
        self.directory = TemporaryDirectory(prefix="case-", dir=work)
        root = Path(self.directory.name)
        self.database = self.driver.Database(
            str(root / "graph"), backend="pybind",
            buffer_pool_size=self.config["memory_mb"] * 1024 * 1024,
            max_num_threads=self.config["threads"],
        )
        if self.database.backend != "pybind" or not self.database._use_pybind_backend:
            raise ValueError("Ladybug experiments require the pybind backend")
        self.connection = self.driver.Connection(self.database)
        self.version["engine"] = self.driver._get_version_info()

        for label, properties in graph.schema["nodes"].items():
            columns = [f"{identifier(key)} {TYPES[kind]}" + (" PRIMARY KEY" if key == "uid" else "")
                       for key, kind in properties.items()]
            self.connection.execute(f"CREATE NODE TABLE {identifier(label)}({', '.join(columns)})").close()
        for name, schema in graph.schema["relationships"].items():
            fields = [f"FROM {identifier(a)} TO {identifier(b)}" for a, b in schema["endpoints"]]
            fields += [f"{identifier(key)} {TYPES[kind]}" for key, kind in schema["properties"].items()]
            self.connection.execute(f"CREATE REL TABLE {identifier(name)}({', '.join(fields)})").close()

        groups = defaultdict(list)
        for node in graph.nodes:
            groups[node["labels"][0]].append(node["properties"])
        for label, records in groups.items():
            self.copy_rows(label, list(graph.schema["nodes"][label]), records)
        groups = defaultdict(list)
        for edge in graph.relationships:
            source = graph.nodes[edge["start"]]["labels"][0]
            target = graph.nodes[edge["end"]]["labels"][0]
            groups[edge["type"], source, target].append(
                {**edge["properties"], "start": edge["start"], "end": edge["end"]})
        for (name, source, target), records in groups.items():
            fields = ["start", "end", *graph.schema["relationships"][name]["properties"]]
            self.copy_rows(name, fields, records, source, target)

    def copy_rows(self, table, columns, records, source=None, target=None):
        path = Path(self.directory.name) / "batch.csv"
        # Native CSV distinguishes unquoted null fields from quoted empty strings.
        # 原生 CSV 区分未加引号的空值字段和加引号的空字符串。
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream, quoting=csv.QUOTE_NOTNULL)
            writer.writerow(columns)
            for record in records:
                values = [record.get(key) for key in columns]
                writer.writerow([str(value).lower() if isinstance(value, bool) else value
                                 for value in values])
        options = ["HEADER=true", "AUTO_DETECT=false", "QUOTE='\"'", "ESCAPE='\"'"]
        if source is not None:
            options += [f"FROM={literal(source)}", f"TO={literal(target)}"]
        query = f"COPY {identifier(table)} FROM {literal(str(path))} ({', '.join(options)})"
        self.connection.execute(query).close()

    def read(self, query, timeout, profile=False):
        self.connection.set_query_timeout(int(timeout * 1000))
        started = perf_counter()
        try:
            result = self.connection.execute(("PROFILE\n" if profile else "") + query.text, query.parameters)
            try:
                columns, types = result.get_column_names(), result.get_column_data_types()
                rows = []
                while result.has_next():
                    rows.append(result.get_next())
                elapsed = (perf_counter() - started) * 1000
            finally:
                result.close()
        except RuntimeError as error:
            if str(error).strip() == "Interrupted.":
                raise TimeoutError(str(error)) from error
            raise
        return columns, types, rows, elapsed

    def execute(self, query, timeout):
        columns, types, rows, elapsed = self.read(query, timeout)
        # Conversion follows timing; metadata distinguishes entities from ordinary maps.
        # 停止计时后再转换，按类型元数据区分实体与普通映射。
        return columns, [tuple(normalize(v, t) for v, t in zip(row, types)) for row in rows], elapsed

    def profile(self, query, timeout):
        columns, _, rows, _ = self.read(query, timeout, profile=True)
        return {"status": "ok", "format": "ladybug_profile", "columns": columns, "rows": rows}

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.database is not None:
            self.database.close()
            self.database = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


def normalize(value, kind):
    if value is None:
        return ("null",)
    if kind in {"NODE", "REL"}:
        identity = value["_ID"]
        return ("node" if kind == "NODE" else "relationship", (identity["table"], identity["offset"]))
    if kind == "INTERNAL_ID":
        return ("internal_id", (value["table"], value["offset"]))
    if kind == "RECURSIVE_REL":
        return ("path", tuple(normalize(v, "NODE")[1] for v in value["_NODES"]),
                tuple(normalize(v, "REL")[1] for v in value["_RELS"]))
    if kind.endswith("[]"):
        return ("list", tuple(normalize(v, kind[:-2]) for v in value))
    if kind.startswith("STRUCT("):
        fields = [part.strip().split(" ", 1) for part in type_parts(kind[7:-1])]
        return ("map", tuple(sorted((key, normalize(value[key], field_type)) for key, field_type in fields)))
    if kind.startswith("MAP("):
        _, item_type = type_parts(kind[4:-1])
        return ("map", tuple(sorted((key, normalize(v, item_type.strip())) for key, v in value.items())))
    return freeze(value)


def type_parts(text):
    # Split only the current type level, preserving nested map/struct descriptions.
    # 只拆分当前类型层级，保留嵌套 MAP 和 STRUCT 的描述。
    depth, start, parts = 0, 0, []
    for i, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    return parts + [text[start:]]
