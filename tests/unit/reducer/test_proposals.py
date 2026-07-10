from graph_sym_perf.reducer.proposals import clause_removal_proposals


def test_proposals_never_leave_dangling_variable_reference() -> None:
    query = "MATCH (n:Person) MATCH (n)-[:KNOWS]->(m) RETURN m"
    proposals = clause_removal_proposals(query)
    assert all("MATCH (n:Person)" in proposal.query for proposal in proposals)
