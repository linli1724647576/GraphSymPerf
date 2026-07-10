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
