import json
from data_structures.graph import Graph
from .random_graph import random_graph
from .constraints import apply_anchor_constraints, plant_host_witness


def build_graph(case, pattern, options, rng, support, register_schema):
    graph = Graph()
    # Background data uses host symbols; the pattern adds its own regions afterwards.
    random_graph(graph, case.host_symbols, options, rng, support)
    # Plant one host match before populate(), so planted candidate roots also get guards and workload.
    plant_host_witness(graph, case)
    pattern.populate(graph, case.payload, options["high_units"])
    # Workload roots also satisfy the host's equality filters on the anchor.
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
    graph.schema = json.loads(json.dumps(graph.schema))
    return graph

