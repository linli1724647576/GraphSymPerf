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
