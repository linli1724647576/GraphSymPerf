from graph_sym_perf.dialects.base import CompatibilityDecision, DecisionKind, PortableQuery


class ApacheAgeDialect:
    """AGE is compiled through its Cypher proxy; unsupported syntax is rejected."""
    def check(self, query: PortableQuery) -> CompatibilityDecision:
        if "CALL {" in query.text.upper():
            return CompatibilityDecision(DecisionKind.REJECT, "age_proxy_subquery_unsupported")
        return CompatibilityDecision(DecisionKind.ACCEPT, "age_proxy_portable_subset")
