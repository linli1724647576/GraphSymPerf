"""Triage confirmed gaps: cluster them by plan signature, or reduce one case.

    python scripts/triage.py cluster <results-dir>
    python scripts/triage.py reduce <case-dir> [--budget N] [--config FILE]

`cluster` reads archived cases only. `reduce` regenerates the case from its archived
pattern, seed and configuration, requires the regenerated queries to match the archive,
and then uses the database selected in config.yaml (same system as the archive). Every
trial replaces the experiment graph.
"""
import argparse
import copy
import json
from collections import defaultdict
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cypher
import gremlin
from databases import select_database
from databases.plans import signature
from data_structures.ast import at, walk
from data_structures.case import Inapplicable
from data_structures.graph import Graph
from executor import run_pair, save_case
from main import language_for, prepare_case

REPORTED = {"candidate", "scale_candidate"}


def cluster(results):
    groups = defaultdict(list)
    for record_path in sorted(Path(results).rglob("result.json")):
        record = json.loads(record_path.read_text())
        if record.get("status") not in REPORTED:
            continue
        plans_path = record_path.parent / "plans.json"
        plans = json.loads(plans_path.read_text()) if plans_path.exists() else {}
        if record["status"] == "scale_candidate":
            slower = record["scale"]["confirmation"]["side"]
        else:
            slower = (record.get("confirmation_summary") or record["discovery_summary"])["slower"]
        system = f'{record["config"]["language"]}/{record["config"]["database"]["system"]}'
        key = signature(system, record["pattern"], plans, slower)
        groups[json.dumps(key, sort_keys=True)].append({
            "case": str(record_path.parent), "status": record["status"], "slower": slower,
            "size": record["nodes"] + record["relationships"],
            "text": len((record_path.parent / f'query_a.{record["config"]["language"]}').read_text())})
    clusters = []
    for key, members in groups.items():
        # The representative is the smallest input in its cluster.
        members.sort(key=lambda item: (item["size"], item["text"]))
        clusters.append({"signature": json.loads(key), "count": len(members),
                         "representative": members[0]["case"], "members": members})
    clusters.sort(key=lambda item: -item["count"])
    (Path(results) / "clusters.json").write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
    print("| count | system | pattern | operator | extra work | representative |")
    print("| --- | --- | --- | --- | --- | --- |")
    for item in clusters:
        s = item["signature"]
        print(f'| {item["count"]} | {s["system"]} | {s["pattern"]} | {s["operator"]} | {s["extra_work"]} | {item["representative"]} |')
    return clusters


