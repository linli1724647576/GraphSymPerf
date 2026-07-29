#!/usr/bin/env python3
"""Screen explicit scalar/list/temporal equivalences on Neo4j.

The cases deliberately avoid approximate floating-point identities.  Any lead
is replayed in alternating A/B order and in both Cypher 5 and Cypher 25.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


@dataclass(frozen=True)
class Case:
    case_id: str
    family: str
    theorem: str
    query_a: str
    query_b: str
    candidate_eligible: bool = True


def typed(expression: str) -> str:
    return f"RETURN {expression} AS value, valueType({expression}) AS value_type"


def cases() -> tuple[Case, ...]:
    values = (
        ("zero", "0"),
        ("minus-one", "-1"),
        ("max", "9223372036854775807"),
        ("min", "-9223372036854775808"),
    )
    result: list[Case] = []

    def add(
        case_id: str,
        family: str,
        theorem: str,
        left: str,
        right: str,
        *,
        candidate_eligible: bool = True,
    ) -> None:
        result.append(
            Case(
                case_id,
                family,
                theorem,
                typed(left),
                typed(right),
                candidate_eligible,
            )
        )

    for value_id, value in values:
        add(f"add-zero-{value_id}", "integer_identity", "x + 0 = x", f"{value} + 0", value)
        add(f"subtract-zero-{value_id}", "integer_identity", "x - 0 = x", f"{value} - 0", value)
        add(f"multiply-one-{value_id}", "integer_identity", "x * 1 = x", f"{value} * 1", value)
        add(f"divide-one-{value_id}", "integer_identity", "x / 1 = x", f"{value} / 1", value)
        add(
            f"string-roundtrip-{value_id}",
            "integer_conversion_roundtrip",
            "Parsing the canonical decimal representation recovers the integer",
            f"toInteger(toString({value}))",
            value,
        )
        add(
            f"singleton-membership-{value_id}",
            "singleton_membership",
            "Every non-null value is a member of its singleton list",
            f"{value} IN [{value}]",
            "true",
        )

    add(
        "min-mod-minus-one",
        "integer_remainder",
        "The remainder of integer division by -1 is zero",
        "-9223372036854775808 % -1",
        "0",
    )
    add(
        "min-div-minus-one-vs-negation",
        "integer_division_negation",
        "For integer x, x / -1 and -x have the same value or overflow outcome",
        "-9223372036854775808 / -1",
        "-(-9223372036854775808)",
    )
    add(
        "min-sign",
        "integer_sign",
        "A negative integer has sign -1",
        "sign(-9223372036854775808)",
        "-1",
    )

    strings = (
        ("ascii", "'abcXYZ'"),
        ("bmp", "'汉字é'"),
        ("supplementary", "'🙂a🚀'"),
        ("combining", "'é'"),
    )
    for value_id, value in strings:
        add(f"string-double-reverse-{value_id}", "string_reverse", "reverse(reverse(s)) = s", f"reverse(reverse({value}))", value)
        add(f"string-full-substring-{value_id}", "string_slice", "substring(s, 0, size(s)) = s", f"substring({value}, 0, size({value}))", value)
        add(f"string-left-size-{value_id}", "string_slice", "left(s, size(s)) = s", f"left({value}, size({value}))", value)
        add(f"string-size-alias-{value_id}", "string_size_alias", "char_length(s) = size(s)", f"char_length({value})", f"size({value})")

    list_cases = (
        ("ascending", "reverse(range(-3, 3))", "range(3, -3, -1)"),
        ("stride", "reverse(range(-4, 8, 3))", "range(8, -4, -3)"),
        ("slice", "range(0, 8)[2..6]", "range(2, 5)"),
        ("double-reverse", "reverse(reverse([1, null, 3]))", "[1, null, 3]"),
    )
    for case_id, left, right in list_cases:
        add(f"list-{case_id}", "list_surface", "The two expressions construct the same list", left, right)

    quantifier_lists = ("[]", "[true]", "[false]", "[null]", "[true, false, null]")
    for index, values_expr in enumerate(quantifier_lists):
        add(
            f"none-any-dual-{index}",
            "quantifier_duality",
            "none(P) = NOT any(P) in three-valued logic",
            f"none(x IN {values_expr} WHERE x)",
            f"NOT any(x IN {values_expr} WHERE x)",
        )
        add(
            f"all-none-negated-dual-{index}",
            "quantifier_duality",
            "all(P) = none(NOT P) in three-valued logic",
            f"all(x IN {values_expr} WHERE x)",
            f"none(x IN {values_expr} WHERE NOT x)",
        )

    temporal_cases = (
        ("date-zero", "date('2024-02-29') + duration('P0D')", "date('2024-02-29')"),
        ("datetime-zero", "datetime('2024-02-29T23:59:59Z') + duration('PT0S')", "datetime('2024-02-29T23:59:59+00:00')"),
        ("instant-zone", "datetime('2024-01-01T00:00:00Z')", "datetime('2023-12-31T19:00:00-05:00')"),
        ("date-day-step", "date('2024-02-28') + duration('P1D')", "date('2024-02-29')"),
        ("duration-zero", "duration.between(date('2024-01-01'), date('2024-01-01'))", "duration('P0D')"),
        ("duration-negation", "duration.between(date('2024-01-02'), date('2024-01-01'))", "-duration('P1D')"),
    )
    for case_id, left, right in temporal_cases:
        add(
            f"temporal-{case_id}",
            "temporal_identity",
            "The temporal expressions denote the same value",
            left,
            right,
            # Neo4j driver temporal and duration values are not yet fully
            # canonicalized by this bounded harness. Keep the observations,
            # but do not promote them as DBMS logical candidates.
            candidate_eligible=False,
        )

    add(
        "collect-reverse-order",
        "ordered_collect",
        "Reversing an ascending collection yields the descending collection",
        "reverse([(x IN range(-3, 3)) | x])",
        "[(x IN range(3, -3, -1)) | x]",
    )
    return tuple(result)


def execute(session: Any, language: str, query: str) -> dict[str, Any]:
    from neo4j import Query

    try:
        result = session.run(Query(f"CYPHER {language} {query}", timeout=10))
        rows = tuple(to_typed_value(record.data()) for record in result)
        result.consume()
        return {"status": "ok", "rows": rows}
    except Exception as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "code", None),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }


def equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "error":
        return left.get("error_code") == right.get("error_code")
    return compare_typed_results(left["rows"], right["rows"], ordered=True).equivalent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7702"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--rounds", type=int, default=6)
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    leads = 0
    deterministic = 0
    ineligible_observations = 0
    try:
        for case in cases():
            observations: list[dict[str, Any]] = []
            with driver.session(database="neo4j") as session:
                for language in ("5", "25"):
                    for round_index in range(args.rounds):
                        order = ("A", "B") if round_index % 2 == 0 else ("B", "A")
                        outcomes: dict[str, dict[str, Any]] = {}
                        for side in order:
                            outcomes[side] = execute(
                                session,
                                language,
                                case.query_a if side == "A" else case.query_b,
                            )
                        observations.append(
                            {
                                "language": language,
                                "round": round_index + 1,
                                "order": "".join(order),
                                "equivalent": equivalent(outcomes["A"], outcomes["B"]),
                                "a": outcomes["A"],
                                "b": outcomes["B"],
                            }
                        )
            mismatch = any(not item["equivalent"] for item in observations)
            stable = all(not item["equivalent"] for item in observations)
            eligible_mismatch = mismatch and case.candidate_eligible
            leads += int(eligible_mismatch)
            deterministic += int(stable and case.candidate_eligible)
            ineligible_observations += int(mismatch and not case.candidate_eligible)
            if mismatch:
                print(
                    json.dumps(
                        {
                            "case_id": case.case_id,
                            "family": case.family,
                            "theorem": case.theorem,
                            "query_a": case.query_a,
                            "query_b": case.query_b,
                            "deterministic": stable,
                            "screening_disposition": (
                                "CANDIDATE"
                                if case.candidate_eligible
                                else "INELIGIBLE_UNSUPPORTED_TEMPORAL_CANONICALIZATION"
                            ),
                            "observations": observations,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )
        print(
            json.dumps(
                {
                    "summary": {
                        "cases": len(cases()),
                        "leads": leads,
                        "deterministic": deterministic,
                        "ineligible_observations": ineligible_observations,
                    }
                },
                sort_keys=True,
                default=str,
            ),
            flush=True,
        )
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
