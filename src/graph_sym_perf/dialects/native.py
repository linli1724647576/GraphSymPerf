from graph_sym_perf.dialects.base import (
    CompiledQuery,
    DialectCapabilities,
    ExecutionEnvelope,
    ReasonCode,
    accept,
    has_multi_label_node,
    parse_projection,
    reject,
)
from graph_sym_perf.generation.corpus import PortableQuery


class NativeCypherDialect:
    def __init__(self, *, multiple_node_labels: bool = True) -> None:
        self.capabilities = DialectCapabilities(
            name="native_cypher",
            multiple_node_labels=multiple_node_labels,
            native_parameters=True,
            requires_explicit_result_schema=False,
            requires_graph_path=False,
        )

    def check(self, query: PortableQuery):
        if not self.capabilities.multiple_node_labels and has_multi_label_node(query.text):
            return reject(ReasonCode.MULTI_LABEL_UNSUPPORTED)
        try:
            projection = parse_projection(query.text, allow_star_scope=True)
            columns = projection.columns
        except (LookupError, ValueError):
            # Native drivers discover their result schema from the protocol;
            # failure of the conservative AGE projection parser is not a
            # compatibility rejection for native Cypher.
            columns = ()
        compiled = CompiledQuery(
            cypher_core=query.text,
            execution_envelope=ExecutionEnvelope(query.text, "native_driver_parameters"),
            result_schema=columns,
            parameters=query.parameters,
            provenance={"generation_backend": "gdsmith", "dialect": "native_cypher"},
        )
        return accept(ReasonCode.NATIVE_CYPHER_SUPPORTED, compiled)
