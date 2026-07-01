from __future__ import annotations

from graph_sym_perf.adapters.apache_age import (
    ApacheAgeAdapter,
    _has_distinct_order_by_projection_mismatch,
    _has_variable_label_conflict,
    _return_aliases,
)
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.plan import normalize_plan


def test_apache_age_return_aliases_default_and_explicit_names() -> None:
    assert _return_aliases("MATCH (n) RETURN n.id AS id, count(*) AS c ORDER BY c") == ("id", "c")
    assert _return_aliases("MATCH (n) RETURN n.id, n.name LIMIT 3") == ("col1", "col2")


def test_apache_age_explain_analyze_text_is_normalized() -> None:
    plan = normalize_plan(
        TargetSystem.APACHE_AGE,
        (
            "Function Scan on cypher  (cost=0.25..10.25 rows=1000 width=32) "
            "(actual time=0.100..0.200 rows=42 loops=1)",
            "  Buffers: shared hit=17 read=3",
        ),
    )

    assert plan.operator_skeleton[0] == "FunctionScan"
    assert plan.metrics["rows"] == 42.0
    assert plan.metrics["page_cache_hits"] == 17.0


def test_apache_age_rejects_same_variable_with_different_labels() -> None:
    assert _has_variable_label_conflict("MATCH (n:L0)-->(m:L1) MATCH (n:L2) RETURN n")
    assert _has_variable_label_conflict("MATCH (n)-->(m:L1) OPTIONAL MATCH (n:L2) RETURN n")
    assert not _has_variable_label_conflict("MATCH (n:L0)-->(m:L1) MATCH (n:L0) RETURN n")


def test_apache_age_rejects_distinct_order_by_expression_not_projected() -> None:
    assert _has_distinct_order_by_projection_mismatch("MATCH (n) RETURN DISTINCT n.k1 ORDER BY n.k2")
    assert not _has_distinct_order_by_projection_mismatch("MATCH (n) RETURN DISTINCT n.k1 ORDER BY n.k1")


def test_apache_age_replay_filter_uses_backend_specific_rejections() -> None:
    adapter = object.__new__(ApacheAgeAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n:L0)-->(m:L1) MATCH (n:L2) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) RETURN DISTINCT n.k1 ORDER BY n.k2")
