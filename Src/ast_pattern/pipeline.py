"""Six explicit stages, with bounded attempts and protected stage artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
import platform
import subprocess

from . import METHOD_VERSION
from .contracts import StageFailure, derive_seed, digest, encode


STAGES = ("M1", "M2", "M3", "M4", "M5", "M6")
ARCHIVE_FILES = {
    "M1": "M1-host.json", "M2": "M2-embedding.json", "M3": "M3-construction.json",
    "M4": "M4-instantiation.json", "M5": "M5-pair.json", "M6": "M6-dependencies.json",
}


def source_version():
    root = Path(__file__).resolve().parents[1]
    files = sorted(root.rglob("*.py"))
    hasher = hashlib.sha256()
    for path in files:
        if "__pycache__" not in path.parts:
            hasher.update(str(path.relative_to(root)).encode())
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
    repository = root.parent
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True,
                                  text=True, check=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repository, capture_output=True,
                                   text=True, check=True, timeout=5).stdout)
    except (OSError, subprocess.SubprocessError):
        revision, dirty = "unavailable", None
    return {"git_revision": revision, "working_tree_dirty": dirty,
            "source_sha256": hasher.hexdigest(), "python": platform.python_version()}


def _record(records, stage, data):
    payload = encode(data)
    records.append({"stage": stage, "version": METHOD_VERSION,
                    "input_digest": records[-1]["output_digest"] if records else None,
                    "output_digest": digest(payload), "data": payload})


def generation_options(options=None):
    options = dict(options or {})
    allowed = {"grammar", "capabilities", "ast_budget", "embedding_budget", "solver_budget",
               "max_attempts", "pattern_options", "host_seed", "embed_seed", "require_target_entry"}
    if set(options) - allowed:
        raise ValueError(f"Unknown AST generation options: {sorted(set(options) - allowed)}")
    attempts = options.setdefault("max_attempts", 32)
    if type(attempts) is not int or not 1 <= attempts <= 512:
        raise ValueError("AST generation max_attempts must be between 1 and 512")
    for name in ("host_seed", "embed_seed"):
        if name in options and (type(options[name]) is not int or options[name] < 0):
            raise ValueError(f"{name} must be a nonnegative integer")
    if type(options.setdefault("require_target_entry", True)) is not bool:
        raise ValueError("require_target_entry must be a boolean")
    return options


def generate_case(pattern, scale, seed, data_seed=0, *, options=None):
    from ir import to_data
    from query import render_query
    from .host_generator import generate_random_ast
    from .embedding import embed_patterns
    from .state_builder import construct_graph_state
    from .instantiation import instantiate_query
    from .mutation import mutate_target
    from .dependency_check import check_dependencies

    if type(scale) is not int or scale < 1:
        raise ValueError("scale must be a positive integer")
    for value in (seed, data_seed):
        if type(value) is not int or value < 0:
            raise ValueError("query and data seeds must be nonnegative integers")
    options = generation_options(options)
    failures = []
    for attempt in range(options["max_attempts"]):
        records = []
        try:
            host = generate_random_ast(options.get("grammar"), options.get("capabilities"),
                                       options.get("ast_budget"),
                                       derive_seed(options.get("host_seed", seed), "host", attempt))
            _record(records, "M1", host.to_data())
            local_options = dict(options.get("pattern_options", {}))
            policy = {"pattern": pattern, "options": local_options}
            if "form" in local_options:
                policy["form"] = local_options["form"]
            embedded = embed_patterns(host, selection_policy=policy,
                                      embed_seed=derive_seed(options.get("embed_seed", seed), "embedding", attempt),
                                      budget=options.get("embedding_budget"))
            _record(records, "M2", embedded.to_data())
            state, plan, construction = construct_graph_state(
                embedded, scale, state_seed=data_seed, solver_budget=options.get("solver_budget"))
            _record(records, "M3", {"graph_spec": state.graph_spec, "binding_plan": plan.to_data(),
                                    "construction_record": construction})
            source = instantiate_query(embedded, state, plan)
            _record(records, "M4", {"query_ir": to_data(source.source), "record": source.record})
            pair = mutate_target(source, state, embedded.target_id)
            _record(records, "M5", {"query_ir": {"a": to_data(pair.query_a), "b": to_data(pair.query_b)},
                                    "mutation_record": pair.mutation_record})
            report = check_dependencies(pair, embedded, state)
            if report.static.get("result_relation") != "proved":
                raise StageFailure("M6", "semantic_relation_unknown", "Root result relation is not proved",
                                   evidence=report.static)
            if options["require_target_entry"] and not report.static.get("target_valid", False):
                raise StageFailure("M6", "target_unreachable", "The target envelope has no valid logical input",
                                   evidence=report.static)
            _record(records, "M6", {"static": report.static, "runtime": report.runtime})
        except StageFailure as exc:
            if exc.code in {"invalid_configuration", "invalid_budget", "unknown_pattern", "contract_drift",
                            "unbound_slot", "unexpanded_structure", "out_of_region_mutation"}:
                raise
            failures.append({"attempt": attempt, **exc.to_data(), "stages": records})
            continue
        population = state.population
        checks = [*population.checks, *pair.checks]
        for check in pair.bindings.get("required_state_checks", ()):
            if check not in checks:
                checks.append(check)
        rule = embedded.instances[0]
        direction = pair.mutation_record["direction"]
        case = {
            "method_version": METHOD_VERSION, "generation_method": METHOD_VERSION,
            "source_version": source_version(),
            "pattern_catalog": "paper-v1", "pattern": rule.pattern_id, "paper_pattern": rule.pattern_id,
            "legacy_pattern": pair.bindings.get("legacy_pattern", rule.pattern_id), "pattern_form": rule.form,
            "seed": seed, "data_seed": data_seed, "scale": scale, "parameters": {},
            "source": {"generator": "ast_first", "generation_method": METHOD_VERSION,
                       "grammar_version": host.grammar_version, "host_seed": host.host_seed,
                       "host_digest": host.host_digest, "attempt": attempt},
            "query_ir": {"a": to_data(pair.query_a), "b": to_data(pair.query_b)},
            "query_a": render_query(pair.query_a), "query_b": render_query(pair.query_b),
            "graph_spec": state.graph_spec, "setup": population.setup,
            "checks": checks, "state_checks": state.spec.state_checks(), "index_requirements": state.spec.indexes,
            "expected_rows": population.expected_rows, "bindings": pair.bindings,
            "population_constraints": population.constraints, "population_evidence": population.evidence,
            "construction_record": construction, "instantiation_record": source.record,
            "mutation_record": pair.mutation_record, "mutation_direction": direction,
            "side_roles": {"a": "L", "b": "H"} if direction == "L_to_H" else
                          {"a": "symmetric", "b": "symmetric"},
            "dependency_report": {"static": report.static, "runtime": report.runtime},
            "stage_records": records, "generation_failures": failures, "ast_options": options,
        }
        if direction == "L_to_H":
            case["reference_side"] = "a"
        validate_stage_records(case)
        return case
    raise StageFailure("pipeline", "generation_budget_exhausted", "No accepted AST case within the attempt budget",
                       evidence={"failures": failures})


def validate_stage_records(case):
    """Validate the stage chain and bind its final artifacts to executable inputs."""
    if case.get("method_version") != METHOD_VERSION:
        raise ValueError("Unknown AST method version")
    records = case.get("stage_records", ())
    if not isinstance(records, list) or tuple(r.get("stage") for r in records) != STAGES:
        raise ValueError("AST archive requires all six stage records")
    previous = None
    for record in records:
        if (record.get("version") != METHOD_VERSION or record.get("input_digest") != previous
                or record.get("output_digest") != digest(record.get("data"))):
            raise ValueError(f"AST stage {record.get('stage')} contract changed")
        previous = record["output_digest"]
    if records[1]["data"].get("host") != records[0]["data"]:
        raise ValueError("AST M2 does not consume the archived M1 host")
    construction = records[2]["data"].get("construction_record")
    if construction != case.get("construction_record"):
        raise ValueError("AST construction certificate differs from M3")
    if records[3]["data"].get("record") != case.get("instantiation_record"):
        raise ValueError("AST instantiation contract differs from M4")
    if records[3]["data"].get("query_ir") != records[4]["data"].get("query_ir", {}).get("a"):
        raise ValueError("AST M5 changed the accepted M4 source")
    mutation = records[4]["data"].get("mutation_record", {})
    if mutation != case.get("mutation_record"):
        raise ValueError("AST mutation contract differs from M5")
    direction = mutation.get("direction")
    expected_roles = {"a": "L", "b": "H"} if direction == "L_to_H" else {"a": "symmetric", "b": "symmetric"}
    if (direction not in {"L_to_H", "symmetry"} or case.get("mutation_direction") != direction
            or case.get("side_roles") != expected_roles):
        raise ValueError("AST comparison roles differ from the accepted mutation")
    if records[2]["data"]["graph_spec"] != case.get("graph_spec"):
        raise ValueError("AST graph differs from accepted M3 state")
    if records[4]["data"]["query_ir"] != case.get("query_ir"):
        raise ValueError("AST queries differ from accepted M5 pair")
    if records[5]["data"] != case.get("dependency_report"):
        raise ValueError("AST dependency report differs from M6")
    if records[5]["data"]["static"].get("result_relation") != "proved":
        raise ValueError("AST root semantic relation has not been proved")
    return records


def reproduce_generation(case):
    """Regenerate with the archived configuration and report the first divergent stage."""
    validate_stage_records(case)
    if case.get("source_version", {}).get("source_sha256") != source_version()["source_sha256"]:
        raise ValueError("Generation replay requires the archived source version")
    regenerated = generate_case(case["paper_pattern"], case["scale"], case["seed"], case["data_seed"],
                                options=case["ast_options"])
    for old, new in zip(case["stage_records"], regenerated["stage_records"]):
        if old["output_digest"] != new["output_digest"]:
            raise ValueError(f"Generation replay diverged at {old['stage']}")
    return regenerated
