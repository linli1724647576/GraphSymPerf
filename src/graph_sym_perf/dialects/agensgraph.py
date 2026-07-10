from graph_sym_perf.dialects.base import CompatibilityDecision, DecisionKind, PortableQuery


class AgensGraphDialect:
    def check(self, query: PortableQuery) -> CompatibilityDecision:
        if ":" in query.text.split("RETURN", 1)[0] and ":" in query.text.split("(", 1)[-1].split(")", 1)[0].replace(":", "", 1):
            return CompatibilityDecision(DecisionKind.REJECT, "single_label_only")
        return CompatibilityDecision(DecisionKind.ACCEPT, "agensgraph_portable_subset")
