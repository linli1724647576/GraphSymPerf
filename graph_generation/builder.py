import json
from data_structures.graph import Graph
from .random_graph import random_graph
from .constraints import apply_anchor_constraints, plant_host_witness


def build_graph(case, pattern, options, rng, support, register_schema):
    graph = Graph()
    # Background data uses host symbols; the pattern adds its own regions afterwards.
    # 背景图使用宿主符号，随后由 Pattern 补充自己的局部区域。
    random_graph(graph, case.host_symbols, options, rng, support)
    # Plant one host match before populate(), so planted candidate roots also get guards and workload.
    # 在 populate() 之前铺设一份宿主匹配，铺设出的候选根同样获得决定项见证和工作量。
    plant_host_witness(graph, case)
    pattern.populate(graph, case.payload, options["high_units"])
    # Workload roots also satisfy the host's equality filters on the anchor.
    # 工作根同时满足宿主对锚点的等值过滤。
    apply_anchor_constraints(graph, case)
    if options["indexes"]:
        for node in graph.nodes:
            for label in node["labels"]:
                if label.startswith("Pattern"):
                    graph.index(label, "uid")
    else:
        graph.indexes.clear()
    if not support["secondary_indexes"]:
        graph.indexes = [index for index in graph.indexes if index[1] == "uid"]
    register_schema(graph, case, pattern)
    # Typed Symbol keys become plain strings before the schema reaches a backend.
    # schema 交给后端前，把带类型的 Symbol 键转成普通字符串。
    graph.schema = json.loads(json.dumps(graph.schema))
    return graph

