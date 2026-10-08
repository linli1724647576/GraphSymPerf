"""Generate, check, and replay cost-aware Cypher query pairs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import random
import sys
import tempfile
import time
import uuid

import yaml

from executor import measurement_settings, run_case, supported_patterns
from generator import PatternNotApplicable, instantiate, load_seeds
from mutator import PATTERNS, mutate
from query import parse_query, render_query, split_statements


ROOT = Path(__file__).resolve().parent.parent


def read_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("configuration must be a YAML mapping")
    database = config.get("database")
    if not isinstance(database, dict):
        raise ValueError("database configuration is required")
    if database.get("dedicated") is not True or database.get("allow_reset") is not True:
        raise ValueError("use a dedicated test database and set database.allow_reset: true")
    patterns = config.get("patterns", list(PATTERNS))
    if not isinstance(patterns, list) or not patterns or any(p not in PATTERNS for p in patterns):
        raise ValueError("patterns must contain supported IDs P1–P16 or S1–S10")
    config["patterns"] = list(dict.fromkeys(patterns))
    for name, default, minimum in (("scales", [100], 1), ("seeds", [0], 0)):
        values = config.get(name, default)
        if not isinstance(values, list) or not values or any(type(v) is not int or v < minimum for v in values):
            raise ValueError(f"{name} must be a nonempty list of integers >= {minimum}")
        config[name] = list(dict.fromkeys(values))
    database.setdefault("dbms", "neo4j")
    if database["dbms"] not in {"neo4j", "memgraph", "falkordb"}:
        raise ValueError("database.dbms must be neo4j, memgraph, or falkordb")
    config.setdefault("schedule_seed", 0)
    if type(config["schedule_seed"]) is not int or config["schedule_seed"] < 0:
        raise ValueError("schedule_seed must be a nonnegative integer")
    pattern_budgets(config)
    generation = config.setdefault("generation", {})
    if not isinstance(generation, dict):
        raise ValueError("generation must be a mapping")
    files = generation.get("log_files", [])
    if not isinstance(files, list) or any(not isinstance(p, str) for p in files):
        raise ValueError("generation.log_files must be a list of paths")
    base = path.resolve().parent
    generation["log_files"] = [str((base / p).resolve()) for p in files]
    generation.setdefault("data_seed", 0)
    if type(generation["data_seed"]) is not int or generation["data_seed"] < 0:
        raise ValueError("generation.data_seed must be a nonnegative integer")
    if "cwd" in generation:
        generation["cwd"] = str((base / generation["cwd"]).resolve())
    config["output"] = str((base / config.get("output", "results")).resolve())
    if not isinstance(config.get("measurement", {}), dict):
        raise ValueError("measurement must be a mapping")
    config["measurement"] = measurement_settings(config.get("measurement", {}))
    return config


def case_digest(case: dict, format_version: int = 2) -> str:
    fields = ("pattern", "seed", "scale", "setup", "query_a", "query_b", "parameters", "checks", "expected_rows")
    if format_version >= 2:
        fields += ("query_ref", "template_id", "bindings", "reference_side", "required_cypher", "data_seed")
    data = {name: case.get(name) for name in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def save_case(directory: Path, case: dict, measurement: dict, result: dict, *, format_version: int = 2) -> None:
    if format_version not in {1, 2} or format_version == 1 and case.get("query_ref"):
        raise ValueError("invalid input format for reference query")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "setup.cypher").write_text("\n\n".join(s.rstrip("; \n") + ";" for s in case["setup"]) + "\n", encoding="utf-8")
    for side in ("a", "b"):
        (directory / f"query_{side}.cypher").write_text(case[f"query_{side}"].rstrip("; \n") + ";\n", encoding="utf-8")
    if case.get("query_ref"):
        (directory / "query_ref.cypher").write_text(case["query_ref"].rstrip("; \n") + ";\n", encoding="utf-8")
    record = {
        "format_version": format_version,
        "input_hash": case_digest(case, format_version),
        "has_reference": bool(case.get("query_ref")),
        "case": {key: value for key, value in case.items() if key not in {"setup", "query_a", "query_b", "query_ref"}},
        "measurement": measurement,
        "result": result,
    }
    temporary = directory / "result.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(directory / "result.json")


def read_case(directory: Path) -> tuple[dict, dict]:
    record = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if record.get("format_version") not in {1, 2}:
        raise ValueError("unsupported result format")
    case = record["case"]
    case["setup"] = list(split_statements((directory / "setup.cypher").read_text(encoding="utf-8")))
    for side in (("a", "b", "ref") if record.get("has_reference") else ("a", "b")):
        statements = split_statements((directory / f"query_{side}.cypher").read_text(encoding="utf-8"))
        if len(statements) != 1:
            raise ValueError("each query file must contain one read query")
        case[f"query_{side}"] = statements[0]
        parse_query(statements[0])
    if case_digest(case, record["format_version"]) != record.get("input_hash"):
        raise ValueError("saved inputs have changed; the recorded cost conditions no longer describe this case")
    return case, record["measurement"]


def new_run_directory(output: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(output) / f"{stamp}-{uuid.uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def pattern_budgets(config: dict) -> dict:
    """Reserve time and cases for every pattern; slow early patterns cannot spend them."""
    patterns = config["patterns"]
    per_cases = config.get("cases_per_pattern")
    if per_cases is None:
        total = config.get("max_cases", 20 * len(patterns))
        if type(total) is not int or total < len(patterns):
            raise ValueError("max_cases must reserve at least one case per pattern; select fewer patterns")
        quotas = {pattern: total // len(patterns) + (index < total % len(patterns))
                  for index, pattern in enumerate(patterns)}
    else:
        if type(per_cases) is not int or per_cases <= 0:
            raise ValueError("cases_per_pattern must be a positive integer")
        quotas = dict.fromkeys(patterns, per_cases)
    seconds = config.get("seconds_per_pattern")
    if seconds is None:
        total_seconds = config.get("max_seconds", 120 * len(patterns))
        if type(total_seconds) not in (int, float) or not 0 < total_seconds < float("inf"):
            raise ValueError("max_seconds must be positive and finite")
        seconds = total_seconds / len(patterns)
    if type(seconds) not in (int, float) or not 0 < seconds < float("inf"):
        raise ValueError("seconds_per_pattern must be positive and finite")
    return {pattern: {"max_cases": int(quotas[pattern]), "max_seconds": seconds} for pattern in patterns}


def write_run(directory: Path, record: dict) -> None:
    temporary = directory / "run.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(directory / "run.json")


def prepare_case(source, pattern, scale, seed, data_seed):
    case = instantiate(source, pattern, scale, seed, data_seed=data_seed)
    query_a = parse_query(case["query_a"]) if isinstance(case["query_a"], str) else case["query_a"]
    query_b = mutate(query_a, pattern, bindings=case.get("bindings"))
    case.update(pattern=pattern, seed=seed, scale=scale,
                query_a=render_query(query_a), query_b=render_query(query_b))
    if case.get("query_ref") is not None and not isinstance(case["query_ref"], str):
        case["query_ref"] = render_query(case["query_ref"])
    return case


def run(config: dict) -> int:
    generation = config.get("generation", {})
    sources = load_seeds(generation) if generation.get("log_files") or generation.get("command") else [None]
    directory = new_run_directory(config["output"])
    measurement = config.get("measurement", {})
    dbms = config["database"].get("dbms", "neo4j")
    budgets = pattern_budgets(config)
    record = {"format_version": 1, "kind": "discovery", "dbms": dbms, "database_version": "unknown",
              "patterns": config["patterns"], "budgets": budgets, "schedule_seed": config.get("schedule_seed", 0),
              "per_pattern": {pattern: {"attempted": 0, "executed": 0, "elapsed_seconds": 0.0,
                                         "stop_reason": "pending"} for pattern in config["patterns"]}}
    write_run(directory, record)
    completed, skipped = 0, 0
    seen = set()
    state_cache = {}
    order = list(config["patterns"])
    random.Random(config.get("schedule_seed", 0)).shuffle(order)
    supported = supported_patterns(config["database"])
    for pattern in order:
        stats = record["per_pattern"][pattern]
        if pattern not in supported:
            stats["stop_reason"] = "unsupported_feature"
            write_run(directory, record)
            continue
        started = time.monotonic()
        stats["stop_reason"] = "running"
        for scale, seed in ((scale, seed) for scale in config["scales"] for seed in config["seeds"]):
            if stats["executed"] >= budgets[pattern]["max_cases"]:
                stats["stop_reason"] = "case_budget"
                break
            if time.monotonic() - started >= budgets[pattern]["max_seconds"]:
                stats["stop_reason"] = "time_budget"
                break
            stats["attempted"] += 1
            case = None
            offset = seed % len(sources)
            for source in sources[offset:] + sources[:offset]:
                try:
                    case = prepare_case(source, pattern, scale, seed, generation.get("data_seed", 0))
                    break
                except PatternNotApplicable:
                    skipped += 1
            if case is None:
                continue
            digest = case_digest(case)
            if digest in seen:
                continue
            seen.add(digest)
            case_dir = directory / f"{completed + 1:04d}-{pattern}-n{scale}-{digest[:8]}"
            save_case(case_dir, case, measurement, {"status": "started", "dbms": dbms})
            result = run_case(case, config["database"], measurement, state_cache=state_cache)
            result.setdefault("dbms", dbms)
            save_case(case_dir, case, measurement, result)
            completed += 1
            stats["executed"] += 1
            stats["elapsed_seconds"] = time.monotonic() - started
            if result.get("database_version"):
                record["database_version"] = result["database_version"]
            write_run(directory, record)
            print(f"{pattern} scale={scale} seed={seed}: {result['status']} — {case_dir}", flush=True)
            if result.get("stop_target"):
                stats["stop_reason"] = "target_error"
                for pending in record["per_pattern"].values():
                    if pending["stop_reason"] == "pending":
                        pending["stop_reason"] = "target_stopped"
                write_run(directory, record)
                print("Stopped: the database cannot safely continue this run.", file=sys.stderr)
                return 1
        if stats["stop_reason"] == "running":
            stats["stop_reason"] = "inputs_exhausted"
        stats["elapsed_seconds"] = time.monotonic() - started
        write_run(directory, record)
    print(f"Finished: {completed} cases, {skipped} inapplicable; {directory}")
    return 0 if completed else 1


def replay(directory: Path, config: dict) -> int:
    case, measurement = read_case(directory)
    # Retain legacy identity too: upgrading the hash scheme would turn the
    # same historical input into another case in pattern contribution reports.
    format_version = json.loads((directory / "result.json").read_text(encoding="utf-8"))["format_version"]
    # Replaying never invokes the generator or overwrites the original evidence.
    output = new_run_directory(config["output"]) / f"replay-{case['pattern']}-{case_digest(case, format_version)[:8]}"
    dbms = config["database"].get("dbms", "neo4j")
    save_case(output, case, measurement, {"status": "started", "dbms": dbms}, format_version=format_version)
    result = run_case(case, config["database"], measurement)
    result.setdefault("dbms", dbms)
    save_case(output, case, measurement, result, format_version=format_version)
    write_run(output.parent, {"format_version": 1, "kind": "replay", "dbms": result["dbms"],
                              "database_version": result.get("database_version", "unknown"),
                              "patterns": [case["pattern"]], "budgets": {}, "per_pattern": {}})
    print(f"Replay: {result['status']} — {output}")
    return 1 if result.get("stop_target") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cost-aware Cypher query testing (P1–P16, SCor S1–S10).")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--replay", type=Path, help="replay a saved case using the configured test database")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        target = config["database"]
        lock_id = hashlib.sha256(f"{target.get('dbms', 'neo4j')}|{target['uri']}|{target['database']}".encode()).hexdigest()[:20]
        with open(Path(tempfile.gettempdir()) / f"graphsymperf-{lock_id}.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another GraphSymPerf process is using this database") from None
            return replay(args.replay, config) if args.replay else run(config)
    except (ValueError, OSError, KeyError, yaml.YAMLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Replay the saved case to restart its measurement.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
