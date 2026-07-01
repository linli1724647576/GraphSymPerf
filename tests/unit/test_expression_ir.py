from __future__ import annotations

from graph_sym_perf.query_ir import expression_candidates, select_expression, wrap_as_case_true


def test_expression_ir_renders_safe_families_and_variable_refs() -> None:
    records = expression_candidates("n0", property_names=("id",), max_complexity="medium")
    families = {record.family for record in records}

    assert {"identity", "null_partition", "self_equality_guarded", "label_membership_guarded", "boolean_combo"} <= families
    assert all(record.referenced_variables == ("n0",) for record in records)
    assert any(record.rendered_cypher == "(n0.id IS NULL OR n0.id IS NOT NULL)" for record in records)


def test_expression_ir_respects_complexity_cap() -> None:
    records = expression_candidates("n0", property_names=("id",), max_complexity="low")

    assert records
    assert all(record.complexity == "low" for record in records)


def test_case_true_wrapper_keeps_original_expression_metadata() -> None:
    expression = select_expression("n0", index=0)
    wrapped = wrap_as_case_true(expression)

    assert wrapped.family == expression.family
    assert wrapped.referenced_variables == ("n0",)
    assert wrapped.rendered_cypher.startswith("CASE WHEN")
    assert "THEN true ELSE true END" in wrapped.rendered_cypher


def test_expression_ir_supports_portable_dialect() -> None:
    records = expression_candidates("n0", property_names=("id",), dialect="portable")

    assert records
    assert records[0].family == "literal_tautology"
    assert records[0].rendered_cypher == "true"
