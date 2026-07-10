import pytest

from graph_sym_perf.equivalence.purity import PurityContext, assess_query_purity


def test_purity_rejects_writes_volatile_and_nondeterministic_result_shapes() -> None:
    for query in (
        "CREATE (n)",
        "MATCH (n) RETURN rand()",
        "MATCH (n) RETURN datetime()",
        "MATCH (n) RETURN id(n)",
        "MATCH (n) RETURN n LIMIT 3",
        "MATCH (n) RETURN collect(n)",
    ):
        assert not assess_query_purity(query).safe, query


def test_purity_accepts_ordered_read_query() -> None:
    evidence = assess_query_purity("MATCH (n) WHERE n.age > $age RETURN n.name ORDER BY n.name")
    assert evidence.safe


@pytest.mark.parametrize(
    ("query", "reason"),
    (
        ("MATCH (n) FOREACH (x IN [1] | SET n.x = x) RETURN n", "write_clause"),
        ("CALL db.labels()", "procedural_call"),
        ("MATCH (n) RETURN elementId(n)", "internal_id_function"),
        ("MATCH (n) RETURN localdatetime()", "volatile_function"),
        ("MATCH (n) RETURN toFloat('Infinity')", "non_finite_float"),
        ("MATCH (n) RETURN 0.0 / 0.0", "non_finite_float"),
        ("MATCH (n) RETURN collect(n) AS xs ORDER BY size(xs)", "unstable_collect"),
    ),
)
def test_purity_rejects_every_automatic_validation_hazard(query: str, reason: str) -> None:
    evidence = assess_query_purity(query)

    assert not evidence.safe
    assert reason in evidence.reason_codes


def test_purity_uses_adapter_declared_volatile_functions() -> None:
    evidence = assess_query_purity("MATCH (n) RETURN vendorClock()", volatile_functions=("vendorClock",))

    assert not evidence.safe
    assert "volatile_function" in evidence.reason_codes


def test_purity_accepts_collect_only_when_input_was_ordered_before_aggregation() -> None:
    evidence = assess_query_purity(
        "MATCH (n) WITH n ORDER BY n.id RETURN collect(n)",
        context=PurityContext(
            unique_order_keys=("n.id",),
            collect_input_order=("n.id",),
            expected_final_clause="RETURN",
        ),
    )

    assert evidence.safe


def test_collect_rejects_an_order_by_invalidated_by_a_later_data_source() -> None:
    evidence = assess_query_purity("MATCH (n) WITH n ORDER BY n.id MATCH (m) RETURN collect(m)")

    assert not evidence.safe
    assert "unstable_collect" in evidence.reason_codes


def test_limit_requires_explicit_unique_total_order_on_the_final_clause() -> None:
    query = "MATCH (n) RETURN n.name, n.id ORDER BY n.name, n.id LIMIT 3"

    assert not assess_query_purity(query).safe
    assert not assess_query_purity(
        query,
        context=PurityContext(unique_order_keys=("n.id",), expected_final_clause="RETURN"),
    ).safe
    assert assess_query_purity(
        query,
        context=PurityContext(
            unique_order_keys=("n.name", "n.id"),
            expected_final_clause="RETURN",
        ),
    ).safe


def test_collect_context_must_match_the_full_immediate_input_order() -> None:
    query = "MATCH (n) WITH n ORDER BY n.name, n.id RETURN collect(n)"
    partial = PurityContext(
        unique_order_keys=("n.id",),
        collect_input_order=("n.id",),
        expected_final_clause="RETURN",
    )
    full = PurityContext(
        unique_order_keys=("n.name", "n.id"),
        collect_input_order=("n.name", "n.id"),
        expected_final_clause="RETURN",
    )

    assert not assess_query_purity(query, context=partial).safe
    assert assess_query_purity(query, context=full).safe
