#!/usr/bin/env python3
"""Fresh-database verifier for the scalar double-NOT WHERE bug."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import requests

from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import enumerate_safety_points


RULE_ID = "double_negation_predicate"
QUERY_A = (
    "MATCH (n:BoolCase) WHERE n.flag "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_B = (
    "MATCH (n:BoolCase) WHERE NOT NOT n.flag "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_FULLY_PARENTHESIZED = (
    "MATCH (n:BoolCase) WHERE NOT (NOT (n.flag)) "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_WORKING_CONTROL = (
    "MATCH (n:BoolCase) WHERE NOT (NOT n.flag) "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_SINGLE_NOT = (
    "MATCH (n:BoolCase) WHERE NOT n.flag "
    "RETURN n.id AS id ORDER BY id"
)
QUERY_PROJECTION = (
    "MATCH (n:BoolCase) "
    "RETURN n.id AS id, n.flag AS flag, "
    "NOT NOT n.flag AS bare, "
    "NOT (NOT n.flag) AS parenthesized, "
    "NOT (NOT (n.flag)) AS fully_parenthesized "
    "ORDER BY id"
)


class ArcadeClient:
    def __init__(self, url: str, database: str) -> None:
        self.url = url.rstrip("/")
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
    ) -> dict[str, Any]:
        payload: dict[str, Any] | None = None
        if command is not None:
            payload = {"command": command}
            if language is not None:
                payload["language"] = language
        response = self.session.request(
            method,
            f"{self.url}{path}",
            json=payload,
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path}: HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )
        return response.json() if response.content else {}

    def reset_and_load(self) -> None:
        if self.request("GET", f"/api/v1/exists/{self.database}").get("result"):
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
            "CREATE VERTEX TYPE BoolCase",
            "CREATE PROPERTY BoolCase.id LONG",
            "CREATE PROPERTY BoolCase.flag BOOLEAN",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="sql",
                command=command,
            )
        for command in (
            "CREATE (:BoolCase {id: 1, flag: true}) RETURN count(*) AS c",
            "CREATE (:BoolCase {id: 2, flag: false}) RETURN count(*) AS c",
            "CREATE (:BoolCase {id: 3}) RETURN count(*) AS c",
        ):
            self.request(
                "POST",
                f"/api/v1/command/{self.database}",
                language="opencypher",
                command=command,
            )

    def query(self, command: str) -> list[dict[str, Any]]:
        result = self.request(
            "POST",
            f"/api/v1/query/{self.database}",
            language="opencypher",
            command=command,
        ).get("result", [])
        if not isinstance(result, list):
            raise RuntimeError("result is not a complete row list")
        return result

    def explain(self, command: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/query/{self.database}",
            language="opencypher",
            command=f"EXPLAIN {command}",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--session", type=int, choices=(1, 2), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--url",
        default=os.environ.get("ARCADEDB_URL", "http://127.0.0.1:2482"),
    )
    args = parser.parse_args()

    rule = RuleRegistry.default().get(RULE_ID)
    certificate = next(
        item
        for item in enumerate_safety_points(
            QUERY_A,
            rule,
            "arcadedb-double-negation-scalar-v1",
        )
        if item.ast_path.endswith(".where")
    )
    client = ArcadeClient(args.url, args.database)
    client.reset_and_load()

    queries = {
        "original": QUERY_A,
        "double_not_bare": QUERY_B,
        "double_not_fully_parenthesized": QUERY_FULLY_PARENTHESIZED,
        "double_not_working_control": QUERY_WORKING_CONTROL,
        "single_not": QUERY_SINGLE_NOT,
        "projection_context": QUERY_PROJECTION,
        "constant_true_bare": "WITH true AS p WHERE NOT NOT p RETURN p",
        "constant_false_bare": "WITH false AS p WHERE NOT NOT p RETURN p",
        "constant_true_control": "WITH true AS p WHERE NOT (NOT p) RETURN p",
        "constant_false_control": "WITH false AS p WHERE NOT (NOT p) RETURN p",
    }
    repeated_results = {
        name: [client.query(query) for _ in range(5)]
        for name, query in queries.items()
    }
    if any(len({json.dumps(value, sort_keys=True) for value in runs}) != 1
           for runs in repeated_results.values()):
        raise RuntimeError("non-deterministic result during repeated validation")
    results = {name: runs[0] for name, runs in repeated_results.items()}

    evidence = {
        "schema_version": 1,
        "session": args.session,
        "database": args.database,
        "url": args.url,
        "rule_id": RULE_ID,
        "rule_tier": rule.tier.value,
        "submit_eligible": rule.submit_eligible,
        "proof_id": rule.proof_id,
        "proof_theorem": rule.proof_theorem,
        "certificate": dataclasses.asdict(certificate),
        "queries": queries,
        "results": results,
        "repeat_count": 5,
        "deterministic": True,
        "checks": {
            "original_expected_id_1": results["original"] == [{"id": 1, "@props": "id:3"}],
            "bare_double_not_wrong_id_2": results["double_not_bare"] == [{"id": 2, "@props": "id:3"}],
            "bare_matches_single_not": results["double_not_bare"] == results["single_not"],
            "working_control_matches_original": results["double_not_working_control"] == results["original"],
            "fully_parenthesized_matches_original": results["double_not_fully_parenthesized"] == results["original"],
            "projection_context_is_correct": all(
                row.get("bare") == row.get("flag")
                and row.get("parenthesized") == row.get("flag")
                and row.get("fully_parenthesized") == row.get("flag")
                for row in results["projection_context"]
            ),
        },
        "explain_original": client.explain(QUERY_A),
        "explain_double_not_bare": client.explain(QUERY_B),
        "explain_double_not_fully_parenthesized": client.explain(
            QUERY_FULLY_PARENTHESIZED
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence["checks"], sort_keys=True))


if __name__ == "__main__":
    main()
