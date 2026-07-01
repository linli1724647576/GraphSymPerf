#!/usr/bin/env python3
"""Replay a GraphSymPerf candidate on an amplified GDsmith graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from graph_sym_perf.adapters.neo4j import _neo4j_plan
from graph_sym_perf.core import TargetSystem, stable_json_dumps, to_jsonable
from graph_sym_perf.plan import diff_normalized_plans, normalize_plan


ID_PROP = re.compile(r"\bid\s*:\s*(-?\d+)")
DESTRUCTIVE_REPLAY_ENV = "GRAPHSYMPERF_REPLAY_ALLOW_DESTRUCTIVE"


@dataclass(frozen=True)
class QuerySpec:
    name: str
    path: Path
    query: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one GraphSymPerf candidate with amplified graph copies.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    _require_destructive_replay_guard()
    uri = _required_env("GRAPHSYMPERF_NEO4J_URI")
    user = _required_env("GRAPHSYMPERF_NEO4J_USER")
    password = _required_env("GRAPHSYMPERF_NEO4J_PASSWORD")
    database = os.environ.get("GRAPHSYMPERF_NEO4J_DATABASE") or None

    run_dir = args.run_dir
    case_dir = run_dir / "neo4j" / args.case_id
    output_dir = args.output_dir or run_dir / "replay" / f"{args.case_id}_scale{args.scale}"
    output_dir.mkdir(parents=True, exist_ok=True)

    seed = QuerySpec("seed", case_dir / "slow_query.cypher", _read_query(case_dir / "slow_query.cypher"))
    variant_id = _fast_variant_id(case_dir / "case.json") or "variant"
    variant = QuerySpec(variant_id, case_dir / "fast_query.cypher", _read_query(case_dir / "fast_query.cypher"))
    graph_setup = _build_amplified_graph(run_dir / "gdsmith_graph.cypher", args.scale)
    (output_dir / "amplified_graph.cypher").write_text(graph_setup, encoding="utf-8")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) if database else driver.session() as session:
            _reset_database(session)
            load_stats = _load_graph(session, graph_setup, timeout=args.timeout)
            graph_counts = _graph_counts(session)
            warmup = [_profile_once(session, spec, timeout=args.timeout) for spec in (seed, variant) for _ in range(args.warmup)]
            samples = _paired_profile(session, seed, variant, repeats=args.repeats, timeout=args.timeout)
    finally:
        driver.close()

    seed_samples = [sample for sample in samples if sample["name"] == seed.name]
    variant_samples = [sample for sample in samples if sample["name"] == variant.name]
    seed_summary = _summarize(seed_samples)
    variant_summary = _summarize(variant_samples)
    equivalence = _equivalence(seed_samples, variant_samples)
    plan_diff = diff_normalized_plans(
        variant_samples[-1]["normalized_plan_object"],
        seed_samples[-1]["normalized_plan_object"],
    )

    payload = {
        "case_id": args.case_id,
        "scale": args.scale,
        "repeats": args.repeats,
        "warmup_count": args.warmup,
        "environment": {
            "neo4j_uri": uri,
            "database": database,
        },
        "graph": {
            "load_stats": load_stats,
            "counts": graph_counts,
            "setup_artifact": _display_path(output_dir / "amplified_graph.cypher"),
        },
        "queries": {
            "seed": {
                "path": _display_path(seed.path),
                "query": seed.query,
            },
            "variant": {
                "variant_id": variant.name,
                "path": _display_path(variant.path),
                "query": variant.query,
            },
        },
        "equivalence": equivalence,
        "summaries": {
            "seed": seed_summary,
            "variant": variant_summary,
        },
        "deltas": _deltas(fast=variant_summary, slow=seed_summary),
        "plan_diff": to_jsonable(plan_diff),
        "warmup_samples": [_public_sample(sample) for sample in warmup],
        "samples": [_public_sample(sample) for sample in samples],
    }
    _write_json(output_dir / "replay_summary.json", payload)
    _write_json(output_dir / "plan_diff.json", to_jsonable(plan_diff))
    _write_json(output_dir / "seed_profile_last.json", seed_samples[-1]["profile"])
    _write_json(output_dir / "variant_profile_last.json", variant_samples[-1]["profile"])
    print(json.dumps(_compact_result(payload), ensure_ascii=False, indent=2))
    return 0


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _require_destructive_replay_guard() -> None:
    if os.environ.get(DESTRUCTIVE_REPLAY_ENV) != "1":
        raise SystemExit(
            f"refusing to reset Neo4j database; set {DESTRUCTIVE_REPLAY_ENV}=1 "
            "only for a disposable replay database"
        )


def _read_query(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip().rstrip(";")


def _fast_variant_id(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    for section in ("oracle", "confirmation", "exploratory_oracle", "exploratory_confirmation"):
        value = payload.get(section)
        if isinstance(value, Mapping):
            variant_id = value.get("fast_variant_id")
            if isinstance(variant_id, str) and variant_id:
                return variant_id
    return None


def _split_statements(text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        current.append(stripped)
        if stripped.endswith(";"):
            statement = " ".join(current).rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    if current:
        statements.append(" ".join(current).strip())
    return statements


def _build_amplified_graph(graph_path: Path, scale: int) -> str:
    if scale < 1:
        raise SystemExit("--scale must be >= 1")
    statements = _split_statements(graph_path.read_text(encoding="utf-8"))
    index_statements = [statement for statement in statements if statement.upper().startswith("CREATE INDEX")]
    data_statements = [statement for statement in statements if not statement.upper().startswith("CREATE INDEX")]
    max_id = max((int(match.group(1)) for statement in data_statements for match in ID_PROP.finditer(statement)), default=0)
    stride = max_id + 1 + 1000
    expanded: list[str] = []
    expanded.extend(index_statements)
    for copy_index in range(scale):
        offset = copy_index * stride
        for statement in data_statements:
            expanded.append(ID_PROP.sub(lambda match, offset=offset: f"id : {int(match.group(1)) + offset}", statement))
    return "".join(f"{statement};\n" for statement in expanded)


def _reset_database(session: Any) -> None:
    indexes = list(session.run("SHOW INDEXES YIELD name, type RETURN name, type"))
    for record in indexes:
        if str(record["type"]).upper() == "LOOKUP":
            continue
        name = str(record["name"]).replace("`", "``")
        session.run(f"DROP INDEX `{name}` IF EXISTS").consume()
    session.run("MATCH (n) DETACH DELETE n").consume()


def _load_graph(session: Any, graph_setup: str, *, timeout: float) -> dict[str, Any]:
    from neo4j import Query

    statements = _split_statements(graph_setup)
    index_count = 0
    data_count = 0
    started = perf_counter()
    for statement in statements:
        if statement.upper().startswith("CREATE INDEX"):
            index_count += 1
        else:
            data_count += 1
        session.run(Query(statement, timeout=timeout)).consume()
    session.run(Query("CALL db.awaitIndexes()", timeout=timeout)).consume()
    return {
        "statement_count": len(statements),
        "index_statement_count": index_count,
        "data_statement_count": data_count,
        "elapsed_ms": (perf_counter() - started) * 1000.0,
    }


def _graph_counts(session: Any) -> dict[str, int]:
    node_count = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
    rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()["count"]
    return {"nodes": int(node_count), "relationships": int(rel_count)}


def _paired_profile(session: Any, seed: QuerySpec, variant: QuerySpec, *, repeats: int, timeout: float) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(repeats):
        ordered = (seed, variant) if index % 2 == 0 else (variant, seed)
        for spec in ordered:
            sample = _profile_once(session, spec, timeout=timeout)
            sample["pair_index"] = index
            samples.append(sample)
    return samples


def _profile_once(session: Any, spec: QuerySpec, *, timeout: float) -> dict[str, Any]:
    from neo4j import Query

    started = perf_counter()
    result = session.run(Query(f"PROFILE {spec.query}", timeout=timeout))
    row_counter: Counter[str] = Counter()
    sample_rows: list[Mapping[str, Any]] = []
    row_count = 0
    for record in result:
        row = to_jsonable(record.data())
        key = stable_json_dumps(row)
        row_counter[key] += 1
        row_count += 1
        if len(sample_rows) < 5:
            sample_rows.append(row)
    summary = result.consume()
    elapsed_ms = (perf_counter() - started) * 1000.0
    raw_profile = _neo4j_plan(summary.profile)
    normalized = normalize_plan(TargetSystem.NEO4J, raw_profile)
    return {
        "name": spec.name,
        "elapsed_ms": elapsed_ms,
        "result": {
            "row_count": row_count,
            "hash": _counter_hash(row_counter),
            "sample_rows": sample_rows,
        },
        "metrics": dict(normalized.metrics),
        "structural_hash": normalized.structural_hash,
        "profile": {
            "raw_profile": raw_profile,
            "normalized": to_jsonable(normalized),
        },
        "normalized_plan_object": normalized,
    }


def _counter_hash(counter: Counter[str]) -> str:
    payload = stable_json_dumps(sorted(counter.items())).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(sample["elapsed_ms"]) for sample in samples]
    metric_names = sorted({name for sample in samples for name in sample["metrics"]})
    metrics = {
        name: _numeric_summary([float(sample["metrics"][name]) for sample in samples if name in sample["metrics"]])
        for name in metric_names
    }
    return {
        "sample_count": len(samples),
        "elapsed_ms": _numeric_summary(elapsed),
        "result_hashes": sorted({sample["result"]["hash"] for sample in samples}),
        "row_counts": sorted({sample["result"]["row_count"] for sample in samples}),
        "structural_hashes": sorted({sample["structural_hash"] for sample in samples}),
        "metrics": metrics,
    }


def _numeric_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "max": None, "mean": None, "stdev": None}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def _equivalence(seed_samples: list[dict[str, Any]], variant_samples: list[dict[str, Any]]) -> dict[str, Any]:
    seed_hashes = sorted({sample["result"]["hash"] for sample in seed_samples})
    variant_hashes = sorted({sample["result"]["hash"] for sample in variant_samples})
    seed_counts = sorted({sample["result"]["row_count"] for sample in seed_samples})
    variant_counts = sorted({sample["result"]["row_count"] for sample in variant_samples})
    return {
        "passed": seed_hashes == variant_hashes and seed_counts == variant_counts,
        "seed_result_hashes": seed_hashes,
        "variant_result_hashes": variant_hashes,
        "seed_row_counts": seed_counts,
        "variant_row_counts": variant_counts,
    }


def _deltas(*, fast: Mapping[str, Any], slow: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "elapsed_ms": _ratio_delta(fast["elapsed_ms"]["median"], slow["elapsed_ms"]["median"]),
        "metrics": {},
    }
    for metric in sorted(set(fast["metrics"]) | set(slow["metrics"])):
        fast_value = (fast["metrics"].get(metric) or {}).get("median")
        slow_value = (slow["metrics"].get(metric) or {}).get("median")
        result["metrics"][metric] = _ratio_delta(fast_value, slow_value)
    return result


def _ratio_delta(fast_value: float | None, slow_value: float | None) -> dict[str, float | None]:
    if fast_value is None or slow_value is None:
        return {"fast": fast_value, "slow": slow_value, "ratio": None, "delta": None}
    return {
        "fast": fast_value,
        "slow": slow_value,
        "ratio": slow_value / max(fast_value, 0.001),
        "delta": slow_value - fast_value,
    }


def _public_sample(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": sample["name"],
        "pair_index": sample.get("pair_index"),
        "elapsed_ms": sample["elapsed_ms"],
        "result": sample["result"],
        "metrics": sample["metrics"],
        "structural_hash": sample["structural_hash"],
    }


def _compact_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": payload["case_id"],
        "scale": payload["scale"],
        "graph": payload["graph"]["counts"],
        "equivalence": payload["equivalence"],
        "elapsed_delta": payload["deltas"]["elapsed_ms"],
        "db_hits_delta": payload["deltas"]["metrics"].get("db_hits"),
        "rows_delta": payload["deltas"]["metrics"].get("rows"),
        "memory_delta": payload["deltas"]["metrics"].get("memory_bytes"),
        "output": str(Path(payload["graph"]["setup_artifact"]).parents[0] / "replay_summary.json"),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