class Reducer:
    def __init__(self, directory, budget, config_path="config.yaml"):
        self.directory = Path(directory)
        self.record = json.loads((self.directory / "result.json").read_text())
        if self.record.get("status") != "candidate":
            raise ValueError("Only ratio candidates are reduced; scale candidates need two graphs per trial")
        with open(config_path) as file:
            current = yaml.safe_load(file)
        self.config = copy.deepcopy(self.record["config"])
        if current["database"]["system"] != self.config["database"]["system"] or current["language"] != self.config["language"]:
            raise ValueError(f"{config_path} must select the archived language and database system")
        self.config["database"] = current["database"]
        self.language, self.support = language_for(self.config)
        self.module = cypher if self.language is cypher else gremlin
        self.pattern = self.module.patterns_for(self.support)[self.record["pattern"]]
        self.case, self.graph, source, mutant = prepare_case(self.pattern, self.record["seed"], self.config)
        suffix = self.config["language"]
        archived = [(self.directory / f"query_{side}.{suffix}").read_text().rstrip("\n") for side in "ab"]
        if [source.text, mutant.text] != archived:
            raise ValueError("Regenerated queries differ from the archive; the case predates the current generator")
        self.budget, self.trials = budget, 0
        self.interface = self.output_interface(self.case.query)
        extra = {"language": self.config["language"]} if self.config["database"]["system"] == "arcadedb" else {}
        self.db = select_database(self.config["database"]["system"]).Database(self.config["database"], **extra)

    def output_interface(self, query):
        if self.module is cypher:
            return [(v.name, v.kind) for v in query.output()]
        from gremlin.scope import analyze
        return analyze(query).current.kind

    def render(self, case):
        # A reduced pair keeps the archived output interface.
        if self.output_interface(case.query) != self.interface:
            raise ValueError("Reduction changed the output interface")
        options = {"groovy": self.support.get("groovy", False)} if self.module is gremlin else {}
        mutant = self.pattern.mutate(copy.deepcopy(case))
        if self.module is cypher:
            cypher.check_pair(case.query, mutant, case, self.support.get("persistent_imports", True),
                              self.support.get("match_after_optional", True))
        else:
            gremlin.check_pair(case.query, mutant, case)
        return self.module.render(case.query, **options), self.module.render(mutant, **options)

    def interesting(self, case, graph):
        """The reduced pair still returns equal results and still shows the discovery gap."""
        self.trials += 1
        try:
            source, mutant = self.render(case)
        except (AssertionError, ValueError, KeyError, IndexError, AttributeError, Inapplicable):
            return False
        self.db.load(graph)
        try:
            result = run_pair(self.db, source, mutant, self.config["measurement"], phases=("discovery",), profile=False)
        except ValueError:
            # An adapter that rejects the reduced response shape makes the trial uninteresting.
            return False
        return result["status"] == "candidate"

    def ast_edits(self, case):
        protected = [at(case.query, case.target)]
        if "swap_index" in case.payload:
            owner = protected[0]
            protected = owner.clauses[case.payload["swap_index"]:case.payload["swap_index"] + 2]
        guarded = {id(node) for item in protected for _, node in walk(item)}
        edits = []
        for path, node in walk(case.query):
            if id(node) in guarded:
                continue
            for name in ("clauses", "paths", "items", "steps"):
                items = getattr(node, name, None)
                if not isinstance(items, list) or len(items) < 2:
                    continue
                for index, item in enumerate(items):
                    if id(item) in guarded or any(id(inner) in guarded for _, inner in walk(item)):
                        continue
                    if name == "steps" and getattr(node, "root", False) and index < 2:
                        continue
                    edits.append((path, name, index, sum(1 for _ in walk(item))))
            for name in ("where", "skip", "limit"):
                value = getattr(node, name, None)
                if value is not None and not any(id(inner) in guarded for _, inner in walk(value)):
                    edits.append((path, name, None, sum(1 for _ in walk(value)) if hasattr(value, "__dataclass_fields__") else 1))
        # Try the largest removals first.
        return sorted(edits, key=lambda edit: -edit[3])

    def apply(self, case, edit):
        trial = copy.deepcopy(case)
        target = at(trial.query, trial.target)
        swapped = None
        if "swap_index" in trial.payload:
            swapped = target.clauses[trial.payload["swap_index"]]
        path, name, index, _ = edit
        owner = at(trial.query, path)
        if index is None:
            setattr(owner, name, None)
        else:
            del getattr(owner, name)[index]
        # Locate the mutation point again by identity after the edit shifted positions.
        trial.target = next(p for p, node in walk(trial.query) if node is target)
        if swapped is not None:
            trial.payload["swap_index"] = target.clauses.index(swapped)
        return trial

    def reduce_ast(self):
        improved = True
        while improved and self.trials < self.budget:
            improved = False
            for edit in self.ast_edits(self.case):
                if self.trials >= self.budget:
                    break
                trial = self.apply(self.case, edit)
                if self.interesting(trial, self.graph):
                    self.case, improved = trial, True
                    print(f"  kept edit {edit[:3]} after {self.trials} trials", flush=True)
                    break

    def reduce_graph(self):
        payload = self.case.payload
        kept = set(payload.get("anchor_uids") or ()) | set(payload.get("host_witness") or ())
        kept |= {i for i, node in enumerate(self.graph.nodes) if any(l.startswith("Pattern_") for l in node["labels"])}
        chunk = None
        while self.trials < self.budget:
            removable = [i for i in range(len(self.graph.nodes)) if i not in kept]
            if not removable:
                break
            chunk = len(removable) if chunk is None else min(chunk, len(removable))
            for start in range(0, len(removable), chunk):
                if self.trials >= self.budget:
                    break
                trial, mapping = subgraph(self.graph, set(removable[start:start + chunk]))
                if self.interesting(self.case, trial):
                    self.graph, kept = trial, {mapping[i] for i in kept}
                    for key in ("anchor_uids", "host_witness"):
                        if payload.get(key):
                            payload[key] = [mapping[i] for i in payload[key]]
                    print(f"  removed {chunk} background nodes after {self.trials} trials", flush=True)
                    break
            else:
                if chunk == 1:
                    break
                chunk //= 2

    def run(self):
        try:
            if not self.interesting(self.case, self.graph):
                raise ValueError("The archived gap does not reproduce in discovery")
            self.reduce_ast()
            self.reduce_graph()
            source, mutant = self.render(self.case)
            self.db.load(self.graph)
            final = run_pair(self.db, source, mutant, self.config["measurement"])
            output = self.directory / "reduced"
            save_case(output, self.case, self.graph, source, mutant, {**final, "reduction_trials": self.trials},
                      self.config, self.db.version)
            print(f"reduced: {final['status']} after {self.trials} trials -> {output}")
            return final
        finally:
            self.db.close()


def subgraph(graph, drop):
    """Remove nodes and their relationships; keep uid equal to the node position."""
    mapping, nodes = {}, []
    for old, node in enumerate(graph.nodes):
        if old in drop:
            continue
        mapping[old] = len(nodes)
        nodes.append({**node, "properties": {**node["properties"], "uid": len(nodes)}})
    relationships = [{**edge, "start": mapping[edge["start"]], "end": mapping[edge["end"]]}
                     for edge in graph.relationships if edge["start"] in mapping and edge["end"] in mapping]
    return Graph(nodes=nodes, relationships=relationships, indexes=list(graph.indexes), schema=graph.schema), mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cluster").add_argument("results")
    reduce = commands.add_parser("reduce")
    reduce.add_argument("case")
    reduce.add_argument("--budget", type=int, default=40)
    reduce.add_argument("--config", default="config.yaml", help="supplies the database connection")
    args = parser.parse_args()
    if args.command == "cluster":
        cluster(args.results)
    else:
        Reducer(args.case, args.budget, args.config).run()


if __name__ == "__main__":
    main()
