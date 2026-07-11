from graph_sym_perf.dialects.base import (
    CompiledQuery,
    DialectCapabilities,
    ExecutionEnvelope,
    ReasonCode,
    accept,
    parse_projection,
)
from graph_sym_perf.generation.corpus import PortableQuery


class NativeCypherDialect:
    capabilities = DialectCapabilities(
        name="native_cypher",
        multiple_node_labels=True,
        native_parameters=True,
        requires_explicit_result_schema=False,
        requires_graph_path=False,
    )

    def check(self, query: PortableQuery):
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
