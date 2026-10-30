import argparse
import copy
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
import random
import time
import yaml
import cypher
import gremlin
from databases import select_database
from data_structures.case import Inapplicable, instantiate
from executor import run_pair, save_case
from graph_generation.builder import build_graph


def main():
    parser = argparse.ArgumentParser(description="Run the settings in config.yaml. Executed cases replace all graph data.")
    parser.parse_args()
    config = load_config()
    backend = select_database(config["database"]["system"])
    mode = config["run"]["mode"]
    output = Path(config["run"]["output"]) / datetime.now().strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    connection = {"language": config["language"]} if config["database"]["system"] == "arcadedb" else {}
    db = None if mode == "generate" else backend.Database(config["database"], **connection)
    counts = Counter()
    try:
        with (output / "summary.csv").open("w", newline="") as stream:
            columns = ["case", "pattern", "seed", "status", "reason", "nodes",
                       "relationships", "rows", "ratio", "delta_ms"]
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for number, (pattern, seed) in enumerate(schedule(config), 1):
                row = {"case": number, "pattern": pattern.id, "seed": seed}
                result, failure = {}, None
                try:
                    case, graph, source, mutant = prepare_case(pattern, seed, config)
                except Inapplicable as error:
                    # Skip unsupported sampled contexts here; unexpected errors still propagate.
                    # 此处只跳过不适用的随机上下文；实现中的意外错误继续抛出。
                    row.update(status="skipped", reason=str(error))
                else:
                    try:
                        result = {"status": "generated"} if db is None else execute_case(
                            db, graph, source, mutant, config, check_only=mode == "check")
                    except (Exception, KeyboardInterrupt) as error:
                        # Preserve a reproducible failure, then expose the original exception.
                        # 保存可重放的失败输入后继续抛出原始异常，不把实现错误记作随机跳过。
                        status = "interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "error"
                        result = {"status": status, "error": {"type": type(error).__name__, "message": str(error)}}
                        if db is not None and getattr(db, "cancellation", None):
                            result["cancellation"] = getattr(db, "cancellation", None)
                        failure = error
                    summary = result.get("confirmation_summary", result.get("discovery_summary", {}))
                    row.update(status=result["status"], reason=result.get("reason", ""), nodes=len(graph.nodes), relationships=len(graph.relationships),
                               rows=result.get("rows", ""), ratio=summary.get("ratio", ""), delta_ms=summary.get("delta_ms", ""))
                    # A PROFILE stop must also be archived when the timing result is no_gap.
                    # 即使计时结果为 no_gap，PROFILE 触发的停止也必须归档。
                    if config["run"]["save_all"] or mode == "generate" or failure is not None or result.get("stop_run") or result["status"] in {"candidate", "result_mismatch", "timeout", "backend_rejected"}:
                        directory = output / result["status"] / f"{number:05d}-{pattern.id}-{seed}"
                        save_case(directory, case, graph, source, mutant, result, config, db.version if db else None)
                writer.writerow(row)
                counts[row["status"]] += 1
                stream.flush()
                print(f"{number:05d} {pattern.id} seed={seed} {row['status']} {row.get('reason', '')}", flush=True)
                # Persist the terminal row before exposing the error or stopping the run.
                # 先保存终止用例的汇总行，再抛出错误或停止运行。
                if failure is not None:
                    raise failure
                if result.get("stop_run"):
                    raise RuntimeError("Run stopped after saving the case: query termination is unconfirmed")
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
    language, support = language_for(config)
    if config["run"]["mode"] not in {"experiment", "generate", "check"}:
        raise ValueError("run.mode must be experiment, generate, or check")
    case_seed = config["run"]["case_seed"]
    if case_seed is not None:
        if type(case_seed) is not int or case_seed < 0:
            raise ValueError("run.case_seed must be null or a nonnegative integer")
        config["run"]["rounds"] = 1
    if type(config["run"]["save_all"]) is not bool:
        raise ValueError("run.save_all must be a boolean")
    patterns = language.patterns_for(support)
    for name in config["run"]["patterns"]:
        if name not in patterns:
            raise ValueError(f"Pattern {name} is unavailable for {config['language']}/{config['database']['system']}")
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


def language_for(config):
    backend = select_database(config["database"]["system"])
    if config["language"] == "cypher" and config["database"]["system"] not in {"janusgraph", "hugegraph"}:
        return cypher, backend.SUPPORT
    if config["language"] == "gremlin" and config["database"]["system"] in {"arcadedb", "janusgraph", "hugegraph"}:
        return gremlin, backend.GREMLIN_SUPPORT
    raise ValueError("Unsupported language and database combination")


def schedule(config):
    # Scheduling has its own RNG, so one case's generation does not alter later seeds.
    # 调度使用独立的随机数生成器，单个用例的生成过程不会改变后续种子。
    rng = random.Random(config["run"]["seed"])
    language, support = language_for(config)
    patterns = language.patterns_for(support)
    exact_seed = config["run"]["case_seed"]
    start = time.monotonic()
    for _ in range(config["run"]["rounds"]):
        names = list(config["run"]["patterns"])
        rng.shuffle(names)
        for name in names:
            if time.monotonic() - start >= config["run"]["minutes"] * 60:
                return
            yield patterns[name], exact_seed if exact_seed is not None else rng.randrange(2 ** 32)


def prepare_case(pattern, seed, config):
    rng = random.Random(seed)
    # Generate the host without a pattern ID, then choose a compatible replacement site.
    # 先生成不依赖 Pattern 编号的宿主 AST，再选择兼容的替换位置。
    language, support = language_for(config)
    tree = language.generate_ast(rng, config["generation"], support)
    site = (cypher.choose_site(tree, pattern, rng, support["subquery_call"],
                              support.get("subquery_unwind", True),
                              support.get("persistent_imports", True)) if language is cypher
            else gremlin.choose_site(tree, pattern, rng))
    if site is None:
        raise Inapplicable("No compatible AST position")
    case = pattern.embed(tree, site, seed, rng)
    graph, bindings = build_graph(case, pattern, config["graph"], rng, support, language.register_schema)
    instantiate(case, bindings)
    source = case.query
    # Mutate a copy after binding; both queries retain the same constructed graph.
    # 完成绑定后只变异副本，两条查询共用已经构造好的图。
    mutant = pattern.mutate(copy.deepcopy(case))
    if language is cypher:
        cypher.check_pair(source, mutant, case, support.get("persistent_imports", True))
    else:
        gremlin.check_pair(source, mutant, case)
    render_options = {"groovy": support.get("groovy", False)} if language is gremlin else {}
    return case, graph, language.render(source, **render_options), language.render(mutant, **render_options)


def execute_case(db, graph, source, mutant, config, check_only):
    # Load once for the pair; measurement does not rebuild or modify the graph.
    # 每个查询对只装载一次图，测量期间不重建或修改图数据。
    db.load(graph)
    return run_pair(db, source, mutant, config["measurement"], check_only=check_only)


if __name__ == "__main__":
    main()
