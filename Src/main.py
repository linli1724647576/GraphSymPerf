"""Generate, check, and replay cost-aware query pairs from shared IR."""

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

from executor import measurement_settings, run_case, supported_patterns, typed_value
from generator import PatternNotApplicable, instantiate, load_seeds
from mutator import PATTERNS, mutate
from query import parse_query, render_query, split_statements
from ir import from_data, to_data
from languages import DBMS, LANGUAGES, UnsupportedFeature, UnsupportedLanguage, generate, render_saved
from paper_patterns import COMPOSITION_PATTERNS, PAPER_CATALOG, LEGACY_CATALOG, compositional_rule


ROOT = Path(__file__).resolve().parent.parent
COMPOSITION_EXTENSION_FIELDS = ("composition", "dependency_analysis", "population_constraints", "population_evidence", "mutation_record",
                                "pattern_catalog", "paper_pattern", "legacy_pattern", "pattern_form",
                                "symbolic_skeleton", "construction_record", "instantiation_record")
CASE_EXTENSION_FIELDS = ("query_ir", "graph_spec", "state_checks", "index_requirements", "language",
                         "target_dbms", "output_columns", "required_features", "compile_error", "ir_hash", "verified_features", "source_input_hash", "query_bindings",
                         *COMPOSITION_EXTENSION_FIELDS)


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
    generation = config.setdefault("generation", {})
    if not isinstance(generation, dict):
        raise ValueError("generation must be a mapping")
    catalog = config.setdefault("pattern_catalog", PAPER_CATALOG if "composition" in generation else LEGACY_CATALOG)
    if catalog not in {PAPER_CATALOG, LEGACY_CATALOG}:
        raise ValueError("pattern_catalog must be paper-v1 or legacy-v1")
    if catalog == PAPER_CATALOG and "composition" not in generation:
        raise ValueError("paper-v1 currently requires generation.composition")
    if "composition" in generation and catalog != PAPER_CATALOG:
        raise ValueError("generation.composition uses paper-v1 identifiers")
    allowed = COMPOSITION_PATTERNS if catalog == PAPER_CATALOG else PATTERNS
    patterns = config.get("patterns", list(allowed))
    if not isinstance(patterns, list) or not patterns or any(p not in allowed for p in patterns):
        raise ValueError(f"patterns must contain supported {catalog} IDs: {', '.join(allowed)}")
    config["patterns"] = list(dict.fromkeys(patterns))
    for name, default, minimum in (("scales", [100], 1), ("seeds", [0], 0)):
        values = config.get(name, default)
        if not isinstance(values, list) or not values or any(type(v) is not int or v < minimum for v in values):
            raise ValueError(f"{name} must be a nonempty list of integers >= {minimum}")
        config[name] = list(dict.fromkeys(values))
    database.setdefault("dbms", "neo4j")
    if database["dbms"] not in DBMS:
        raise ValueError(f"database.dbms must be one of {', '.join(DBMS)}")
    database.setdefault("language", "cypher")
    if database["language"] not in LANGUAGES:
        raise ValueError(f"database.language must be one of {', '.join(LANGUAGES)}")
    verified = database.get("verified_features", [])
    if not isinstance(verified, list) or any(not isinstance(value, str) for value in verified):
        raise ValueError("database.verified_features must be a list of feature names")
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
    if database["dbms"] == "ladybug":
        database["uri"] = str((base / database.get("path", database.get("uri", "../results/ladybug-test"))).resolve())
        database["path"] = database["uri"]
    generation["log_files"] = [str((base / p).resolve()) for p in files]
    generation.setdefault("data_seed", 0)
    if type(generation["data_seed"]) is not int or generation["data_seed"] < 0:
        raise ValueError("generation.data_seed must be a nonnegative integer")
    generation.setdefault("complexity", 0)
    if type(generation["complexity"]) is not int or generation["complexity"] not in {0, 1, 2}:
        raise ValueError("generation.complexity must be 0, 1, or 2")
    if "composition" in generation:
        from composition import composition_options
        # Validate without resolving the per-query seeded syntax choices.
        composition_options(generation["composition"])
    generation.setdefault("state_variant", "original")
    if not isinstance(generation["state_variant"], str) or generation["state_variant"] not in {
            "original", "empty_low", "multi_low", "pruning_path"}:
        raise ValueError("generation.state_variant must be original, empty_low, multi_low, or pruning_path")
    state_patterns = {"empty_low": {"P10", "P11"}, "multi_low": {"P11", "P13"}, "pruning_path": {"P3"}}
    if generation["state_variant"] in state_patterns and any(
            p not in state_patterns[generation["state_variant"]] for p in config["patterns"]):
        raise ValueError(f"{generation['state_variant']} state is not defined for the selected patterns")
    if "composition" in generation and (generation["state_variant"] != "original" or generation["complexity"] != 0):
        raise ValueError("composition requires state_variant: original and complexity: 0; use composition.depth")
    if "cwd" in generation:
        generation["cwd"] = str((base / generation["cwd"]).resolve())
    config["output"] = str((base / config.get("output", "results")).resolve())
    if not isinstance(config.get("measurement", {}), dict):
        raise ValueError("measurement must be a mapping")
    config["measurement"] = measurement_settings(config.get("measurement", {}))
    return config


