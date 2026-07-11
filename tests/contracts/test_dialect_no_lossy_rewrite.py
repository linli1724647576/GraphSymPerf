from graph_sym_perf.dialects.base import DecisionKind, ReasonCode, render_typed_parameters
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.generation.corpus import PortableQuery


def test_dialect_never_drops_labels_to_raise_acceptance() -> None:
    query = PortableQuery("MATCH (n:A:B) RETURN n")
    decision = AgensGraphDialect().check(query)
    assert decision.kind is DecisionKind.REJECT
    assert decision.rewritten_query is None
    assert decision.rewritten_ast is None
    assert decision.reason_code is ReasonCode.MULTI_LABEL_UNSUPPORTED


def test_accept_never_drops_query_semantics() -> None:
    text = (
        "MATCH (n:A) WHERE n.x = 1 "
        "RETURN DISTINCT n.x AS x ORDER BY x"
    )
    decision = AgensGraphDialect().check(PortableQuery(text))

    assert decision.kind is DecisionKind.ACCEPT
    assert decision.rewritten_ast is None
    assert decision.compiled_query is not None
    assert decision.compiled_query.cypher_core == text


def test_typed_parameter_renderer_does_not_rewrite_literals_or_change_nested_types() -> None:
    query = "RETURN '$name' AS literal, $name AS value, $payload AS payload, $items AS items"
    rendered = render_typed_parameters(
        query,
        {"name": 'A"B', "payload": {"flag": True, "nested": {"x": None}}, "items": [1, "x"]},
    )

    assert "'$name' AS literal" in rendered
    assert "'A\"B' AS value" in rendered
    assert "{flag: true, nested: {x: null}}" in rendered
    assert "[1, 'x']" in rendered


def test_age_uses_collision_free_dollar_quote_without_modifying_original_dollars() -> None:
    text = "RETURN '$$' AS marker, $value AS value"
    decision = ApacheAgeDialect(graph_name="g").check(PortableQuery(text, parameters={"value": 7}))

    assert decision.kind is DecisionKind.ACCEPT
    compiled = decision.compiled_query
    assert compiled is not None
    assert "'$$'" in compiled.cypher_core
    tag = compiled.execution_envelope.dollar_quote_tag
    assert tag is not None and tag != "$$"
    assert compiled.execution_envelope.statement.count(tag) == 2
    assert "RETURN '$$' AS marker, 7 AS value" in compiled.execution_envelope.statement
