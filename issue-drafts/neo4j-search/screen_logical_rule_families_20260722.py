#!/usr/bin/env python3
"""Screen Neo4j logical metamorphic pairs with complete typed results.

The self-test intentionally compares different constants so the command can
prove that the loop is red-capable before real rule families are added.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

from graph_sym_perf.core import to_artifact_jsonable, to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


@dataclass(frozen=True)
class Case:
    case_id: str
    rule_id: str
    theorem: str
    query_a: str
    query_b: str
    setup: tuple[str, ...] = ()
    language: str = "25"
    ordered: bool = False


SELF_TEST = Case(
    case_id="feedback-loop-sentinel",
    rule_id="sentinel",
    theorem="the sentinel is intentionally not equivalent",
    query_a="RETURN 1 AS value",
    query_b="RETURN 2 AS value",
)


def logical_cases() -> tuple[Case, ...]:
    graph = (
        "CREATE (a:N {id: 1, p: 1}), "
        "(b:N {id: 2, p: 2}), "
        "(c:N {id: 3, p: 3}), "
        "(d:N {id: 4}), "
        "(a)-[:R {id: 11}]->(b), "
        "(a)-[:R {id: 12}]->(b), "
        "(b)-[:R {id: 13}]->(c), "
        "(c)-[:R {id: 14}]->(a), "
        "(a)-[:S {id: 15}]->(c), "
        "(d)-[:R {id: 16}]->(d)",
    )
    return (
        Case(
            "filter-list-null",
            "filter_with_where_symmetry",
            "FILTER is equivalent to WITH * WHERE for null-aware list rows",
            "UNWIND [null, 0, 1, 2] AS x "
            "FILTER x IS NULL OR x > 1 RETURN x AS value",
            "UNWIND [null, 0, 1, 2] AS x "
            "WITH * WHERE x IS NULL OR x > 1 RETURN x AS value",
        ),
        Case(
            "filter-after-optional",
            "filter_with_where_symmetry",
            "FILTER is equivalent to WITH * WHERE after OPTIONAL MATCH",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) "
            "FILTER m IS NULL OR m.p = 3 RETURN n.id AS n, m.id AS m",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) "
            "WITH * WHERE m IS NULL OR m.p = 3 RETURN n.id AS n, m.id AS m",
            setup=graph,
        ),
        Case(
            "filter-after-union",
            "filter_with_where_symmetry",
            "FILTER is equivalent to WITH * WHERE after a UNION ALL subquery",
            "CALL () { RETURN null AS x UNION ALL RETURN 1 AS x "
            "UNION ALL RETURN 2 AS x } "
            "FILTER x IS NULL OR x = 2 RETURN x AS value",
            "CALL () { RETURN null AS x UNION ALL RETURN 1 AS x "
            "UNION ALL RETURN 2 AS x } "
            "WITH * WHERE x IS NULL OR x = 2 RETURN x AS value",
        ),
        Case(
            "filter-after-aggregation",
            "filter_with_where_symmetry",
            "FILTER is equivalent to WITH * WHERE after grouping",
            "MATCH (n:N) WITH n.p AS p, count(*) AS c "
            "FILTER p IS NULL OR c > 1 RETURN p, c",
            "MATCH (n:N) WITH n.p AS p, count(*) AS c "
            "WITH * WHERE p IS NULL OR c > 1 RETURN p, c",
            setup=graph,
        ),
        Case(
            "for-unwind-null-nested",
            "for_unwind_symmetry",
            "FOR and UNWIND have the same semantics for null and nested values",
            "FOR x IN [null, 1, [2, 3], []] RETURN x AS value",
            "UNWIND [null, 1, [2, 3], []] AS x RETURN x AS value",
        ),
        Case(
            "for-unwind-correlated-expression",
            "for_unwind_symmetry",
            "FOR and UNWIND have the same semantics for correlated list expressions",
            "MATCH (n:N) FOR x IN [n.id, null] RETURN n.id AS id, x",
            "MATCH (n:N) UNWIND [n.id, null] AS x RETURN n.id AS id, x",
            setup=graph,
        ),
        Case(
            "for-unwind-count-subquery",
            "for_unwind_symmetry",
            "FOR and UNWIND have the same semantics inside COUNT subqueries",
            "RETURN COUNT { FOR x IN [1, 2, null] "
            "FILTER x IS NOT NULL RETURN x } AS value",
            "RETURN COUNT { UNWIND [1, 2, null] AS x "
            "FILTER x IS NOT NULL RETURN x } AS value",
        ),
        Case(
            "for-unwind-empty-correlated",
            "for_unwind_symmetry",
            "FOR and UNWIND both remove an outer row when expanding an empty list",
            "MATCH (n:N) FOR x IN CASE WHEN n.id = 2 THEN [] ELSE [n.id] END "
            "RETURN n.id AS id, x",
            "MATCH (n:N) UNWIND CASE WHEN n.id = 2 THEN [] ELSE [n.id] END AS x "
            "RETURN n.id AS id, x",
            setup=graph,
        ),
        Case(
            "let-with-star-chain",
            "let_with_star_symmetry",
            "LET bindings equal WITH * projections when all variables remain in scope",
            "UNWIND [null, -1, 0, 2] AS x "
            "LET y = coalesce(x, 0) + 1 LET z = y * 2 RETURN x, y, z",
            "UNWIND [null, -1, 0, 2] AS x "
            "WITH *, coalesce(x, 0) + 1 AS y WITH *, y * 2 AS z RETURN x, y, z",
        ),
        Case(
            "let-with-star-optional",
            "let_with_star_symmetry",
            "LET and WITH * preserve the same optional variables and null values",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) "
            "LET v = coalesce(m.p, -1) RETURN n.id AS n, m.id AS m, v",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:S]->(m:N) "
            "WITH *, coalesce(m.p, -1) AS v RETURN n.id AS n, m.id AS m, v",
            setup=graph,
        ),
        Case(
            "let-with-star-call-subquery",
            "let_with_star_symmetry",
            "LET and WITH * bind the same expression inside a scoped CALL subquery",
            "MATCH (n:N) CALL (n) { LET x = n.id + 1 RETURN x } "
            "RETURN n.id AS id, x",
            "MATCH (n:N) CALL (n) { WITH *, n.id + 1 AS x RETURN x } "
            "RETURN n.id AS id, x",
            setup=graph,
        ),
        Case(
            "extended-case-null-type",
            "extended_generic_case_symmetry",
            "Extended simple CASE equals the corresponding generic CASE for null and type tests",
            "UNWIND [null, -1, 0, 'x'] AS x RETURN x, "
            "CASE x WHEN IS NULL THEN 'null' "
            "WHEN IS TYPED INTEGER THEN 'int' ELSE 'other' END AS value",
            "UNWIND [null, -1, 0, 'x'] AS x RETURN x, "
            "CASE WHEN x IS NULL THEN 'null' "
            "WHEN x IS TYPED INTEGER THEN 'int' ELSE 'other' END AS value",
        ),
        Case(
            "extended-case-comparator-list",
            "extended_generic_case_symmetry",
            "Extended simple CASE comparator lists equal their generic Boolean expansion",
            "UNWIND [-2, 0, 1, 2, 5] AS x RETURN x, "
            "CASE x WHEN < 0 THEN 'negative' WHEN = 0, = 1 THEN 'small' "
            "WHEN >= 2 THEN 'large' ELSE 'other' END AS value",
            "UNWIND [-2, 0, 1, 2, 5] AS x RETURN x, "
            "CASE WHEN x < 0 THEN 'negative' WHEN x = 0 OR x = 1 THEN 'small' "
            "WHEN x >= 2 THEN 'large' ELSE 'other' END AS value",
        ),
        Case(
            "extended-case-string-comparators",
            "extended_generic_case_symmetry",
            "Extended simple CASE string comparators equal generic CASE predicates",
            "UNWIND ['alpha', 'beta', 'omega', null] AS x RETURN x, "
            "CASE x WHEN STARTS WITH 'a' THEN 1 WHEN ENDS WITH 'a' THEN 2 "
            "WHEN IS NULL THEN 3 ELSE 4 END AS value",
            "UNWIND ['alpha', 'beta', 'omega', null] AS x RETURN x, "
            "CASE WHEN x STARTS WITH 'a' THEN 1 WHEN x ENDS WITH 'a' THEN 2 "
            "WHEN x IS NULL THEN 3 ELSE 4 END AS value",
        ),
        Case(
            "explicit-default-match-mode",
            "default_match_mode_symmetry",
            "Explicit DIFFERENT RELATIONSHIPS is functionally equivalent to the default match mode",
            "MATCH p = (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH DIFFERENT RELATIONSHIPS p = (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "explicit-default-walk-mode",
            "default_path_mode_symmetry",
            "Explicit WALK equals the default path mode",
            "MATCH p = (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH p = WALK (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "trail-under-default-match-mode",
            "trail_default_match_symmetry",
            "TRAIL adds no constraint when DIFFERENT RELATIONSHIPS already enforces relationship uniqueness",
            "MATCH p = (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH p = TRAIL (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "quantified-relationship-expanded-qpp",
            "quantified_relationship_qpp_symmetry",
            "A quantified relationship equals its documented expanded quantified path pattern",
            "MATCH p = (a:N)-[:R]->{1,2}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH p = (a:N) (()-[:R]->()){1,2} (b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "legacy-varlength-quantified-relationship",
            "legacy_quantified_path_surface_symmetry",
            "Legacy and quantified relationship syntax bind the same endpoints and path lengths",
            "MATCH p = (a:N)-[:R*1..2]->(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH p = (a:N)-[:R]->{1,2}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "count-collect-size-correlated",
            "count_collect_size_symmetry",
            "COUNT subquery rows equal the size of the corresponding COLLECT subquery",
            "MATCH (n:N) RETURN n.id AS id, "
            "COUNT { MATCH (n)-[:R]->(m:N) RETURN m } AS value",
            "MATCH (n:N) RETURN n.id AS id, "
            "size(COLLECT { MATCH (n)-[:R]->(m:N) RETURN m }) AS value",
            setup=graph,
        ),
        Case(
            "count-collect-size-union-all",
            "count_collect_size_symmetry",
            "COUNT and COLLECT size agree for UNION ALL subquery rows",
            "RETURN COUNT { MATCH (n:N) WHERE n.p IS NULL RETURN n "
            "UNION ALL MATCH (n:N) WHERE n.p = 1 RETURN n } AS value",
            "RETURN size(COLLECT { MATCH (n:N) WHERE n.p IS NULL RETURN n "
            "UNION ALL MATCH (n:N) WHERE n.p = 1 RETURN n }) AS value",
            setup=graph,
        ),
        Case(
            "exists-count-positive-correlated",
            "exists_count_positive_symmetry",
            "EXISTS subquery equals checking whether the corresponding COUNT is positive",
            "MATCH (n:N) RETURN n.id AS id, "
            "EXISTS { MATCH (n)-[:R]->(:N) } AS value",
            "MATCH (n:N) RETURN n.id AS id, "
            "COUNT { MATCH (n)-[:R]->(:N) } > 0 AS value",
            setup=graph,
        ),
        Case(
            "next-with-projection",
            "next_with_symmetry",
            "NEXT passes the same projected rows as WITH",
            "UNWIND [null, 1, 2] AS x RETURN x AS x "
            "NEXT RETURN coalesce(x, 0) + 1 AS value",
            "UNWIND [null, 1, 2] AS x WITH x "
            "RETURN coalesce(x, 0) + 1 AS value",
        ),
        Case(
            "next-with-aggregation",
            "next_with_symmetry",
            "Aggregation after NEXT equals aggregation after the same WITH projection",
            "UNWIND [1, 1, 2, 3] AS x RETURN x AS x "
            "NEXT RETURN x % 2 AS key, count(*) AS value",
            "UNWIND [1, 1, 2, 3] AS x WITH x "
            "RETURN x % 2 AS key, count(*) AS value",
        ),
        Case(
            "when-case-null",
            "when_case_symmetry",
            "A row-wise conditional CALL equals the corresponding CASE expression",
            "UNWIND [null, -1, 0, 1] AS x CALL (x) { "
            "WHEN x > 0 THEN RETURN 'positive' AS value "
            "ELSE RETURN 'other' AS value } RETURN x, value",
            "UNWIND [null, -1, 0, 1] AS x RETURN x, "
            "CASE WHEN x > 0 THEN 'positive' ELSE 'other' END AS value",
        ),
        Case(
            "when-case-multiple-branches",
            "when_case_symmetry",
            "Conditional query branches use the same first-true semantics as CASE",
            "UNWIND [-1, 0, 1, 2] AS x CALL (x) { "
            "WHEN x < 0 THEN RETURN 'negative' AS value "
            "WHEN x = 0 THEN RETURN 'zero' AS value "
            "WHEN x > 0 THEN RETURN 'positive' AS value "
            "ELSE RETURN 'other' AS value } RETURN x, value",
            "UNWIND [-1, 0, 1, 2] AS x RETURN x, CASE "
            "WHEN x < 0 THEN 'negative' WHEN x = 0 THEN 'zero' "
            "WHEN x > 0 THEN 'positive' ELSE 'other' END AS value",
        ),
        Case(
            "explicit-match-mode-exists-subquery",
            "default_match_mode_symmetry",
            "Explicit DIFFERENT RELATIONSHIPS equals the default inside EXISTS",
            "MATCH (n:N) RETURN n.id AS id, EXISTS { "
            "MATCH (n)-[:R]->{1,3}(:N) } AS value",
            "MATCH (n:N) RETURN n.id AS id, EXISTS { "
            "MATCH DIFFERENT RELATIONSHIPS (n)-[:R]->{1,3}(:N) } AS value",
            setup=graph,
        ),
        Case(
            "repeatable-explicit-walk",
            "repeatable_walk_symmetry",
            "WALK is the only path mode allowed by REPEATABLE ELEMENTS and is its default",
            "MATCH REPEATABLE ELEMENTS p = (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH REPEATABLE ELEMENTS p = WALK (a:N)-[:R]->{1,3}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "default-vs-repeatable-rel-inequality",
            "match_mode_guard_symmetry",
            "Default relationship uniqueness equals REPEATABLE ELEMENTS plus explicit relationship inequality",
            "MATCH (a:N)-[r1:R]->(b:N)-[r2:R]->(c:N) "
            "RETURN a.id AS a, b.id AS b, c.id AS c, r1.id AS r1, r2.id AS r2",
            "MATCH REPEATABLE ELEMENTS (a:N)-[r1:R]->(b:N)-[r2:R]->(c:N) "
            "WHERE r1 <> r2 "
            "RETURN a.id AS a, b.id AS b, c.id AS c, r1.id AS r1, r2.id AS r2",
            setup=graph,
        ),
        Case(
            "repeatable-vs-split-match",
            "repeatable_split_match_symmetry",
            "REPEATABLE ELEMENTS on two fixed hops equals matching each hop in a separate MATCH clause",
            "MATCH REPEATABLE ELEMENTS (a:N)-[r1:R]->(b:N)-[r2:R]->(c:N) "
            "RETURN a.id AS a, b.id AS b, c.id AS c, r1.id AS r1, r2.id AS r2",
            "MATCH (a:N)-[r1:R]->(b:N) MATCH (b)-[r2:R]->(c:N) "
            "RETURN a.id AS a, b.id AS b, c.id AS c, r1.id AS r1, r2.id AS r2",
            setup=graph,
        ),
        Case(
            "acyclic-two-hop-node-guards",
            "acyclic_node_guard_symmetry",
            "ACYCLIC on a two-hop path equals requiring all three nodes to differ",
            "MATCH p = ACYCLIC (a:N)-[:R]->(b:N)-[:R]->(c:N) "
            "RETURN a.id AS a, b.id AS b, c.id AS c, length(p) AS hops",
            "MATCH p = (a:N)-[:R]->(b:N)-[:R]->(c:N) "
            "WHERE a <> b AND a <> c AND b <> c "
            "RETURN a.id AS a, b.id AS b, c.id AS c, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "acyclic-three-hop-node-guards",
            "acyclic_node_guard_symmetry",
            "ACYCLIC on a three-hop path equals pairwise inequality of its four nodes",
            "MATCH p = ACYCLIC (a:N)-[:R]->(b:N)-[:R]->(c:N)-[:R]->(d:N) "
            "RETURN a.id AS a, b.id AS b, c.id AS c, d.id AS d, length(p) AS hops",
            "MATCH p = (a:N)-[:R]->(b:N)-[:R]->(c:N)-[:R]->(d:N) "
            "WHERE a <> b AND a <> c AND a <> d AND b <> c AND b <> d AND c <> d "
            "RETURN a.id AS a, b.id AS b, c.id AS c, d.id AS d, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "zero-one-legacy-quantified",
            "legacy_quantified_path_surface_symmetry",
            "Legacy and quantified relationship syntax bind the same zero-or-one-hop paths",
            "MATCH p = (a:N)-[:R*0..1]->(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            "MATCH p = (a:N)-[:R]->{0,1}(b:N) "
            "RETURN a.id AS a, b.id AS b, length(p) AS hops",
            setup=graph,
        ),
        Case(
            "optional-node-property-inline-where",
            "optional_inline_predicate_symmetry",
            "An inline optional node property equals the same predicate attached to OPTIONAL MATCH",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m:N {p: 3}) "
            "RETURN n.id AS n, m.id AS m",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m:N) WHERE m.p = 3 "
            "RETURN n.id AS n, m.id AS m",
            setup=graph,
        ),
        Case(
            "optional-rel-property-inline-where",
            "optional_inline_predicate_symmetry",
            "An inline optional relationship property equals the same attached predicate",
            "MATCH (n:N) OPTIONAL MATCH (n)-[r:R {id: 13}]->(m:N) "
            "RETURN n.id AS n, r.id AS r, m.id AS m",
            "MATCH (n:N) OPTIONAL MATCH (n)-[r:R]->(m:N) WHERE r.id = 13 "
            "RETURN n.id AS n, r.id AS r, m.id AS m",
            setup=graph,
        ),
        Case(
            "optional-label-inline-where",
            "optional_inline_predicate_symmetry",
            "An inline optional label equals the same attached label predicate",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m:N) "
            "RETURN n.id AS n, m.id AS m",
            "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m) WHERE m:N "
            "RETURN n.id AS n, m.id AS m",
            setup=graph,
        ),
        Case(
            "conditional-count-case",
            "conditional_count_case_symmetry",
            "A conditional COUNT subquery equals choosing between the same two COUNT subqueries",
            "MATCH (n:N) RETURN n.id AS id, COUNT { "
            "WHEN n.p IS NULL THEN { MATCH (n)-[:S]->(m) RETURN m } "
            "ELSE { MATCH (n)-[:R]->(m) RETURN m } } AS value",
            "MATCH (n:N) RETURN n.id AS id, CASE WHEN n.p IS NULL THEN "
            "COUNT { MATCH (n)-[:S]->(m) RETURN m } ELSE "
            "COUNT { MATCH (n)-[:R]->(m) RETURN m } END AS value",
            setup=graph,
        ),
        Case(
            "conditional-count-no-else",
            "conditional_count_case_symmetry",
            "A false conditional COUNT branch without ELSE contributes zero rows",
            "MATCH (n:N) RETURN n.id AS id, COUNT { "
            "WHEN n.p = 1 THEN { MATCH (n)-[:R]->(m) RETURN m } } AS value",
            "MATCH (n:N) RETURN n.id AS id, CASE WHEN n.p = 1 THEN "
            "COUNT { MATCH (n)-[:R]->(m) RETURN m } ELSE 0 END AS value",
            setup=graph,
        ),
        Case(
            "conditional-call-no-else-filter",
            "conditional_call_filter_symmetry",
            "A conditional CALL without ELSE filters outer rows like WITH WHERE",
            "MATCH (n:N) CALL (n) { WHEN n.p IS NOT NULL THEN "
            "RETURN n.id AS id } RETURN id",
            "MATCH (n:N) WITH n WHERE n.p IS NOT NULL RETURN n.id AS id",
            setup=graph,
        ),
        Case(
            "conditional-call-union-expansion",
            "conditional_call_union_symmetry",
            "A conditional CALL equals mutually exclusive UNION ALL branches",
            "UNWIND [null, -1, 0, 1] AS x CALL (x) { "
            "WHEN x > 0 THEN RETURN 'positive' AS value "
            "ELSE RETURN 'other' AS value } RETURN x, value",
            "UNWIND [null, -1, 0, 1] AS x CALL (x) { "
            "WITH x WHERE x > 0 RETURN 'positive' AS value UNION ALL "
            "WITH x WHERE coalesce(x > 0, false) = false RETURN 'other' AS value "
            "} RETURN x, value",
        ),
        Case(
            "next-with-distinct",
            "next_with_symmetry",
            "DISTINCT rows passed through NEXT equal the same WITH DISTINCT rows",
            "UNWIND [1, 1, 2, null, null] AS x RETURN DISTINCT x AS x "
            "NEXT RETURN x AS value",
            "UNWIND [1, 1, 2, null, null] AS x WITH DISTINCT x "
            "RETURN x AS value",
        ),
        Case(
            "next-with-order-limit",
            "next_with_symmetry",
            "Ordered limited rows passed through NEXT equal the same WITH result",
            "UNWIND [3, 1, 2] AS x RETURN x AS x ORDER BY x LIMIT 2 "
            "NEXT RETURN x AS value",
            "UNWIND [3, 1, 2] AS x WITH x ORDER BY x LIMIT 2 "
            "RETURN x AS value",
            ordered=True,
        ),
        Case(
            "union-distinct-small-numeric-swap",
            "union_branch_order_symmetry",
            "UNION duplicate elimination is independent of integer and float branch order",
            "RETURN 1 AS value UNION RETURN 1.0 AS value",
            "RETURN 1.0 AS value UNION RETURN 1 AS value",
        ),
        Case(
            "distinct-small-numeric-input-order",
            "distinct_input_order_symmetry",
            "DISTINCT output is independent of the order of numerically equal inputs",
            "UNWIND [1, 1.0] AS x RETURN DISTINCT x AS value",
            "UNWIND [1.0, 1] AS x RETURN DISTINCT x AS value",
        ),
        Case(
            "union-distinct-nested-numeric-swap",
            "union_branch_order_symmetry",
            "UNION on numerically equal nested lists is independent of branch order",
            "RETURN [1, {x: 2.0}] AS value UNION RETURN [1.0, {x: 2}] AS value",
            "RETURN [1.0, {x: 2}] AS value UNION RETURN [1, {x: 2.0}] AS value",
        ),
        Case(
            "union-distinct-numeric-division-cypher25",
            "union_branch_order_symmetry",
            "Swapping read-only UNION branches cannot change a later calculation",
            "CALL () { RETURN 1 AS x UNION RETURN 1.0 AS x } "
            "RETURN x / 2 AS value",
            "CALL () { RETURN 1.0 AS x UNION RETURN 1 AS x } "
            "RETURN x / 2 AS value",
        ),
        Case(
            "union-distinct-numeric-division-cypher5",
            "union_branch_order_symmetry",
            "Swapping read-only UNION branches cannot change a later calculation",
            "CALL () { RETURN 1 AS x UNION RETURN 1.0 AS x } "
            "RETURN x / 2 AS value",
            "CALL () { RETURN 1.0 AS x UNION RETURN 1 AS x } "
            "RETURN x / 2 AS value",
            language="5",
        ),
        Case(
            "distinct-numeric-division",
            "distinct_input_order_symmetry",
            "Reordering equal inputs before DISTINCT cannot change a later calculation",
            "UNWIND [1, 1.0] AS x WITH DISTINCT x RETURN x / 2 AS value",
            "UNWIND [1.0, 1] AS x WITH DISTINCT x RETURN x / 2 AS value",
        ),
        Case(
            "union-all-numeric-division-control",
            "union_all_branch_order_symmetry",
            "UNION ALL branch order preserves the multiset of later numeric calculations",
            "CALL () { RETURN 1 AS x UNION ALL RETURN 1.0 AS x } "
            "RETURN x / 2 AS value",
            "CALL () { RETURN 1.0 AS x UNION ALL RETURN 1 AS x } "
            "RETURN x / 2 AS value",
        ),
        Case(
            "union-distinct-explicit-float-control",
            "union_branch_order_symmetry",
            "Branch order is immaterial when numerically equal branches use one explicit type",
            "CALL () { RETURN toFloat(1) AS x UNION RETURN 1.0 AS x } "
            "RETURN x / 2 AS value",
            "CALL () { RETURN 1.0 AS x UNION RETURN toFloat(1) AS x } "
            "RETURN x / 2 AS value",
        ),
        Case(
            "union-distinct-unequal-numeric-control",
            "union_branch_order_symmetry",
            "Branch order preserves later calculations when numeric rows are not duplicates",
            "CALL () { RETURN 1 AS x UNION RETURN 2.0 AS x } "
            "RETURN x / 2 AS value",
            "CALL () { RETURN 2.0 AS x UNION RETURN 1 AS x } "
            "RETURN x / 2 AS value",
        ),
        Case(
            "string-concat-operator",
            "gql_concat_surface_symmetry",
            "The GQL string concatenation operator equals Cypher string addition",
            "UNWIND [['a', 'b'], ['a', null], [null, 'b']] AS x "
            "RETURN x, x[0] || x[1] AS value",
            "UNWIND [['a', 'b'], ['a', null], [null, 'b']] AS x "
            "RETURN x, x[0] + x[1] AS value",
        ),
        Case(
            "list-concat-operator",
            "gql_concat_surface_symmetry",
            "The GQL list concatenation operator equals Cypher addition for two lists",
            "UNWIND [[[1], [2, null]], [[], [3]], [[null], []]] AS x "
            "RETURN x, x[0] || x[1] AS value",
            "UNWIND [[[1], [2, null]], [[], [3]], [[null], []]] AS x "
            "RETURN x, x[0] + x[1] AS value",
        ),
        Case(
            "character-length-aliases",
            "character_length_alias_symmetry",
            "char_length and character_length are aliases",
            "UNWIND ['', 'a', '😀', null] AS x RETURN x, char_length(x) AS value",
            "UNWIND ['', 'a', '😀', null] AS x RETURN x, character_length(x) AS value",
        ),
        Case(
            "node-map-projection-properties",
            "node_properties_surface_symmetry",
            "A full node map projection equals properties(node)",
            "MATCH (n:N) RETURN n.id AS id, n{.*} AS value",
            "MATCH (n:N) RETURN n.id AS id, properties(n) AS value",
            setup=graph,
        ),
        Case(
            "node-element-id-equality",
            "element_id_entity_equality_symmetry",
            "Node equality equals equality of their transaction-unique element IDs",
            "MATCH (n:N), (m:N) RETURN n.id AS n, m.id AS m, n = m AS value",
            "MATCH (n:N), (m:N) RETURN n.id AS n, m.id AS m, "
            "elementId(n) = elementId(m) AS value",
            setup=graph,
        ),
    )


def execute(session: Any, case: Case, query: str, timeout: int) -> dict[str, Any]:
    from neo4j import Query

    try:
        result = session.run(
            Query(f"CYPHER {case.language} {query}", timeout=timeout)
        )
        rows = tuple(to_typed_value(record.data()) for record in result)
        result.consume()
        return {
            "status": "ok",
            "typed_rows": rows,
            "rows": to_artifact_jsonable(rows),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "code", None),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }


def equivalent(case: Case, left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "error":
        return left.get("error_code") == right.get("error_code")
    comparison = compare_typed_results(
        left["typed_rows"], right["typed_rows"], ordered=case.ordered
    )
    return comparison.equivalent and comparison.complete_result


def reset_and_setup(driver: Any, setup: tuple[str, ...]) -> None:
    with driver.session(database="neo4j") as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
        for statement in setup:
            session.run(statement).consume()


def replay(driver: Any, case: Case, timeout: int, rounds: int) -> dict[str, Any]:
    reset_and_setup(driver, case.setup)
    observations: list[dict[str, Any]] = []
    with driver.session(database="neo4j") as session:
        for round_index in range(rounds):
            order = ("A", "B") if round_index % 2 == 0 else ("B", "A")
            outcomes: dict[str, dict[str, Any]] = {}
            for side in order:
                outcomes[side] = execute(
                    session,
                    case,
                    case.query_a if side == "A" else case.query_b,
                    timeout,
                )
            observations.append(
                {
                    "round": round_index + 1,
                    "order": "".join(order),
                    "equivalent": equivalent(case, outcomes["A"], outcomes["B"]),
                    "a": {k: v for k, v in outcomes["A"].items() if k != "typed_rows"},
                    "b": {k: v for k, v in outcomes["B"].items() if k != "typed_rows"},
                }
            )
    deterministic_mismatch = all(
        not observation["equivalent"] for observation in observations
    )
    return {
        "case_id": case.case_id,
        "rule_id": case.rule_id,
        "theorem": case.theorem,
        "language": case.language,
        "query_a": case.query_a,
        "query_b": case.query_b,
        "deterministic_mismatch": deterministic_mismatch,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7704")
    )
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--case")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if not args.password:
        parser.error("set NEO4J_PASSWORD or pass --password")

    selected = (SELF_TEST,) if args.self_test else logical_cases()
    if args.case:
        selected = tuple(case for case in selected if case.case_id == args.case)
    if not selected:
        parser.error("no logical cases selected")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    records: list[dict[str, Any]] = []
    try:
        with driver.session(database="neo4j") as session:
            component = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "WHERE name = 'Neo4j Kernel' "
                "RETURN versions[0] AS version, edition LIMIT 1"
            ).single(strict=True)
            environment = component.data()
        for case in selected:
            records.append(replay(driver, case, args.timeout, args.rounds))
    finally:
        driver.close()

    payload = {
        "environment": environment,
        "self_test": args.self_test,
        "case_count": len(records),
        "deterministic_mismatch_count": sum(
            int(record["deterministic_mismatch"]) for record in records
        ),
        "cases": records,
    }
    if args.compact:
        payload = {
            **{key: value for key, value in payload.items() if key != "cases"},
            "case_status": [
                {
                    "case_id": record["case_id"],
                    "rule_id": record["rule_id"],
                    "deterministic_mismatch": record["deterministic_mismatch"],
                }
                for record in records
            ],
            "mismatches": [
                record for record in records if record["deterministic_mismatch"]
            ],
        }
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    print(rendered)
    return 2 if payload["deterministic_mismatch_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