def case_digest(case: dict, format_version: int | None = None) -> str:
    format_version = format_version or (3 if case.get("query_ir") else 2)
    fields = ("pattern", "seed", "scale", "setup", "query_a", "query_b", "parameters", "checks", "expected_rows")
    if format_version >= 2:
        fields += ("query_ref", "template_id", "bindings", "reference_side", "required_cypher", "data_seed")
        if "complexity" in case:
            fields += ("complexity",)
        if "state_variant" in case:
            fields += ("state_variant",)
    if format_version >= 3:
        fields += ("query_ir", "graph_spec", "state_checks", "index_requirements", "language",
                   "target_dbms", "output_columns", "required_features", "compile_error", "ir_hash")
        if "verified_features" in case:
            fields += ("verified_features",)
        if "source_input_hash" in case:
            fields += ("source_input_hash",)
        if "query_bindings" in case:
            fields += ("query_bindings",)
    data = {name: case.get(name) for name in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def extension_digest(case: dict) -> str | None:
    """Protect added execution metadata without changing legacy input IDs."""
    data = {name: case[name] for name in CASE_EXTENSION_FIELDS if name in case}
    if not data:
        return None
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def save_case(directory: Path, case: dict, measurement: dict, result: dict, *, format_version: int | None = None) -> None:
    format_version = format_version or (3 if case.get("query_ir") else 2)
    if format_version not in {1, 2, 3} or format_version == 1 and case.get("query_ref"):
        raise ValueError("invalid input format for reference query")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "setup.cypher").write_text("\n\n".join(s.rstrip("; \n") + ";" for s in case["setup"]) + "\n", encoding="utf-8")
    suffix = "gremlin" if case.get("language") == "gremlin" else "cypher"
    for side in ("a", "b"):
        (directory / f"query_{side}.{suffix}").write_text(case[f"query_{side}"].rstrip("; \n") + ";\n", encoding="utf-8")
    if case.get("query_ref"):
        (directory / f"query_ref.{suffix}").write_text(case["query_ref"].rstrip("; \n") + ";\n", encoding="utf-8")
    record = {
        "format_version": format_version,
        "input_hash": case_digest(case, format_version),
        "has_reference": bool(case.get("query_ref")),
        "case": {key: value for key, value in case.items() if key not in {"setup", "query_a", "query_b", "query_ref"}},
        "measurement": measurement,
        "result": result,
    }
    extension_hash = extension_digest(case)
    if extension_hash is not None:
        # Write this for format 3 too: changing a record's format does not
        # discard integrity protection for metadata outside a legacy digest.
        record["extension_hash"] = extension_hash
    temporary = directory / "result.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(directory / "result.json")


