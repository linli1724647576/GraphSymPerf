#!/usr/bin/env python3
"""Find deterministic result differences between Cypher 5 and Cypher 25.

This is a correctness screen over a frozen, read-only GDsmith corpus.  A query
is a lead only when both language versions complete without truncation and
their complete typed results differ. Leads are replayed in alternating version
order to exclude execution-order effects.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


LANGUAGES = ("5", "25")


def collect(
    session: Any,
    query: str,
    language: str,
    timeout_seconds: int,
    max_rows: int,
) -> tuple[tuple[Any, ...], bool]:
    from neo4j import Query

    result = session.run(
        Query(f"CYPHER {language} {query}", timeout=timeout_seconds)
    )
    rows: list[Any] = []
    for record in result:
        rows.append(to_typed_value(record.data()))
        if len(rows) > max_rows:
            return tuple(rows), True
    result.consume()
    return tuple(rows), False


def reset_and_load(driver: Any, statements: list[str]) -> None:
    with driver.session(database="neo4j") as session:
        session.run(
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS"
        ).consume()
        for statement in statements:
            session.run(statement).consume()


def deterministic_replay(
    driver: Any,
    query: str,
    *,
    timeout_seconds: int,
    max_rows: int,
    rounds: int,
) -> tuple[bool, list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for round_index in range(rounds):
            language_order = LANGUAGES if round_index % 2 == 0 else tuple(reversed(LANGUAGES))
            outputs: dict[str, tuple[Any, ...]] = {}
            truncated = False
            for language in language_order:
                rows, was_truncated = collect(
                    session, query, language, timeout_seconds, max_rows
                )
                outputs[language] = rows
                truncated = truncated or was_truncated
            comparison = compare_typed_results(
                outputs["5"], outputs["25"], ordered=False
            )
            observations.append(
                {
                    "round": round_index + 1,
                    "language_order": list(language_order),
                    "cypher_5_rows": len(outputs["5"]),
                    "cypher_25_rows": len(outputs["25"]),
                    "cypher_5_preview": outputs["5"][:3],
                    "cypher_25_preview": outputs["25"][:3],
                    "equivalent": comparison.equivalent,
                    "truncated": truncated,
                }
            )
    deterministic = all(
        not item["equivalent"] and not item["truncated"] for item in observations
    )
    return deterministic, observations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-rows", type=int, default=20_000)
    parser.add_argument("--repeat", type=int, default=4)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    corpus = json.loads(args.corpus.read_text())
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    errors: Counter[str] = Counter()
    screened = 0
    truncated = 0
    leads = 0
    deterministic = 0
    try:
        reset_and_load(driver, corpus["graph_statements"])
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "WHERE name = 'Neo4j Kernel' "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            print(json.dumps({"environment": component.data()}, sort_keys=True), flush=True)

        for ordinal, query in enumerate(corpus["query_texts"][: args.queries], start=1):
            outputs: dict[str, tuple[Any, ...]] = {}
            query_truncated = False
            try:
                with driver.session(database="neo4j") as session:
                    for language in LANGUAGES:
                        rows, was_truncated = collect(
                            session, query, language, args.timeout, args.max_rows
                        )
                        outputs[language] = rows
                        query_truncated = query_truncated or was_truncated
            except Exception as exc:
                errors[f"{type(exc).__name__}: {str(exc)[:160]}"] += 1
                continue
            if query_truncated:
                truncated += 1
                continue
            screened += 1
            comparison = compare_typed_results(
                outputs["5"], outputs["25"], ordered=False
            )
            if comparison.equivalent:
                continue
            leads += 1
            is_deterministic, observations = deterministic_replay(
                driver,
                query,
                timeout_seconds=args.timeout,
                max_rows=args.max_rows,
                rounds=args.repeat,
            )
            deterministic += int(is_deterministic)
            print(
                json.dumps(
                    {
                        "ordinal": ordinal,
                        "deterministic": is_deterministic,
                        "query": query,
                        "observations": observations,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                flush=True,
            )
        print(
            json.dumps(
                {
                    "summary": {
                        "requested_queries": args.queries,
                        "screened_complete_queries": screened,
                        "truncated_queries": truncated,
                        "language_difference_leads": leads,
                        "deterministic_language_differences": deterministic,
                        "errors": dict(errors),
                    }
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
