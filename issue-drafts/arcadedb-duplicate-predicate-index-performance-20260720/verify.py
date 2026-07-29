#!/usr/bin/env python3
"""Independent ArcadeDB runtime check for a certified Boolean rule.

Each invocation creates a fresh database, rebuilds the same typed schema and
LSM index, derives query B through GraphSymPerf's certificate-bound rule,
checks complete results, and records balanced A/B timings plus A/A and B/B
negative controls.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

import requests

from graph_sym_perf.runtime.statistics import bootstrap_delta_ci, bootstrap_ratio_ci
from graph_sym_perf.transforms.engine import apply_certified_rule_with_proof
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


QUERY_A = "MATCH (n:Bench) WHERE n.id = 1 RETURN count(*) AS c"
DEFAULT_RULE_ID = "duplicate_predicate_control"
NODE_COUNT = 20_000


class ArcadeClient:
    def __init__(self, base_url: str, database: str, node_count: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.node_count = node_count
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
        timeout: float = 45.0,
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
                f"{method} {path}: HTTP {response.status_code}: {response.text[:2000]}"
            )
        return response.json() if response.content else {}

    def reset_and_load(self) -> None:
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
        commands = (
            "CREATE VERTEX TYPE Bench",
            "CREATE PROPERTY Bench.id LONG",
            "CREATE INDEX ON Bench (id) UNIQUE",
        )
        for command in commands:
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="sql",
                command=command,
            )
        for lower in range(1, self.node_count + 1, 2_000):
            upper = min(lower + 1_999, self.node_count)
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="opencypher",
                command=(
                    f"UNWIND range({lower}, {upper}) AS i "
                    "CREATE (:Bench {id: i}) RETURN count(*) AS c"
                ),
            )

    def execute(self, query: str) -> tuple[float, list[dict[str, Any]]]:
        start = time.perf_counter()
        payload = self.request(
            "POST",
            f"/api/v1/query/{self.database}",
            language="opencypher",
            command=query,
        )
        elapsed_ms = (time.perf_counter() - start) * 1_000.0
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


def transformed_query(rule_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    rule = RuleRegistry.default().get(rule_id)
    certificates = enumerate_safety_points(
        QUERY_A,
        rule,
        f"arcadedb-{rule_id}-index-v1",
    )
    certificate = next(item for item in certificates if item.ast_path.endswith(".where"))
    transformed = apply_certified_rule_with_proof(QUERY_A, rule, certificate)
    return (
        transformed.query,
        dataclasses.asdict(transformed.certificate),
        dict(transformed.semantic_proof),
    )


def pair_schedule(seed: int) -> list[str]:
    schedule = ["AB", "BA"] * 10
    random.Random(seed).shuffle(schedule)
    return schedule


def timed_pair(
    client: ArcadeClient,
    query_a: str,
    query_b: str,
    order: str,
) -> dict[str, Any]:
    if order == "AB":
        a_ms, result_a = client.execute(query_a)
        b_ms, result_b = client.execute(query_b)
    else:
        b_ms, result_b = client.execute(query_b)
        a_ms, result_a = client.execute(query_a)
    if result_a != result_b:
        raise RuntimeError("complete typed result mismatch during timing")
    return {"order": order, "a_ms": a_ms, "b_ms": b_ms}


def negative_controls(
    client: ArcadeClient,
    query: str,
    side: str,
    seed: int,
) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    schedule = ["12", "21"] * 5
    random.Random(seed).shuffle(schedule)
    for order in schedule:
        if order == "12":
            first_ms, first_result = client.execute(query)
            second_ms, second_result = client.execute(query)
        else:
            second_ms, second_result = client.execute(query)
            first_ms, first_result = client.execute(query)
        if first_result != second_result:
            raise RuntimeError(f"{side}/{side} negative-control result mismatch")
        controls.append(
            {
                "side": side,
                "order": order,
                "first_ms": first_ms,
                "second_ms": second_ms,
                "absolute_delta_ms": abs(first_ms - second_ms),
            }
        )
    return controls


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--session", type=int, choices=(1, 2), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rule-id", default=DEFAULT_RULE_ID)
    parser.add_argument("--node-count", type=int, default=NODE_COUNT)
    parser.add_argument(
        "--url",
        default=os.environ.get("ARCADEDB_URL", "http://127.0.0.1:2482"),
    )
    args = parser.parse_args()

    rule = RuleRegistry.default().get(args.rule_id)
    query_b, certificate, semantic_proof = transformed_query(args.rule_id)
    client = ArcadeClient(args.url, args.database, args.node_count)
    client.reset_and_load()

    count_result = client.execute("MATCH (n:Bench) RETURN count(*) AS c")[1]
    result_a = client.execute(QUERY_A)[1]
    result_b = client.execute(query_b)[1]
    if result_a != result_b:
        raise RuntimeError("complete typed result mismatch before timing")

    for _ in range(5):
        client.execute(QUERY_A)
        client.execute(query_b)

    samples = [
        timed_pair(client, QUERY_A, query_b, order)
        for order in pair_schedule(20260720 + args.session)
    ]
    aa = negative_controls(client, QUERY_A, "a", 31000 + args.session)
    bb = negative_controls(client, query_b, "b", 32000 + args.session)

    a_values = [float(item["a_ms"]) for item in samples]
    b_values = [float(item["b_ms"]) for item in samples]
    slower_side = "a" if statistics.median(a_values) > statistics.median(b_values) else "b"
    oriented = [
        (float(item["a_ms"]), float(item["b_ms"]))
        if slower_side == "b"
        else (float(item["b_ms"]), float(item["a_ms"]))
        for item in samples
    ]
    ratio_ci = bootstrap_ratio_ci(
        oriented,
        resamples=10_000,
        seed=41000 + args.session,
    )
    delta_ci = bootstrap_delta_ci(
        oriented,
        slower_side="b",
        resamples=10_000,
        seed=42000 + args.session,
    )
    negative_deltas = [
        float(item["absolute_delta_ms"]) for item in (*aa, *bb)
    ]
    summary = {
        "median_a_ms": statistics.median(a_values),
        "median_b_ms": statistics.median(b_values),
        "slower_side": slower_side,
        "median_ratio": statistics.median(slow / fast for fast, slow in oriented),
        "median_delta_ms": statistics.median(slow - fast for fast, slow in oriented),
        "ratio_ci_low": ratio_ci.low,
        "ratio_ci_high": ratio_ci.high,
        "delta_ci_low_ms": delta_ci.low,
        "delta_ci_high_ms": delta_ci.high,
        "direction_consistency": sum(slow > fast for fast, slow in oriented) / len(oriented),
        "negative_control_p99_ms": percentile(negative_deltas, 0.99),
    }
    evidence = {
        "schema_version": 1,
        "session": args.session,
        "database": args.database,
        "url": args.url,
        "node_count": args.node_count,
        "setup": {
            "vertex_type": "Bench",
            "property": "Bench.id LONG",
            "index": "CREATE INDEX ON Bench (id) UNIQUE",
            "load": "UNWIND range(...) CREATE (:Bench {id: i})",
        },
        "rule_id": args.rule_id,
        "rule_tier": rule.tier.value,
        "submit_eligible": rule.submit_eligible,
        "query_a": QUERY_A,
        "query_b": query_b,
        "certificate": certificate,
        "semantic_proof": semantic_proof,
        "graph_count_result": count_result,
        "result_a": result_a,
        "result_b": result_b,
        "complete_typed_equivalent": result_a == result_b,
        "warmups_per_query": 5,
        "balanced_pairs": len(samples),
        "samples": samples,
        "negative_controls": {"aa": aa, "bb": bb},
        "summary": summary,
        "explain_a": client.explain(QUERY_A),
        "explain_b": client.explain(query_b),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