def read_case(directory: Path) -> tuple[dict, dict]:
    record = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if record.get("format_version") not in {1, 2, 3}:
        raise ValueError("unsupported result format")
    case = record["case"]
    case["setup"] = list(split_statements((directory / "setup.cypher").read_text(encoding="utf-8")))
    for side in (("a", "b", "ref") if record.get("has_reference") else ("a", "b")):
        suffix = "gremlin" if case.get("language") == "gremlin" else "cypher"
        text = (directory / f"query_{side}.{suffix}").read_text(encoding="utf-8")
        statements = (text.strip().rstrip(";"),) if suffix == "gremlin" else split_statements(text)
        if len(statements) != 1:
            raise ValueError("each query file must contain one read query")
        case[f"query_{side}"] = statements[0]
        if record["format_version"] < 3 and not case.get("query_ir"):
            parse_query(statements[0])
    if case_digest(case, record["format_version"]) != record.get("input_hash"):
        raise ValueError("saved inputs have changed; the recorded cost conditions no longer describe this case")
    extension_hash = extension_digest(case)
    if ((extension_hash is not None and record["format_version"] < 3)
            or any(field in case for field in COMPOSITION_EXTENSION_FIELDS)
            or case.get("graph_spec", {}).get("version") == 2
            or "extension_hash" in record):
        if extension_hash != record.get("extension_hash"):
            raise ValueError("saved execution metadata have changed or lack an extension hash")
    if record["format_version"] == 3 and not case.get("query_ir"):
        raise ValueError("format 3 requires query IR")
    if "query_ir" in case:
        sides = {"a", "b", "ref"} if record.get("has_reference") else {"a", "b"}
        if not isinstance(case["query_ir"], dict) or set(case["query_ir"]) != sides:
            raise ValueError("saved query IR must describe exactly the archived query sides")
        for value in case["query_ir"].values():
            from_data(value)
        if not case.get("compile_error"):
            for side in case["query_ir"]:
                compiled = render_saved(from_data(case["query_ir"][side]), case.get("target_dbms", "neo4j"),
                                        case.get("language", "cypher"))
                if compiled.text != case[f"query_{side}"]:
                    raise ValueError("saved compiled query differs from its query IR")
                if case.get("output_columns", {}).get(side) != [list(item) for item in compiled.columns]:
                    raise ValueError("saved compiled metadata differ from their query IR")
                if typed_value(compiled.bindings) != typed_value(case.get("query_bindings", {}).get(side, {})):
                    raise ValueError("saved bindings differ from their query IR")
                # Feature classification may gain new names after an archive
                # was written. Historical flags cannot invent requirements.
                if not set(case.get("required_features", {}).get(side, ())) <= compiled.required_features:
                    raise ValueError("saved compiled metadata differ from their query IR")
            logical = {key: case.get(key) for key in ("query_ir", "graph_spec", "parameters", "expected_rows")}
            digest = hashlib.sha256(json.dumps(logical, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            if case.get("ir_hash") != digest:
                raise ValueError("saved compiled metadata differ from their query IR")
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


def compile_case(case: dict, database: dict) -> dict:
    """Compile preserved IR for one target; unsupported syntax is recorded."""
    target, language = database.get("dbms", "neo4j"), database.get("language", "cypher")
    case.update(target_dbms=target, language=language)
    case.pop("compile_error", None)
    verified = database.get("verified_features", ())
    if verified:
        case["verified_features"] = sorted(set(verified))
    else:
        case.pop("verified_features", None)
    case["output_columns"], case["required_features"] = {}, {}
    case.pop("query_bindings", None)
    if case.get("graph_spec", {}).get("version") == 2 and target != "neo4j":
        case["compile_error"] = {"status": "unsupported_feature",
                                 "reason": "explicit_state_loading_not_verified_for_target"}
        return case
    try:
        compiled = {side: generate(from_data(value), target, language, verified_features=verified)
                    for side, value in case["query_ir"].items()}
    except (UnsupportedFeature, UnsupportedLanguage) as exc:
        case["compile_error"] = {"status": "unsupported_feature", "reason": str(exc)}
        return case
    names = [tuple(logical for _, logical in query.columns) for query in compiled.values()]
    if any(value != names[0] for value in names[1:]):
        case["compile_error"] = {"status": "invalid_case", "reason": "query_output_columns_differ"}
        return case
    for side, query in compiled.items():
        case[f"query_{side}"] = query.text
        case["output_columns"][side] = [list(item) for item in query.columns]
        case["required_features"][side] = sorted(query.required_features)
        if query.bindings:
            case.setdefault("query_bindings", {})[side] = query.bindings
    if (case["query_a"] == case["query_b"] and
            typed_value(case.get("query_bindings", {}).get("a", {})) ==
            typed_value(case.get("query_bindings", {}).get("b", {}))):
        case["compile_error"] = {"status": "unsupported_transform", "reason": "target_erases_query_difference"}
    logical = {key: case.get(key) for key in ("query_ir", "graph_spec", "parameters", "expected_rows")}
    case["ir_hash"] = hashlib.sha256(json.dumps(logical, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return case


def prepare_case(source, pattern, scale, seed, data_seed, complexity=0, state_variant="original", *, database=None,
                 composition=None):
    case = instantiate(source, pattern, scale, seed, data_seed=data_seed, complexity=complexity,
                       state_variant=state_variant, composition=composition)
    query_a = parse_query(case["query_a"]) if isinstance(case["query_a"], str) else case["query_a"]
    if case.get("instantiation_record"):
        query_b = case["query_b"]
        if any(check not in case["checks"] for check in case["bindings"].get("required_state_checks", [])):
            raise PatternNotApplicable("a required construction check was dropped before compilation")
    else:
        query_b = mutate(query_a, case.get("legacy_pattern", pattern), bindings=case.get("bindings"))
    if case.get("composition") is not None and not case.get("instantiation_record"):
        from dependencies import find_mutation_sites, propagate_equivalence
        from executor import typed_bag
        from population import evaluate_query
        from state import GraphSpec
        spec = GraphSpec.from_dict(case["graph_spec"])
        proof_spec = GraphSpec.from_dict(case["population_evidence"]["validation_graph_spec"])
        for tree in (query_a, query_b):
            if typed_bag(evaluate_query(tree, proof_spec)) != typed_bag(case["expected_rows"]):
                raise PatternNotApplicable("local rewrite did not preserve the synthesized result bag")
        # Large states stay symbolic. Their expansion certificate is checked by
        # the population solver; concrete DB checks remain outside query timers.
        if spec.counts()["nodes"] <= 5000:
            if typed_bag(evaluate_query(query_b, spec)) != typed_bag(case["expected_rows"]):
                raise PatternNotApplicable("expanded state changed the synthesized result bag")
        address = tuple(case["bindings"]["mutation_site"])
        site = next(site for site in find_mutation_sites(query_a, case["paper_pattern"], case["pattern_form"])
                    if site.address == address)
        case["mutation_record"] = {"rule": case["pattern_form"], "paper_pattern": case["paper_pattern"],
                                   "site": site.to_dict(),
                                   "propagation": propagate_equivalence(query_a, site),
                                   "relation": "bag_equal_per_binding"}
    case["query_ir"] = {"a": to_data(query_a), "b": to_data(query_b)}
    case.update(pattern=case.get("paper_pattern", pattern), seed=seed, scale=scale,
                query_a=render_query(query_a), query_b=render_query(query_b))
    if case.get("query_ref") is not None and not isinstance(case["query_ref"], str):
        case["query_ir"]["ref"] = to_data(case["query_ref"])
        case["query_ref"] = render_query(case["query_ref"])
    return compile_case(case, database or {})


def run(config: dict) -> int:
    generation = config.get("generation", {})
    sources = load_seeds(generation) if generation.get("log_files") or generation.get("command") else [None]
    directory = new_run_directory(config["output"])
    measurement = config.get("measurement", {})
    dbms = config["database"].get("dbms", "neo4j")
    budgets = pattern_budgets(config)
    record = {"format_version": 1, "kind": "discovery", "dbms": dbms, "language": config["database"].get("language", "cypher"), "database_version": "unknown",
              "patterns": config["patterns"], "pattern_catalog": config.get("pattern_catalog", PAPER_CATALOG if "composition" in generation else LEGACY_CATALOG),
              "budgets": budgets, "schedule_seed": config.get("schedule_seed", 0),
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
        target_pattern = compositional_rule(pattern).legacy_pattern if "composition" in generation else pattern
        if target_pattern not in supported:
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
                    case = prepare_case(source, pattern, scale, seed, generation.get("data_seed", 0),
                                        generation.get("complexity", 0), generation.get("state_variant", "original"),
                                        database=config["database"], composition=generation.get("composition"))
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
    if case.get("query_ir"):
        different_target = (case.get("target_dbms", "neo4j"), case.get("language", "cypher")) != (
            config["database"].get("dbms", "neo4j"), config["database"].get("language", "cypher"))
        different_features = set(case.get("verified_features", ())) != set(config["database"].get("verified_features", ()))
        if different_target or different_features:
            source_hash = case_digest(case, format_version)
            case = compile_case(case, config["database"])
            case["source_input_hash"] = source_hash
            format_version = 3
    # Replaying never invokes the generator or overwrites the original evidence.
    output = new_run_directory(config["output"]) / f"replay-{case['pattern']}-{case_digest(case, format_version)[:8]}"
    dbms = config["database"].get("dbms", "neo4j")
    save_case(output, case, measurement, {"status": "started", "dbms": dbms}, format_version=format_version)
    result = run_case(case, config["database"], measurement)
    result.setdefault("dbms", dbms)
    save_case(output, case, measurement, result, format_version=format_version)
    write_run(output.parent, {"format_version": 1, "kind": "replay", "dbms": result["dbms"],
                              "language": case.get("language", "cypher"),
                              "database_version": result.get("database_version", "unknown"),
                              "patterns": [case["pattern"]], "pattern_catalog": case.get("pattern_catalog", LEGACY_CATALOG),
                              "budgets": {}, "per_pattern": {}})
    print(f"Replay: {result['status']} — {output}")
    return 1 if result.get("stop_target") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cost-aware graph query testing (paper catalog; legacy archive compatibility).")
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
