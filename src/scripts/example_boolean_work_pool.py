"""Generate three SC1 pairs on the same Neo4j host, site, anchor and background."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cypher
from cypher.ast_nodes import Match, NodePattern, PathPattern, Projection, ProjectionItem, Property, Query
from data_structures.ast import Symbol, Variable, at
from databases.neo4j import SUPPORT
from graph_generation.builder import build_graph


def main():
    output = ROOT / "results/boolean-work-pool-example"
    output.mkdir(parents=True, exist_ok=True)
    labels = [Symbol("Root", "label"), Symbol("Subject", "label")]
    anchor = Variable("v1", "node", tuple(labels))
    host = Query([
        Match([PathPattern([NodePattern(anchor, labels)], [])]),
        Projection("RETURN", [ProjectionItem(Property(anchor, "keep", "bool"), Variable("result", "bool"))]),
    ])
    pattern = cypher.patterns_for(SUPPORT)["SC1"]
    options = {"background_nodes": 4, "background_relationships": 3, "high_units": 4, "indexes": False}
    controls = []
    comparison = {"pattern": pattern.id, "graph_seed": 0, "graph_options": options, "candidates": {}}
    for seed, candidate in [(4, "one_hop_count"), (43, "two_hop_count"), (33, "no_match_exists")]:
        tree = deepcopy(host)
        rng = random.Random(seed)
        site = cypher.choose_site(tree, pattern, rng)
        case = pattern.embed(tree, site, seed, rng)
        assert case.payload["work_candidate"] == candidate
        assert case.payload["correlated"] and case.payload["guard_name"] == "exists"

        # Reset only graph generation: the three natural embedding draws use different seeds.
        graph = build_graph(case, pattern, options, random.Random(0), SUPPORT, cypher.register_schema)
        mutant = pattern.mutate(deepcopy(case))
        cypher.check_pair(case.query, mutant, case)
        payload = case.payload
        assert payload["anchor_uids"] == [3] and payload["high_allocations"] == [4]
        background = {"nodes": graph.nodes[:options["background_nodes"]],
                      "relationships": graph.relationships[:options["background_relationships"]]}
        host_text = cypher.render(host).text
        controls.append((host_text, case.target, at(case.query, case.target).left, background))

        directory = output / candidate
        directory.mkdir(exist_ok=True)
        (directory / "query_a.cypher").write_text(cypher.render(case.query).text + "\n")
        (directory / "query_b.cypher").write_text(cypher.render(mutant).text + "\n")
        (directory / "graph.json").write_text(json.dumps(asdict(graph), indent=2) + "\n")
        regions = {}
        for role in ("witness", "low", "high"):
            leaves = [n for n in graph.nodes if payload[role + "_label"] in n["labels"]]
            regions[role] = {"terminals": len(leaves), "keep_true": sum(n["properties"]["keep"] for n in leaves)}
        assert regions["witness"] == {"terminals": 1, "keep_true": 1}
        comparison["candidates"][candidate] = {
            "embed_seed": case.seed, "nodes": len(graph.nodes), "relationships": len(graph.relationships),
            "regions": regions,
        }

    assert all(control == controls[0] for control in controls), "Host, site, guard or background differs"
    host_text, target, _, background = controls[0]
    (output / "host.cypher").write_text(host_text + "\n")
    comparison.update(target=target, anchor_uid=3, guard="exists", background=background)
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
