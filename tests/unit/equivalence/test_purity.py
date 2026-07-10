import pytest

from graph_sym_perf.equivalence.purity import assess_query_purity


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
    evidence = assess_query_purity("MATCH (n) WITH n ORDER BY n.id RETURN collect(n)")

    assert evidence.safe


def test_collect_rejects_an_order_by_invalidated_by_a_later_data_source() -> None:
    evidence = assess_query_purity("MATCH (n) WITH n ORDER BY n.id MATCH (m) RETURN collect(m)")

    assert not evidence.safe
    assert "unstable_collect" in evidence.reason_codes
