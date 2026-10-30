from data_structures.case import symbols, bind
from data_structures.graph import Graph
from .random_graph import random_graph


def build_graph(case, pattern, options, rng, support, register_schema):
    bindings = bind_symbols(case, rng)
    graph = Graph()
    # Background data uses host symbols; the pattern adds its own regions afterwards.
    # 背景图使用宿主符号，随后由 Pattern 补充自己的局部区域。
    random_graph(graph, case.host_symbols, bindings, options, rng, support)
    payload = bind(case.payload, bindings)
    pattern.populate(graph, payload, options["high_units"])
    if options["indexes"]:
        for node in graph.nodes:
            for label in node["labels"]:
                if label.startswith("Pattern"):
                    graph.index(label, "uid")
    else:
        graph.indexes.clear()
    if not support["secondary_indexes"]:
        graph.indexes = [index for index in graph.indexes if index[1] == "uid"]
    for key in ("anchor_count", "anchor_uids", "high_allocations"):
        if key in payload:
            case.payload[key] = payload[key]
    register_schema(graph, case, bindings, pattern)
    return graph, bindings


def bind_symbols(case, rng):
    values = {}
    # Include both query forms and sort names to keep binding reproducible for a seed.
    # 同时收集源查询与变异内容中的符号，并排序以保持种子对应的绑定可复现。
    for symbol in sorted(set(symbols(case.query)) | set(symbols(case.payload)) | case.host_symbols,
                         key=lambda s: s.name):
        if symbol.kind in {"label", "relationship"}:
            # Separate names prevent accidental overlap; they do not hide nodes from queries.
            # 独立命名避免角色意外重叠，但不会让这些节点对其他查询不可见。
            prefix = "Pattern" if symbol.name.startswith("p_") else "Random"
            values[symbol.name] = prefix + "_" + symbol.name
        elif symbol.kind.startswith("key_"):
            values[symbol.name] = symbol.name
        else:
            values[symbol.name] = {"int": rng.randint(-2, 4), "bool": rng.choice([True, False]),
                                   "string": rng.choice(["a", "b", "c"]), "float": rng.choice([0.5, 1.5, 2.5])}[symbol.kind]
    return values
