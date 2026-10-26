"""Check the manuscript's complete examples with the existing bounded interpreter.

Run from the repository root:
    .venv/bin/python docs/examples/verify_six_stage_examples.py

The checks establish the small examples' semantic calculations; they are not
database execution measurements or evidence of a performance difference.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import json
from pathlib import Path
import re
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "Src"))

import ir
from ast_pattern.dependency_check import _LogicalTrace
from executor import typed_bag
from population import evaluate_query
from query import parse_query
from state import GraphSpec


def explicit_graph(nodes, edges):
    labels = {uid: names[0] for uid, names, _ in nodes}
    return GraphSpec(
        "AST", 1, 0, include_reference=False, version=2,
        node_groups=tuple({"count": 1, "labels": names,
                           "properties": {"uid": uid, **properties}}
                          for uid, names, properties in nodes),
        edge_groups=tuple({"count": 1, "source": source, "target": target,
                           "source_label": labels[source], "target_label": labels[target],
                           "type": kind, "properties": {}}
                          for source, target, kind in edges),
    )


def graph_a(scale):
    # 1=s, 2=a, 3=t, 4=b, 5...=z_i; stored relationships are directed,
    # while both manuscript queries match them without a direction constraint.
    nodes = [(1, ["Start"], {"id": 1, "keep": True}),
             (2, ["Middle"], {"keep": True}),
             (3, ["Target"], {"keep": True}),
             (4, ["Middle"], {"keep": False})]
    nodes.extend((5 + i, ["Target"], {"keep": True}) for i in range(scale))
    edges = [(1, 2, "T"), (2, 3, "T"), (1, 4, "T")]
    edges.extend((4, 5 + i, "T") for i in range(scale))
    return explicit_graph(nodes, edges)


def graph_b(inputs, scale):
    nodes = [(uid, ["Root"], {}) for uid in range(1, inputs + 1)]
    root = inputs + 1
    nodes.append((root, ["WorkRoot"], {"id": 1}))
    nodes.extend((root + 1 + i, ["Work"], {}) for i in range(scale))
    edges = [(root, root + 1 + i, "HIGH") for i in range(scale)]
    return explicit_graph(nodes, edges)


def located(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from located(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for i, child in enumerate(value):
            yield from located(child, (*address, i))


def coalesce_address(query):
    addresses = [address for address, value in located(query)
                 if isinstance(value, ir.Call) and value.name == "coalesce"]
    assert len(addresses) == 1
    return addresses[0]


def unfiltered_count(query):
    count = query.returns[0].expression
    assert isinstance(count, ir.Count)
    if count.body is None:
        count = replace(count, where=None)
    else:
        body = count.body
        assert isinstance(body, ir.Query)
        clauses = tuple(replace(clause, where=None) if isinstance(clause, ir.Match) else clause
                        for clause in ir.clauses(body))
        count = replace(count, body=ir.with_clauses(body, clauses))
    return replace(query, returns=(replace(query.returns[0], expression=count),))


def verify():
    manuscript = REPOSITORY / "docs" / "GraphSymPerf-六阶段方法汇报稿.md"
    blocks = re.findall(r"```cypher\s*\n(.*?)\n```", manuscript.read_text(), re.S)
    assert len(blocks) == 5, "Update the verifier if the manuscript's complete-query blocks change"
    a_source, b_source, a_mutant, b_mutant, dead_projection = map(parse_query, blocks)
    for query in (a_source, b_source, a_mutant, b_mutant, dead_projection):
        ir.validate_query(query)

    output = {"validation": "bounded offline semantics, not DBMS/performance evidence", "P3": [], "S7": []}
    for scale in (1, 2, 7):
        graph = graph_a(scale)
        assert graph.counts() == {"nodes": scale + 4, "edges": scale + 3}
        expected = [{"n": 1}]
        assert typed_bag(evaluate_query(a_source, graph)) == typed_bag(expected)
        assert typed_bag(evaluate_query(a_mutant, graph)) == typed_bag(expected)
        candidates = evaluate_query(unfiltered_count(a_source), graph)
        assert candidates == [{"n": scale + 1}]
        trace = _LogicalTrace(graph)
        trace.run_at(a_source)
        output["P3"].append({"N": scale, **graph.counts(), "candidate_paths": scale + 1,
                             "accepted_paths": 1, "result_a": expected, "result_b": expected})

    for inputs, scale in ((1, 3), (2, 3), (3, 5)):
        graph = graph_b(inputs, scale)
        expected = [{"value": 0}] * inputs
        assert typed_bag(evaluate_query(b_source, graph)) == typed_bag(expected)
        assert typed_bag(evaluate_query(b_mutant, graph)) == typed_bag(expected)
        assert evaluate_query(dead_projection, graph) == [{"n": 1}]
        address = coalesce_address(b_mutant)
        trace = _LogicalTrace(graph)
        trace.run_at(b_mutant)
        assert len(trace.inputs[address]) == inputs
        assert not trace.inputs.get((*address, "args", 2))
        dead_trace = _LogicalTrace(graph)
        dead_trace.run_at(dead_projection)
        assert not dead_trace.inputs.get(coalesce_address(dead_projection))
        output["S7"].append({"m": inputs, "N": scale, "result_a": expected, "result_b": expected,
                             "logical_coalesce_inputs": inputs, "logical_fallback_requests": 0,
                             "dead_projection_result": [{"n": 1}], "dead_coalesce_value_requests": 0})
    return output


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
