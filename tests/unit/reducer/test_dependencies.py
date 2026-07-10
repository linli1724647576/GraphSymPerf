from graph_sym_perf.reducer.dependencies import dependency_safe_removal


def test_removal_rejects_definition_when_remaining_clause_uses_its_variable() -> None:
    clauses = ("MATCH (n:Person)", "MATCH (n)-[:KNOWS]->(m)", "RETURN m")
    assert not dependency_safe_removal(clauses, 0)
    assert dependency_safe_removal(clauses, 1)
