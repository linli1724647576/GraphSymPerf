from graph_sym_perf.dialects.base import CompatibilityDecision, DecisionKind, PortableQuery


class NativeCypherDialect:
    def check(self, query: PortableQuery) -> CompatibilityDecision:
        return CompatibilityDecision(DecisionKind.ACCEPT, "native_cypher")
