#!/usr/bin/env python3
"""Replay historical Neo4j typed-result mismatches on a disposable instance.

The script reconstructs the exact certificate-bound query pairs from one
schema-v3 run, reloads that run's graph snapshot, and prints only deterministic
complete-result mismatches.  It is intentionally separate from the runtime
issue oracle: a result mismatch is a correctness lead, not a performance hit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from graph_sym_perf.core import TargetSystem, to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    RulePlanEntry,
    RuleStream,
)
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.workload.rule_pairs import RulePairFactory


ENTRY_RULE_ID = "edge_notation_symmetry"
MAX_RESULT_ROWS = 100_000


def _entry_from_payload(payload: dict[str, Any]) -> RulePlanEntry:
    values = dict(payload)
    values["phase"] = CampaignPhase(values["phase"])
    values["target"] = TargetSystem(values["target"])
    values["stream"] = RuleStream(values["stream"])
    return RulePlanEntry(**values)


def _entry_directory(run_dir: Path, entry_id: str) -> Path:
    entries = run_dir / "entries"
    matches = []
    for path in entries.iterdir():
        progress_path = path / "acceptance_progress.json"
        if not path.is_dir() or not progress_path.is_file():
            continue
        if json.loads(progress_path.read_text())["entry_id"] == entry_id:
            matches.append(path)
    if len(matches) != 1:
        raise RuntimeError(f"expected one entry directory for {entry_id}, found {len(matches)}")
    return matches[0]


def _collect(session: Any, query: str, timeout_seconds: int) -> tuple[tuple[Any, ...], bool]:
    from neo4j import Query

    result = session.run(Query(query, timeout=timeout_seconds))
    rows: list[Any] = []
    for record in result:
        rows.append(to_typed_value(record.data()))
        if len(rows) > MAX_RESULT_ROWS:
            return tuple(rows), True
    result.consume()
    return tuple(rows), False


def _reset_and_load(driver: Any, statements: list[str]) -> None:
    with driver.session(database="neo4j") as session:
        session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS").consume()
        for statement in statements:
            session.run(statement).consume()


def _repeat_mismatch(
    driver: Any,
    query_a: str,
    query_b: str,
    *,
    ordered: bool,
    timeout_seconds: int,
    rounds: int = 3,
) -> tuple[bool, list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for round_index in range(rounds):
            ordered_queries = (("A", query_a), ("B", query_b))
            if round_index % 2:
                ordered_queries = tuple(reversed(ordered_queries))
            outputs: dict[str, tuple[Any, ...]] = {}
            truncated = False
            for name, query in ordered_queries:
                rows, was_truncated = _collect(session, query, timeout_seconds)
                outputs[name] = rows
                truncated = truncated or was_truncated
            comparison = compare_typed_results(outputs["A"], outputs["B"], ordered=ordered)
            observations.append(
                {
                    "round": round_index + 1,
                    "execution_order": "".join(name for name, _query in ordered_queries),
                    "a_rows": len(outputs["A"]),
                    "b_rows": len(outputs["B"]),
                    "a_preview": to_typed_value(outputs["A"][:3]),
                    "b_preview": to_typed_value(outputs["B"][:3]),
                    "equivalent": comparison.equivalent,
                    "truncated": truncated,
                }
            )
    deterministic = all(not item["equivalent"] and not item["truncated"] for item in observations)
    return deterministic, observations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7701"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    plan = json.loads((args.run_dir / "rule_plan.json").read_text())
    entries = [entry for entry in plan["entries"] if entry["rule_id"] == ENTRY_RULE_ID]
    if len(entries) != 1:
        raise RuntimeError(f"expected one {ENTRY_RULE_ID} entry, found {len(entries)}")
    entry = _entry_from_payload(entries[0])
    entry_dir = _entry_directory(args.run_dir, entry.entry_id)
    corpus = json.loads((entry_dir / "generation_corpus.json").read_text())
    progress = json.loads((entry_dir / "acceptance_progress.json").read_text())

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    try:
        _reset_and_load(driver, corpus["graph_statements"])
        factory = RulePairFactory(RuleRegistry.default())
        mismatch_count = 0
        deterministic_count = 0
        errors: list[dict[str, Any]] = []
        for ordinal, query in enumerate(
            corpus["query_texts"][: int(progress["last_query_ordinal"])], start=1
        ):
            build = factory.build(query, entry, TargetSystem.NEO4J)
            if build.pair is None:
                continue
            pair = build.pair
            ordered = bool(re.search(r"\bORDER\s+BY\b", pair.query_a, re.IGNORECASE))
            try:
                with driver.session(database="neo4j") as session:
                    left, left_truncated = _collect(
                        session, pair.query_a, entry.query_timeout_seconds
                    )
                    right, right_truncated = _collect(
                        session, pair.query_b, entry.query_timeout_seconds
                    )
                if left_truncated or right_truncated:
                    continue
                comparison = compare_typed_results(left, right, ordered=ordered)
                if comparison.equivalent or not comparison.complete_result:
                    continue
                mismatch_count += 1
                deterministic, observations = _repeat_mismatch(
                    driver,
                    pair.query_a,
                    pair.query_b,
                    ordered=ordered,
                    timeout_seconds=entry.query_timeout_seconds,
                    rounds=args.repeat,
                )
                deterministic_count += int(deterministic)
                print(
                    json.dumps(
                        {
                            "run_dir": str(args.run_dir),
                            "ordinal": ordinal,
                            "pair_hash": pair.pair_hash,
                            "certificate_path": pair.certificate.ast_path,
                            "deterministic": deterministic,
                            "query_a": pair.query_a,
                            "query_b": pair.query_b,
                            "observations": observations,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
            except Exception as exc:  # keep the scan going; errors are reported separately
                errors.append(
                    {
                        "ordinal": ordinal,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        print(
            json.dumps(
                {
                    "summary": {
                        "run_dir": str(args.run_dir),
                        "historical_mismatch_count": progress["reject_counts"].get(
                            "TYPED_EQUIVALENCE_FAILED", 0
                        ),
                        "current_mismatch_count": mismatch_count,
                        "deterministic_mismatch_count": deterministic_count,
                        "errors": errors,
                    }
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
