from data_structures.case import symbols


def register_schema(graph, case, bindings, pattern):
    # The typed AST and recipe declare properties, including currently empty regions.
    # 类型来自 AST 与构图配方，同时登记当前没有实例的区域。
    terminals = set(symbols(case.query)) | set(symbols(case.payload)) | case.host_symbols
    properties = {"uid": "int", "keep": "bool", "value": "int", "p_gate_true": "bool", "p_gate_false": "bool"}
    keys = {bindings[s.name]: s.kind[4:] for s in terminals if s.kind.startswith("key_")}
    properties.update(keys)
    edge_properties = {"keep": "bool", **keys}
    if case.payload.get("prefix_name") == "property":
        properties["p_prefix"] = case.payload["value"].kind
    nodes, relationships = graph.schema["nodes"], graph.schema["relationships"]
    labels = {bindings[s.name] for s in terminals if s.kind == "label"}
    labels.update(label for node in graph.nodes for label in node["labels"])
    for label in sorted(labels):
        nodes[label] = dict(properties)
    for symbol in sorted(terminals, key=lambda symbol: symbol.name):
        if symbol.kind == "relationship":
            relationships[bindings[symbol.name]] = {"endpoints": [], "properties": dict(edge_properties)}
    for edge in graph.relationships:
        declaration = relationships.setdefault(edge["type"], {"endpoints": [], "properties": dict(edge_properties)})
        pair = [graph.nodes[edge["start"]]["labels"][0], graph.nodes[edge["end"]]["labels"][0]]
        if pair not in declaration["endpoints"]:
            declaration["endpoints"].append(pair)
