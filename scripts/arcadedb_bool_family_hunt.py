#!/usr/bin/env python3
"""Screen Boolean-equivalent OpenCypher queries against ArcadeDB.

The script is deliberately independent from the production experiment runner:
it creates a fresh database, checks complete query results, alternates A/B
execution order, and records ordinary execution time plus EXPLAIN output.
It is a discovery screen. Candidates still require two fresh-session
validations and upstream deduplication before they become issue drafts.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class RulePair:
    rule_id: str
    theorem: str
    query_a: str
    query_b: str


COUNT_RETURN = "RETURN count(*) AS c"
MATCH = "MATCH (n:Bench)"

RULE_PAIRS = (
    RulePair(
        "de_morgan_predicate",
        "NOT (P OR Q) = (NOT P) AND (NOT Q) in strong Kleene logic",
        f"{MATCH} WHERE n.id = 1 AND n.id <= 1 {COUNT_RETURN}",
        (
            f"{MATCH} WHERE NOT (NOT (n.id = 1) "
            f"OR NOT (n.id <= 1)) {COUNT_RETURN}"
        ),
    ),
    RulePair(
        "case_true_selection",
        "P and CASE WHEN P THEN true ELSE false END select the same rows",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        (
            f"{MATCH} WHERE CASE WHEN n.id = 1 "
            f"THEN true ELSE false END {COUNT_RETURN}"
        ),
    ),
    RulePair(
        "null_predicate_negation",
        "P IS NULL = NOT (P IS NOT NULL)",
        f"{MATCH} WHERE n.maybe IS NULL {COUNT_RETURN}",
        f"{MATCH} WHERE NOT (n.maybe IS NOT NULL) {COUNT_RETURN}",
    ),
    RulePair(
        "negated_inequality",
        "x = c = NOT (x <> c) in three-valued comparison semantics",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        f"{MATCH} WHERE NOT (n.id <> 1) {COUNT_RETURN}",
    ),
    RulePair(
        "numeric_equality_range",
        "x = c = (x >= c AND x <= c) for one finite LONG domain",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        f"{MATCH} WHERE n.id >= 1 AND n.id <= 1 {COUNT_RETURN}",
    ),
    RulePair(
        "singleton_in",
        "x = c = x IN [c] for one literal of the same LONG type",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        f"{MATCH} WHERE n.id IN [1] {COUNT_RETURN}",
    ),
    RulePair(
        "boolean_property_truth",
        "a Boolean property and that property = true select the same rows",
        f"{MATCH} WHERE n.flag = true {COUNT_RETURN}",
        f"{MATCH} WHERE n.flag {COUNT_RETURN}",
    ),
    RulePair(
        "predicate_equal_true",
        "Boolean P = (P = true) in three-valued logic",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        f"{MATCH} WHERE (n.id = 1) = true {COUNT_RETURN}",
    ),
    RulePair(
        "coalesce_false_selection",
        "P and coalesce(P, false) select the same rows",
        f"{MATCH} WHERE n.id = 1 {COUNT_RETURN}",
        f"{MATCH} WHERE coalesce(n.id = 1, false) {COUNT_RETURN}",
    ),
    RulePair(
        "and_over_or_distribution",
        "P AND (Q OR R) = (P AND Q) OR (P AND R) in strong Kleene logic",
        (
            f"{MATCH} WHERE n.id = 1 AND "
            f"(n.group_id = 1 OR n.group_id = 2) {COUNT_RETURN}"
        ),
        (
            f"{MATCH} WHERE (n.id = 1 AND n.group_id = 1) "
            f"OR (n.id = 1 AND n.group_id = 2) {COUNT_RETURN}"
        ),
    ),
)


class ArcadeClient:
    def __init__(self, base_url: str, database: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.session = requests.Session()
        self.session.auth = (
            os.environ.get("ARCADEDB_USER", "root"),
            os.environ.get("ARCADEDB_PASSWORD", "playwithdata"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        language: str | None = None,
        command: str | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] | None = None
        if command is not None:
            payload = {"command": command}
            if language is not None:
                payload["language"] = language
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path}: HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )
        return response.json() if response.content else {}

    def server_version(self) -> str:
        return str(self.request("GET", "/api/v1/server").get("version"))

    def reset_and_load(self, node_count: int) -> None:
        exists = self.request("GET", f"/api/v1/exists/{self.database}").get("result")
        if exists:
            self.request(
                "POST",
                "/api/v1/server",
                command=f"drop database {self.database}",
            )
        self.request(
            "POST",
            "/api/v1/server",
            command=f"create database {self.database}",
        )
        for command in (
            "CREATE VERTEX TYPE Bench",
            "CREATE PROPERTY Bench.id LONG",
            "CREATE PROPERTY Bench.group_id LONG",
            "CREATE PROPERTY Bench.maybe LONG",
            "CREATE PROPERTY Bench.flag BOOLEAN",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="sql",
                command=command,
            )
        for lower in range(1, node_count + 1, 5_000):
            upper = min(lower + 4_999, node_count)
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="opencypher",
                command=(
                    f"UNWIND range({lower}, {upper}) AS i "
                    "CREATE (:Bench {"
                    "id: i, group_id: i % 100, "
                    "maybe: CASE WHEN i % 5 = 0 THEN null ELSE i % 10 END, "
                    "flag: CASE WHEN i = 1 THEN true ELSE false END"
                    "}) RETURN count(*) AS c"
                ),
            )
        for command in (
            "CREATE INDEX ON Bench (id) UNIQUE",
            "CREATE INDEX ON Bench (group_id) NOTUNIQUE",
            "CREATE INDEX ON Bench (maybe) NOTUNIQUE",
            "CREATE INDEX ON Bench (flag) NOTUNIQUE",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="sql",
                command=command,
            )

    def execute(self, query: str) -> tuple[float, list[dict[str, Any]]]:
        started = time.perf_counter()
        payload = self.request(
            "POST",
            f"/api/v1/query/{self.database}",
            language="opencypher",
            command=query,
        )
        elapsed_ms = (time.perf_counter() - started) * 1_000.0
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise RuntimeError("ArcadeDB result is not a complete row list")
        return elapsed_ms, result

    def explain(self, query: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/query/{self.database}",
            language="opencypher",
            command=f"EXPLAIN {query}",
        )


def pair_schedule(pair_count: int, seed: int) -> list[str]:
    schedule = ["AB" if index % 2 == 0 else "BA" for index in range(pair_count)]
    random.Random(seed).shuffle(schedule)
    return schedule


def negative_controls(
    client: ArcadeClient,
    query: str,
    *,
    side: str,
    pair_count: int,
    seed: int,
) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for order in pair_schedule(pair_count, seed):
        if order == "AB":
            first_ms, first_result = client.execute(query)
            second_ms, second_result = client.execute(query)
        else:
            second_ms, second_result = client.execute(query)
            first_ms, first_result = client.execute(query)
        if first_result != second_result:
            raise RuntimeError(f"{side}/{side}: result changed during control")
        controls.append(
            {
                "side": side,
                "order": order,
                "first_ms": first_ms,
                "second_ms": second_ms,
                "absolute_delta_ms": abs(second_ms - first_ms),
            }
        )
    return controls


def quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def bootstrap_interval(
    values: list[float],
    *,
    seed: int,
    resamples: int = 10_000,
) -> dict[str, float]:
    source = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sample = [values[source.randrange(len(values))] for _ in values]
        estimates.append(statistics.median(sample))
    return {"low": quantile(estimates, 0.025), "high": quantile(estimates, 0.975)}


def screen_pair(
    client: ArcadeClient,
    pair: RulePair,
    *,
    warmups: int,
    pair_count: int,
    control_pairs: int,
    seed: int,
) -> dict[str, Any]:
    initial_a = client.execute(pair.query_a)[1]
    initial_b = client.execute(pair.query_b)[1]
    equivalent = initial_a == initial_b
    if not equivalent:
        return {
            "rule": asdict(pair),
            "complete_typed_equivalent": False,
            "result_a": initial_a,
            "result_b": initial_b,
            "verdict": "LOGICAL_CANDIDATE",
        }

    for _ in range(warmups):
        client.execute(pair.query_a)
        client.execute(pair.query_b)

    samples: list[dict[str, Any]] = []
    for order in pair_schedule(pair_count, seed):
        if order == "AB":
            a_ms, result_a = client.execute(pair.query_a)
            b_ms, result_b = client.execute(pair.query_b)
        else:
            b_ms, result_b = client.execute(pair.query_b)
            a_ms, result_a = client.execute(pair.query_a)
        if result_a != result_b:
            raise RuntimeError(f"{pair.rule_id}: result changed during timing")
        samples.append({"order": order, "a_ms": a_ms, "b_ms": b_ms})

    a_values = [float(item["a_ms"]) for item in samples]
    b_values = [float(item["b_ms"]) for item in samples]
    median_a = statistics.median(a_values)
    median_b = statistics.median(b_values)
    ratio = median_b / median_a if median_a else float("inf")
    delta = median_b - median_a
    paired_ratios = [b / a for a, b in zip(a_values, b_values, strict=True) if a > 0]
    paired_deltas = [b - a for a, b in zip(a_values, b_values, strict=True)]
    direction = sum(b > a for a, b in zip(a_values, b_values, strict=True)) / len(samples)
    significant = ratio >= 5.0 and delta >= 5.0 and direction >= 0.8
    controls = {"aa": [], "bb": []}
    negative_control_p99_ms = None
    ratio_interval = None
    delta_interval = None
    issue_grade_gate = None
    if control_pairs:
        controls = {
            "aa": negative_controls(
                client,
                pair.query_a,
                side="a",
                pair_count=control_pairs,
                seed=seed + 10_000,
            ),
            "bb": negative_controls(
                client,
                pair.query_b,
                side="b",
                pair_count=control_pairs,
                seed=seed + 20_000,
            ),
        }
        control_deltas = [
            float(item["absolute_delta_ms"])
            for item in (*controls["aa"], *controls["bb"])
        ]
        negative_control_p99_ms = quantile(control_deltas, 0.99)
        ratio_interval = bootstrap_interval(paired_ratios, seed=seed + 30_000)
        delta_interval = bootstrap_interval(paired_deltas, seed=seed + 40_000)
        issue_grade_gate = (
            ratio_interval["low"] >= 5.0
            and delta_interval["low"] >= 5.0
            and delta_interval["low"] > negative_control_p99_ms
            and direction >= 0.9
        )
    return {
        "rule": asdict(pair),
        "complete_typed_equivalent": True,
        "result_a": initial_a,
        "result_b": initial_b,
        "warmups_per_query": warmups,
        "balanced_pairs": pair_count,
        "samples": samples,
        "negative_controls": controls,
        "summary": {
            "median_a_ms": median_a,
            "median_b_ms": median_b,
            "median_b_over_a": ratio,
            "median_delta_ms": delta,
            "b_slower_fraction": direction,
            "paired_ratio_ci95": ratio_interval,
            "paired_delta_ms_ci95": delta_interval,
            "negative_control_p99_ms": negative_control_p99_ms,
            "issue_grade_runtime_gate_passed": issue_grade_gate,
        },
        "explain_a": client.explain(pair.query_a),
        "explain_b": client.explain(pair.query_b),
        "verdict": "PERFORMANCE_CANDIDATE" if significant else "NO_SIGNIFICANT_DIFFERENCE",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-count", type=int, default=100_000)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--control-pairs", type=int, default=0)
    parser.add_argument("--session", type=int)
    parser.add_argument(
        "--rule-id",
        action="append",
        dest="rule_ids",
        choices=tuple(pair.rule_id for pair in RULE_PAIRS),
    )
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument(
        "--url",
        default=os.environ.get("ARCADEDB_URL", "http://127.0.0.1:2482"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = ArcadeClient(args.url, args.database)
    client.reset_and_load(args.node_count)
    evidence = {
        "schema_version": 1,
        "purpose": "discovery_screen_not_issue_grade_validation",
        "arcadedb_url": args.url,
        "arcadedb_version": client.server_version(),
        "database": args.database,
        "node_count": args.node_count,
        "session": args.session,
        "setup": {
            "vertex_type": "Bench",
            "properties": ["id LONG", "group_id LONG", "maybe LONG", "flag BOOLEAN"],
            "indexes": [
                "Bench.id UNIQUE",
                "Bench.group_id NOTUNIQUE",
                "Bench.maybe NOTUNIQUE",
                "Bench.flag NOTUNIQUE",
            ],
            "maybe_null_frequency": "one in five vertices",
        },
        "results": [
            screen_pair(
                client,
                pair,
                warmups=args.warmups,
                pair_count=args.pairs,
                control_pairs=args.control_pairs,
                seed=args.seed + (args.session or 0) * 1_000 + index,
            )
            for index, pair in enumerate(RULE_PAIRS)
            if args.rule_ids is None or pair.rule_id in args.rule_ids
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                item["rule"]["rule_id"]: {
                    "verdict": item["verdict"],
                    "summary": item.get("summary"),
                }
                for item in evidence["results"]
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
