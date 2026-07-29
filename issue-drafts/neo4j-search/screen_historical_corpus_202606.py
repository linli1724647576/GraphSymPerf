#!/usr/bin/env python3
"""Screen a frozen GDsmith corpus against Neo4j 2026.06 CORE rules.

This is a targeted candidate finder, not a replacement for the formal yield
campaign.  It reuses a content-addressed graph/query corpus, checks complete
typed-result equivalence, then spends balanced timing repetitions only on
wide-recall runtime leads.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time
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


RULE_IDS = (
    "constant_true_disjunction",
    "constant_false_conjunction",
    "null_partition_tautology",
    "null_contradiction",
    "null_complete_excluded_middle",
    "predicate_order_symmetry",
    "label_order_symmetry",
    "pattern_order_symmetry",
)


def entry(rule_id: str, *, quota: int, generated_budget: int) -> RulePlanEntry:
    rule = RuleRegistry.default().get(rule_id)
    return RulePlanEntry(
        entry_id=f"targeted:neo4j:discovery:{rule_id}:1280:0",
        phase=CampaignPhase.P0,
        target=TargetSystem.NEO4J,
        rule_id=rule_id,
        family=rule.family.value,
        tier=rule.tier.value,
        graph_scale=1280,
        shard_index=0,
        shard_seed=20260718,
        accepted_pair_quota=quota,
        max_generated_seed_budget=generated_budget,
        max_variants_per_seed=1,
        proof_version=rule.proof_version,
        query_timeout_seconds=20,
        max_entry_runtime_seconds=7200,
        measurement_protocol_id="targeted-corpus-screen-v1",
        stream=RuleStream.DISCOVERY,
    )


def collect(
    session: Any,
    query: str,
    timeout_seconds: int,
    *,
    max_rows: int,
) -> tuple[tuple[Any, ...], float, bool]:
    from neo4j import Query

    started = time.perf_counter()
    result = session.run(Query(query, timeout=timeout_seconds))
    rows: list[Any] = []
    truncated = False
    for record in result:
        rows.append(to_typed_value(record.data()))
        if len(rows) > max_rows:
            truncated = True
            break
    if not truncated:
        result.consume()
    return tuple(rows), (time.perf_counter() - started) * 1000.0, truncated


def reset_and_load(driver: Any, statements: list[str]) -> None:
    with driver.session(database="neo4j") as session:
        session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS").consume()
        for statement in statements:
            session.run(statement).consume()


def balanced_screen(
    driver: Any,
    query_a: str,
    query_b: str,
    *,
    seed: str,
    warmups: int,
    pairs: int,
    timeout_seconds: int,
    max_rows: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for _ in range(warmups):
            collect(session, query_a, timeout_seconds, max_rows=max_rows)
            collect(session, query_b, timeout_seconds, max_rows=max_rows)
        for pair_index in range(pairs):
            order = ["A", "B"]
            rng.shuffle(order)
            elapsed: dict[str, float] = {}
            for name in order:
                _rows, elapsed[name], truncated = collect(
                    session,
                    query_a if name == "A" else query_b,
                    timeout_seconds,
                    max_rows=max_rows,
                )
                if truncated:
                    raise RuntimeError("screen result truncated")
            samples.append(
                {
                    "pair": pair_index + 1,
                    "order": "".join(order),
                    "a_ms": elapsed["A"],
                    "b_ms": elapsed["B"],
                }
            )
    median_a = statistics.median(sample["a_ms"] for sample in samples)
    median_b = statistics.median(sample["b_ms"] for sample in samples)
    slower_is_a = median_a >= median_b
    fast = min(median_a, median_b)
    slow = max(median_a, median_b)
    return {
        "median_a_ms": median_a,
        "median_b_ms": median_b,
        "median_ratio": slow / max(fast, 0.001),
        "median_delta_ms": slow - fast,
        "slow_query": "A" if slower_is_a else "B",
        "direction_consistency": sum(
            1
            for sample in samples
            if (sample["a_ms"] >= sample["b_ms"]) == slower_is_a
        )
        / len(samples),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--accepted-per-rule", type=int, default=150)
    parser.add_argument("--generated-per-rule", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-rows", type=int, default=20_000)
    parser.add_argument("--screen-pairs", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("issue-drafts/neo4j-search/neo4j-202606-corpus-screen.json"))
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    corpus = json.loads(args.corpus.read_text())
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    factory = RulePairFactory(RuleRegistry.default())
    report: dict[str, Any] = {
        "schema_version": 1,
        "corpus": str(args.corpus),
        "graph_statement_count": len(corpus["graph_statements"]),
        "query_count": len(corpus["query_texts"]),
        "rules": {},
    }
    try:
        reset_and_load(driver, corpus["graph_statements"])
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "WHERE name = 'Neo4j Kernel' "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            report["environment"] = component.data()
        for rule_id in RULE_IDS:
            print(f"screening {rule_id}", flush=True)
            rule_entry = entry(
                rule_id,
                quota=args.accepted_per_rule,
                generated_budget=args.generated_per_rule,
            )
            rule_report: dict[str, Any] = {
                "applicable": 0,
                "accepted": 0,
                "skipped": {},
                "logical_mismatches": [],
                "runtime_leads": [],
            }
            report["rules"][rule_id] = rule_report
            for ordinal, query in enumerate(
                corpus["query_texts"][: args.generated_per_rule], start=1
            ):
                if rule_report["accepted"] >= args.accepted_per_rule:
                    break
                build = factory.build(query, rule_entry, TargetSystem.NEO4J)
                if build.pair is None:
                    reason = build.reject_code or "RULE_NOT_APPLICABLE"
                    rule_report["skipped"][reason] = rule_report["skipped"].get(reason, 0) + 1
                    continue
                rule_report["applicable"] += 1
                pair = build.pair
                ordered = bool(re.search(r"\bORDER\s+BY\b", pair.query_a, re.IGNORECASE))
                try:
                    with driver.session(database="neo4j") as session:
                        rows_a, elapsed_a, truncated_a = collect(
                            session,
                            pair.query_a,
                            args.timeout,
                            max_rows=args.max_rows,
                        )
                        rows_b, elapsed_b, truncated_b = collect(
                            session,
                            pair.query_b,
                            args.timeout,
                            max_rows=args.max_rows,
                        )
                except Exception as exc:
                    reason = f"EXECUTION_{type(exc).__name__}"
                    rule_report["skipped"][reason] = rule_report["skipped"].get(reason, 0) + 1
                    continue
                if truncated_a or truncated_b:
                    rule_report["skipped"]["RESULT_TRUNCATED"] = rule_report["skipped"].get("RESULT_TRUNCATED", 0) + 1
                    continue
                equivalence = compare_typed_results(rows_a, rows_b, ordered=ordered)
                if not equivalence.equivalent:
                    rule_report["logical_mismatches"].append(
                        {
                            "ordinal": ordinal,
                            "pair_hash": pair.pair_hash,
                            "query_a": pair.query_a,
                            "query_b": pair.query_b,
                            "a_rows": rows_a[:20],
                            "b_rows": rows_b[:20],
                            "a_count": len(rows_a),
                            "b_count": len(rows_b),
                        }
                    )
                    print(f"  logical mismatch at ordinal {ordinal}", flush=True)
                    continue
                rule_report["accepted"] += 1
                fast = min(elapsed_a, elapsed_b)
                slow = max(elapsed_a, elapsed_b)
                initial_ratio = slow / max(fast, 0.001)
                initial_delta = slow - fast
                if initial_ratio < 1.5 or initial_delta < 5.0:
                    continue
                try:
                    screen = balanced_screen(
                        driver,
                        pair.query_a,
                        pair.query_b,
                        seed=f"{rule_id}:{pair.pair_hash}",
                        warmups=2,
                        pairs=args.screen_pairs,
                        timeout_seconds=args.timeout,
                        max_rows=args.max_rows,
                    )
                except Exception as exc:
                    rule_report["skipped"][f"SCREEN_{type(exc).__name__}"] = rule_report["skipped"].get(f"SCREEN_{type(exc).__name__}", 0) + 1
                    continue
                if (
                    screen["median_ratio"] >= 1.5
                    and screen["median_delta_ms"] >= 10.0
                    and screen["direction_consistency"] >= 0.75
                ):
                    lead = {
                        "ordinal": ordinal,
                        "pair_hash": pair.pair_hash,
                        "query_a": pair.query_a,
                        "query_b": pair.query_b,
                        "initial_a_ms": elapsed_a,
                        "initial_b_ms": elapsed_b,
                        "screen": screen,
                    }
                    rule_report["runtime_leads"].append(lead)
                    print(
                        f"  runtime lead ordinal={ordinal} ratio={screen['median_ratio']:.2f} delta={screen['median_delta_ms']:.2f}ms",
                        flush=True,
                    )
            print(
                f"  done applicable={rule_report['applicable']} accepted={rule_report['accepted']} mismatches={len(rule_report['logical_mismatches'])} leads={len(rule_report['runtime_leads'])}",
                flush=True,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        print(f"wrote {args.output}", flush=True)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
