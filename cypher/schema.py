from copy import deepcopy
from itertools import product
from cypher.ast_nodes import NodePattern, PathPattern, Property
from data_structures.ast import walk


def register_schema(graph, case, pattern):
    # Types come from typed keys and AST expressions, never from sampled property values.
    # 属性类型来自带类型的属性键与 AST 表达式，不根据随机实例值猜测。
    trees = [case.query, pattern.mutate(deepcopy(case))]
    node_types = {"uid": "int", "keep": "bool", "value": "int", "p_guard": "bool", "p_anchor": "int"}
    relationship_types = {"keep": "bool"}
    for symbol in case.host_symbols:
        if symbol.kind.startswith("key_"):
            node_types[symbol] = symbol.kind[4:]
            relationship_types[symbol] = symbol.kind[4:]
    for tree in trees:
        for _, item in walk(tree):
            if isinstance(item, Property):
                target = relationship_types if item.variable.kind == "relationship" else node_types
                target[item.key] = item.kind
            elif isinstance(item, NodePattern):
                for key, value in item.properties:
                    node_types[key] = value.kind
    nodes = graph.schema["nodes"]
    relationships = graph.schema["relationships"]
    for symbol in sorted(case.host_symbols):
        if symbol.kind == "label":
            nodes[symbol] = dict(node_types)
    for node in graph.nodes:
        for label in node["labels"]:
            nodes[label] = dict(node_types)
    for tree in trees:
        for _, item in walk(tree):
            if isinstance(item, NodePattern):
                for label in item.labels or item.variable.labels:
                    nodes[label] = dict(node_types)

    def edge(kind, start, end):
        declaration = relationships.setdefault(kind, {"endpoints": [], "properties": dict(relationship_types)})
        if [start, end] not in declaration["endpoints"]:
            declaration["endpoints"].append([start, end])

    # Register written paths even when their regions contain no stored relationships.
    # 即使区域中没有关系实例，也登记查询路径声明的端点；箭头决定存储方向。
    for tree in trees:
        for _, path in walk(tree):
            if not isinstance(path, PathPattern):
                continue
            for left, relation, right in zip(path.nodes, path.relationships, path.nodes[1:]):
                starts = left.labels or left.variable.labels or nodes
                ends = right.labels or right.variable.labels or nodes
                for start, end in product(starts, ends):
                    for kind in relation.types:
                        if relation.direction in {"out", "both"}:
                            edge(kind, start, end)
                        if relation.direction in {"in", "both"}:
                            edge(kind, end, start)
    for relation in graph.relationships:
        for start, end in product(graph.nodes[relation["start"]]["labels"], graph.nodes[relation["end"]]["labels"]):
            edge(relation["type"], start, end)
