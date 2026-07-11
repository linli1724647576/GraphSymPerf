import re

from graph_sym_perf.dialects.base import (
    CompiledQuery,
    DialectCapabilities,
    ExecutionEnvelope,
    ReasonCode,
    accept,
    dynamic_dollar_quote,
    has_distinct_order_by_projection_mismatch,
    has_multi_label_node,
    has_top_level_keyword,
    has_variable_label_conflict,
    parse_projection,
    reject,
    render_typed_parameters,
)
from graph_sym_perf.generation.corpus import PortableQuery


class ApacheAgeDialect:
    """Compile the GDsmith portable subset into AGE's PostgreSQL envelope."""

    capabilities = DialectCapabilities(
        name="apache_age",
        multiple_node_labels=False,
        native_parameters=False,
        requires_explicit_result_schema=True,
        requires_graph_path=False,
        generation_backend="agensgraph_proxy",
    )

    def __init__(self, *, graph_name: str = "graph_sym_perf") -> None:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", graph_name) is None:
            raise ValueError(f"unsafe Apache AGE graph name: {graph_name!r}")
        self.graph_name = graph_name

    def check(self, query: PortableQuery):
        if has_multi_label_node(query.text):
            return reject(ReasonCode.MULTI_LABEL_UNSUPPORTED)
        if has_variable_label_conflict(query.text):
            return reject(ReasonCode.VARIABLE_LABEL_CONFLICT)
        if has_distinct_order_by_projection_mismatch(query.text):
            return reject(ReasonCode.DISTINCT_ORDER_BY_SCOPE)
        if has_top_level_keyword(query.text, "CALL"):
            return reject(ReasonCode.RETURN_STAR_SCOPE_UNKNOWN if "RETURN *" in query.text.upper() else ReasonCode.AGE_SUBQUERY_UNSUPPORTED)
        try:
            rendered = render_typed_parameters(query.text, query.parameters)
        except (TypeError, ValueError) as exc:
            return reject(ReasonCode.PARAMETER_RENDER_UNSUPPORTED, error=str(exc))
        try:
            projection = parse_projection(rendered, allow_star_scope=True)
        except LookupError as exc:
            return reject(ReasonCode.RETURN_STAR_SCOPE_UNKNOWN, error=str(exc))
        except ValueError as exc:
            return reject(ReasonCode.PROJECTION_SCHEMA_UNSUPPORTED, error=str(exc))
        tag = dynamic_dollar_quote(rendered)
        columns = ", ".join(f'{_sql_identifier(column.name)} {column.postgres_type}' for column in projection.columns)
        graph = "'" + self.graph_name.replace("'", "''") + "'"
        statement = f"SELECT * FROM ag_catalog.cypher({graph}, {tag}{rendered}{tag}) AS ({columns})"
        compiled = CompiledQuery(
            cypher_core=rendered,
            execution_envelope=ExecutionEnvelope(
                statement=statement,
                protocol="age_cypher_typed_literals",
                dollar_quote_tag=tag,
            ),
            result_schema=projection.columns,
            provenance={
                "generation_backend": "agensgraph_proxy",
                "dialect": "apache_age",
                "parameter_protocol": "typed_literal_rendering",
            },
        )
        return accept(ReasonCode.APACHE_AGE_SUPPORTED, compiled)


def _sql_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None:
        raise ValueError(f"unsafe AGE result column: {value!r}")
    return '"' + value.replace('"', '""') + '"'
