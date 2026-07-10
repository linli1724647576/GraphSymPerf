from graph_sym_perf.dialects.base import CompatibilityDecision, DecisionKind, PortableQuery
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect


def test_agensgraph_single_label_and_age_proxy_constraints_are_explicit() -> None:
    assert AgensGraphDialect().check(PortableQuery("MATCH (n:A:B) RETURN n")).kind is DecisionKind.REJECT
    decision = ApacheAgeDialect().check(PortableQuery("MATCH (n) RETURN n"))
    assert decision.kind in {DecisionKind.ACCEPT, DecisionKind.REJECT}
