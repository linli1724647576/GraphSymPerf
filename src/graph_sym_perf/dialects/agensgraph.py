from graph_sym_perf.dialects.base import (
    CompiledQuery,
    DialectCapabilities,
    ExecutionEnvelope,
    ReasonCode,
    accept,
    has_distinct_order_by_projection_mismatch,
    has_multi_label_node,
    has_variable_label_conflict,
    parse_projection,
    reject,
    render_typed_parameters,
)
from graph_sym_perf.generation.corpus import PortableQuery


class AgensGraphDialect:
    capabilities = DialectCapabilities(
        name="agensgraph",
        multiple_node_labels=False,
        native_parameters=False,
        requires_explicit_result_schema=False,
        requires_graph_path=True,
    )

    def check(self, query: PortableQuery):
        if has_multi_label_node(query.text):
            return reject(ReasonCode.MULTI_LABEL_UNSUPPORTED)
        if has_variable_label_conflict(query.text):
            return reject(ReasonCode.VARIABLE_LABEL_CONFLICT)
        if has_distinct_order_by_projection_mismatch(query.text):
            return reject(ReasonCode.DISTINCT_ORDER_BY_SCOPE)
        try:
            rendered = render_typed_parameters(query.text, query.parameters)
            projection = parse_projection(rendered, allow_star_scope=True)
        except (LookupError, TypeError, ValueError) as exc:
            return reject(ReasonCode.PARAMETER_RENDER_UNSUPPORTED, error=str(exc))
        compiled = CompiledQuery(
            cypher_core=rendered,
            execution_envelope=ExecutionEnvelope(rendered, "typed_literal_rendering"),
            result_schema=projection.columns,
            provenance={"generation_backend": "gdsmith", "dialect": "agensgraph"},
        )
        return accept(ReasonCode.AGENSGRAPH_SUPPORTED, compiled)
