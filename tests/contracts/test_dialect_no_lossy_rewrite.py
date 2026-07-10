from graph_sym_perf.dialects.base import DecisionKind, PortableQuery
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect


def test_dialect_never_drops_labels_to_raise_acceptance() -> None:
    query = PortableQuery("MATCH (n:A:B) RETURN n")
    decision = AgensGraphDialect().check(query)
    assert decision.kind is DecisionKind.REJECT
    assert decision.rewritten_query is None
