from graph_sym_perf.reducer.dependencies import analyze_query, dependency_safe_removal


def test_removal_rejects_definition_when_remaining_clause_uses_its_variable() -> None:
    clauses = ("MATCH (n:Person)", "MATCH (n)-[:KNOWS]->(m)", "RETURN m")
    assert not dependency_safe_removal(clauses, 0)
    assert not dependency_safe_removal(clauses, 1)


def test_with_alias_resets_scope_and_rejects_dropped_input() -> None:
    graph = analyze_query(
        "MATCH (n:Person) WITH n.name AS name RETURN name ORDER BY name LIMIT 5"
    )

    assert graph.valid
    assert graph.output_schema == ("name",)
    assert graph.clauses[1].incoming_scope == frozenset({"n"})
    assert graph.clauses[1].outgoing_scope == frozenset({"name"})
    assert graph.clauses[2].uses == frozenset({"name"})
    assert graph.clauses[2].barriers == ("ORDER BY", "LIMIT")


def test_path_node_and_relationship_variables_are_first_class_definitions() -> None:
    graph = analyze_query(
        "MATCH p = (a:Person)-[r:KNOWS*1..3]-(b:Person) "
        "RETURN p, a, r, b ORDER BY b.id SKIP 1"
    )

    assert graph.valid
    assert graph.clauses[0].definitions == frozenset({"p", "a", "r", "b"})
    assert graph.clauses[1].uses == frozenset({"p", "a", "r", "b"})
    assert graph.clauses[1].barriers == ("ORDER BY", "SKIP")


def test_aggregation_in_with_is_observable_and_fail_closed_for_automatic_deletion() -> None:
    graph = analyze_query("MATCH (n), (m) WITH count(*) AS rows RETURN rows")

    assert graph.valid
    assert graph.aggregate_sensitive
    assert not dependency_safe_removal(("MATCH (n), (m)", "WITH count(*) AS rows", "RETURN rows"), 0)


def test_pattern_property_expression_cannot_hide_an_unbound_variable() -> None:
    clauses = ("MATCH (m)", "MATCH (n {id: m.id})", "RETURN n")

    assert analyze_query(" ".join(clauses)).valid
    assert not dependency_safe_removal(clauses, 0)


def test_contains_operator_is_not_treated_as_an_unbound_variable() -> None:
    graph = analyze_query("MATCH (n) WHERE 'uv' CONTAINS '9uvWO' RETURN n")

    assert graph.valid
    assert graph.reason_codes == ()


def test_optional_match_scope_is_analyzable_for_fail_closed_runtime_reduction() -> None:
    graph = analyze_query(
        "MATCH (n) "
        "OPTIONAL MATCH (n)-[r]->(m) WHERE r.id > 0 "
        "OPTIONAL MATCH (m)--(x) "
        "RETURN n, m, x"
    )

    assert graph.valid
    assert graph.reason_codes == ()
    assert graph.clauses[1].keyword == "OPTIONAL MATCH"
    assert graph.clauses[1].definitions == frozenset({"r", "m"})
    assert graph.clauses[2].definitions == frozenset({"x"})
    assert graph.output_schema == ("n", "m", "x")


def test_validated_f989_shape_has_a_dependency_graph_for_reduction() -> None:
    query = (
        "MATCH (n0)-[r0 :T6]->(n1 :L1), (n3 :L5) "
        "WHERE ('uv' CONTAINS '9uvWO') "
        "MATCH (n3), (n3 :L5) "
        "MATCH (n4 :L4)-[r2 :T5]->(n5 :L1)-[r3 :T1]->(n6), "
        "(n4)<-[r4 :T0]-(n7 :L4 :L2)-[r5 :T3]->(n8) "
        "WHERE ((((((((n1.k7) OR (r4.k44)) AND ((r2.id) <> (r3.id))) "
        "AND ((r2.id) <> (r4.id))) AND ((r2.id) <> (r5.id))) "
        "AND ((r3.id) <> (r4.id))) AND ((r3.id) <> (r5.id))) "
        "AND ((r4.id) <> (r5.id))) "
        "OPTIONAL MATCH (n4 :L4)<-[]-(n7 :L4)-[]->(n8), (n3) "
        "WITH r3 ORDER BY (r3.k46) DESC "
        "MATCH (n3 :L5), (n3) WHERE ((n3.k36) < (r3.k47)) "
        "OPTIONAL MATCH (n0 :L2)-[]->(n1)<-[]-(n2) "
        "RETURN (r3.k46) AS a0"
    )

    graph = analyze_query(query)

    assert graph.valid
    assert graph.reason_codes == ()
    assert graph.output_schema == ("a0",)
