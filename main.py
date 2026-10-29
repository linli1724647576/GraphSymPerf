import argparse
import copy
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
import random
import time
import yaml
from databases.neo4j import Neo4j
from data_structures.case import Inapplicable, instantiate
from executor import run_pair, save_case
from graph_generation.builder import build_graph
from mutator import PATTERNS
from query_generation.embedding import choose_site, check_pair
from query_generation.generator import generate_ast


def main():
    parser = argparse.ArgumentParser(description="Run the settings in config.yaml. Executed cases replace all graph data.")
    parser.parse_args()
    config = load_config()
    mode = config["run"]["mode"]
    output = Path(config["run"]["output"]) / datetime.now().strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    db = None if mode == "generate" else Neo4j(config["database"])
    counts = Counter()
    try:
        with (output / "summary.csv").open("w", newline="") as stream:
            columns = ["case", "pattern", "seed", "status", "reason", "nodes",
                       "relationships", "rows", "ratio", "delta_ms"]
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for number, (pattern, seed) in enumerate(schedule(config), 1):
                row = {"case": number, "pattern": pattern.id, "seed": seed}
                try:
                    case, graph, source, mutant = prepare_case(pattern, seed, config)
                except Inapplicable as error:
                    # Skip unsupported sampled contexts here; unexpected errors still propagate.
                    # 此处只跳过不适用的随机上下文；实现中的意外错误继续抛出。
                    row.update(status="skipped", reason=str(error))
                else:
                    result = {"status": "generated"} if db is None else execute_case(
                        db, graph, source, mutant, config, check_only=mode == "check")
                    summary = result.get("confirmation_summary", result.get("discovery_summary", {}))
                    row.update(status=result["status"], nodes=len(graph.nodes), relationships=len(graph.relationships),
                               rows=result.get("rows", ""), ratio=summary.get("ratio", ""), delta_ms=summary.get("delta_ms", ""))
                    if config["run"]["save_all"] or mode == "generate" or result["status"] in {"candidate", "result_mismatch", "timeout"}:
                        directory = output / result["status"] / f"{number:05d}-{pattern.id}-{seed}"
                        save_case(directory, case, graph, source, mutant, result, config, db.version if db else None)
                writer.writerow(row)
                counts[row["status"]] += 1
                stream.flush()
                print(f"{number:05d} {pattern.id} seed={seed} {row['status']} {row.get('reason', '')}", flush=True)
    finally:
        if db:
            db.close()
    print(f"Results: {output.resolve()}")
    print("Cases: " + ", ".join(f"{status}={count}" for status, count in sorted(counts.items())))


def load_config():
    # Validate external settings once before generation and execution.
    # 在生成和执行之前统一校验外部配置，内部直接使用确定后的取值。
    with open("config.yaml") as file:
        config = yaml.safe_load(file)
    if config["run"]["mode"] not in {"experiment", "generate", "check"}:
        raise ValueError("run.mode must be experiment, generate, or check")
    case_seed = config["run"]["case_seed"]
    if case_seed is not None:
        if type(case_seed) is not int or case_seed < 0:
            raise ValueError("run.case_seed must be null or a nonnegative integer")
        config["run"]["rounds"] = 1
    if type(config["run"]["save_all"]) is not bool:
        raise ValueError("run.save_all must be a boolean")
    for name in config["run"]["patterns"]:
        if name not in PATTERNS:
            raise ValueError(f"Unknown pattern: {name}")
    for group, names in {
        "generation": ["clauses", "nodes", "expression_depth", "subquery_depth"],
        "graph": ["background_nodes", "background_relationships", "high_units"],
        "run": ["rounds", "minutes", "seed"],
        "measurement": ["warmups", "pairs", "confirmation_warmups", "confirmation_pairs", "timeout_seconds"],
    }.items():
        for name in names:
            value = config[group][name]
            if type(value) is not int or value < 0:
                raise ValueError(f"{group}.{name} must be a nonnegative integer")
    for group, name in [("generation", "clauses"), ("graph", "high_units"), ("measurement", "pairs"),
                        ("measurement", "confirmation_pairs"), ("measurement", "timeout_seconds")]:
        if config[group][name] == 0:
            raise ValueError(f"{group}.{name} must be positive")
    if config["measurement"]["ratio"] <= 1 or config["measurement"]["minimum_delta_ms"] < 0:
        raise ValueError("Thresholds require ratio > 1 and minimum_delta_ms >= 0")
    if not 0 < config["measurement"]["direction_consistency"] <= 1:
        raise ValueError("direction_consistency must be in (0, 1]")
    return config


def schedule(config):
    # Scheduling has its own RNG, so one case's generation does not alter later seeds.
    # 调度使用独立的随机数生成器，单个用例的生成过程不会改变后续种子。
    rng = random.Random(config["run"]["seed"])
    exact_seed = config["run"]["case_seed"]
    start = time.monotonic()
    for _ in range(config["run"]["rounds"]):
        names = list(config["run"]["patterns"])
        rng.shuffle(names)
        for name in names:
            if time.monotonic() - start >= config["run"]["minutes"] * 60:
                return
            yield PATTERNS[name], exact_seed if exact_seed is not None else rng.randrange(2 ** 32)


def prepare_case(pattern, seed, config):
    rng = random.Random(seed)
    # Generate the host without a pattern ID, then choose a compatible replacement site.
    # 先生成不依赖 Pattern 编号的宿主 AST，再选择兼容的替换位置。
    tree = generate_ast(rng, config["generation"])
    site = choose_site(tree, pattern, rng)
    if site is None:
        raise Inapplicable("No compatible AST position")
    case = pattern.embed(tree, site, seed, rng)
    graph, bindings = build_graph(case, pattern, config["graph"], rng)
    instantiate(case, bindings)
    source = case.query
    # Mutate a copy after binding; both queries retain the same constructed graph.
    # 完成绑定后只变异副本，两条查询共用已经构造好的图。
    mutant = pattern.mutate(copy.deepcopy(case))
    check_pair(source, mutant, case)
    return case, graph, source.to_cypher(), mutant.to_cypher()


def execute_case(db, graph, source, mutant, config, check_only):
    # Load once for the pair; measurement does not rebuild or modify the graph.
    # 每个查询对只装载一次图，测量期间不重建或修改图数据。
    db.load(graph)
    return run_pair(db, source, mutant, config["measurement"], check_only=check_only)


if __name__ == "__main__":
    main()
