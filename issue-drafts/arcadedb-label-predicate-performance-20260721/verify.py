#!/usr/bin/env python3
"""Validate the label_where_symmetry ArcadeDB performance candidate.

The framework catalogs this rule as experimental, so this verifier records the
certificate and rule boundary but performs the surface rewrite explicitly.  It
uses fresh databases, complete result comparison, balanced timings, and A/A +
B/B negative controls.
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
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


RULE_ID = "label_where_symmetry"
QUERY_A = "MATCH (n:Parent) RETURN n.id AS id ORDER BY id"
QUERY_B = "MATCH (n) WHERE n:Parent RETURN n.id AS id ORDER BY id"


class ArcadeClient:
    def __init__(self, base_url: str, database: str, unrelated_count: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.unrelated_count = unrelated_count
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
        timeout: float = 120.0,
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

    def reset_and_load(self) -> None:
        exists = self.request("GET", f"/api/v1/exists/{self.database}").get("result")
        if exists:
            self.request("POST", "/api/v1/server", command=f"drop database {self.database}")
        self.request("POST", "/api/v1/server", command=f"create database {self.database}")
        for command in (
            "CREATE VERTEX TYPE Parent",
            "CREATE PROPERTY Parent.id LONG",
            "CREATE VERTEX TYPE Child EXTENDS Parent",
            "CREATE VERTEX TYPE Other",
            "CREATE PROPERTY Other.id LONG",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="sql",
                command=command,
            )
        for command in (
            "CREATE (:Parent {id: 1}) RETURN count(*) AS c",
            "CREATE (:Child {id: 2}) RETURN count(*) AS c",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="opencypher",
                command=command,
            )
        for lower in range(1, self.unrelated_count + 1, 5_000):
            upper = min(lower + 4_999, self.unrelated_count)
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="opencypher",
                command=(
                    f"UNWIND range({lower}, {upper}) AS i "
                    "CREATE (:Other {id: i}) RETURN count(*) AS c"
                ),
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


def pair_schedule(seed: int) -> list[str]:
    schedule = ["AB", "BA"] * 10
    random.Random(seed).shuffle(schedule)
    return schedule


def timed_pair(client: ArcadeClient, order: str) -> dict[str, Any]:
    if order == "AB":
        a_ms, result_a = client.execute(QUERY_A)
        b_ms, result_b = client.execute(QUERY_B)
    else:
        b_ms, result_b = client.execute(QUERY_B)
        a_ms, result_a = client.execute(QUERY_A)
    if result_a != result_b:
        raise RuntimeError("complete typed result mismatch during timing")
    return {"order": order, "a_ms": a_ms, "b_ms": b_ms}


def negative_controls(
    client: ArcadeClient,
    query: str,
    side: str,
    seed: int,
) -> list[dict[str, Any]]:
    schedule = ["12", "21"] * 5
    random.Random(seed).shuffle(schedule)
    controls: list[dict[str, Any]] = []
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
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--session", type=int, choices=(1, 2), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unrelated-count", type=int, default=100_000)
    parser.add_argument(
        "--url",
        default=os.environ.get("ARCADEDB_URL", "http://127.0.0.1:2482"),
    )
    args = parser.parse_args()

    rule = RuleRegistry.default().get(RULE_ID)
    certificates = enumerate_safety_points(
        QUERY_A,
        rule,
        "arcadedb-label-where-symmetry-v1",
    )
    certificate = next(
        item for item in certificates if item.site_class == "match_pattern_component"
    )
    client = ArcadeClient(args.url, args.database, args.unrelated_count)
    client.reset_and_load()

    graph_counts = {
        label: client.execute(f"MATCH (n:{label}) RETURN count(*) AS c")[1]
        for label in ("Parent", "Child", "Other")
    }
    result_a = client.execute(QUERY_A)[1]
    result_b = client.execute(QUERY_B)[1]
    if result_a != result_b:
        raise RuntimeError("complete typed result mismatch before timing")

    for _ in range(5):
        client.execute(QUERY_A)
        client.execute(QUERY_B)
    samples = [
        timed_pair(client, order)
        for order in pair_schedule(20260721 + args.session)
    ]
    aa = negative_controls(client, QUERY_A, "a", 51000 + args.session)
    bb = negative_controls(client, QUERY_B, "b", 52000 + args.session)

    pairs = [(float(item["a_ms"]), float(item["b_ms"])) for item in samples]
    ratio_ci = bootstrap_ratio_ci(
        pairs,
        resamples=10_000,
        seed=53000 + args.session,
    )
    delta_ci = bootstrap_delta_ci(
        pairs,
        slower_side="b",
        resamples=10_000,
        seed=54000 + args.session,
    )
    negative_deltas = [
        float(item["absolute_delta_ms"])
        for item in (*aa, *bb)
    ]
    summary = {
        "median_a_ms": statistics.median(a for a, _ in pairs),
        "median_b_ms": statistics.median(b for _, b in pairs),
        "median_ratio": statistics.median(b / a for a, b in pairs),
        "median_delta_ms": statistics.median(b - a for a, b in pairs),
        "ratio_ci_low": ratio_ci.low,
        "ratio_ci_high": ratio_ci.high,
        "delta_ci_low_ms": delta_ci.low,
        "delta_ci_high_ms": delta_ci.high,
        "direction_consistency": sum(b > a for a, b in pairs) / len(pairs),
        "negative_control_p99_ms": percentile(negative_deltas, 0.99),
    }
    evidence = {
        "schema_version": 1,
        "session": args.session,
        "database": args.database,
        "url": args.url,
        "unrelated_node_count": args.unrelated_count,
        "schema": {
            "Parent": "vertex type with id LONG",
            "Child": "vertex type extends Parent",
            "Other": "unrelated vertex type with id LONG",
        },
        "graph_counts": graph_counts,
        "rule_id": RULE_ID,
        "rule_tier": rule.tier.value,
        "submit_eligible": rule.submit_eligible,
        "proof_id": rule.proof_id,
        "proof_theorem": rule.proof_theorem,
        "certificate": dataclasses.asdict(certificate),
        "manual_review_constraints": [
            "ordinary mandatory MATCH",
            "single static label",
            "same bound variable and projection",
            "no optional match or multi-label expression",
        ],
        "query_a": QUERY_A,
        "query_b": QUERY_B,
        "result_a": result_a,
        "result_b": result_b,
        "complete_typed_equivalent": result_a == result_b,
        "warmups_per_query": 5,
        "balanced_pairs": len(samples),
        "samples": samples,
        "negative_controls": {"aa": aa, "bb": bb},
        "summary": summary,
        "explain_a": client.explain(QUERY_A),
        "explain_b": client.explain(QUERY_B),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
