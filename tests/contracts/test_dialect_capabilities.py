from graph_sym_perf.dialects.base import (
    CompatibilityDecision,
    DecisionKind,
    ReasonCode,
)
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.native import NativeCypherDialect
from graph_sym_perf.generation.corpus import PortableQuery


def test_agensgraph_single_label_and_age_proxy_constraints_are_explicit() -> None:
    assert AgensGraphDialect().check(PortableQuery("MATCH (n:A:B) RETURN n")).kind is DecisionKind.REJECT
    decision = ApacheAgeDialect(graph_name="g").check(PortableQuery("MATCH (n) RETURN n"))
    assert decision.kind is DecisionKind.ACCEPT
    assert decision.reason_code is ReasonCode.APACHE_AGE_SUPPORTED
    assert decision.compiled_query is not None
    assert decision.compiled_query.result_schema[0].postgres_type == "agtype"


def test_dialect_capabilities_and_generation_provenance_are_typed() -> None:
    agens = AgensGraphDialect()
    age = ApacheAgeDialect(graph_name="g")

    assert agens.capabilities.multiple_node_labels is False
    assert agens.capabilities.requires_graph_path is True
    assert age.capabilities.requires_explicit_result_schema is True
    assert age.capabilities.generation_backend == "agensgraph_proxy"


def test_return_star_requires_a_provable_scope_for_age() -> None:
    accepted = ApacheAgeDialect(graph_name="g").check(
        PortableQuery("MATCH (b:B)-[r:T]->(a:A) RETURN *")
    )
    assert accepted.kind is DecisionKind.ACCEPT
    assert tuple(column.name for column in accepted.compiled_query.result_schema) == ("a", "b", "r")

    rejected = ApacheAgeDialect(graph_name="g").check(
        PortableQuery("CALL { MATCH (n) RETURN n } RETURN *")
    )
    assert rejected.kind is DecisionKind.REJECT
    assert rejected.reason_code is ReasonCode.RETURN_STAR_SCOPE_UNKNOWN


def test_age_expands_star_even_when_projection_has_additional_items() -> None:
    decision = ApacheAgeDialect(graph_name="g").check(
        PortableQuery("MATCH (a:A), (b:B) RETURN *, 1 AS x")
    )

    assert decision.kind is DecisionKind.ACCEPT
    assert tuple(column.name for column in decision.compiled_query.result_schema) == ("a", "b", "x")


def test_backtick_multilabel_is_rejected_and_native_check_is_total() -> None:
    query = PortableQuery("MATCH (n:`A`:`B`) RETURN n")
    assert AgensGraphDialect().check(query).reason_code is ReasonCode.MULTI_LABEL_UNSUPPORTED
    assert ApacheAgeDialect(graph_name="g").check(query).reason_code is ReasonCode.MULTI_LABEL_UNSUPPORTED

    native = NativeCypherDialect().check(PortableQuery("MATCH (n) WITH * RETURN *"))
    assert native.kind is DecisionKind.ACCEPT


def test_backtick_variable_label_conflict_across_patterns_is_rejected() -> None:
    query = PortableQuery("MATCH (n:`A`) OPTIONAL MATCH (n:`B`) RETURN n")

    assert AgensGraphDialect().check(query).kind is DecisionKind.REJECT
    assert ApacheAgeDialect(graph_name="g").check(query).kind is DecisionKind.REJECT
