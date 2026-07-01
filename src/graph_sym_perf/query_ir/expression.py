"""Conservative Cypher expression IR for redundant predicate transforms."""

from __future__ import annotations

import re

from graph_sym_perf.core import (
    CaseExpr,
    Comparison,
    ExprNode,
    ExpressionRecord,
    FunctionCall,
    LogicalExpr,
    NullCheck,
    PropertyRef,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_COMPLEXITY_RANK = {"low": 0, "medium": 1, "high": 2}


def cypher_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"unsafe Cypher identifier: {value!r}")
    return value


def expression_candidates(
    variable: str,
    *,
    property_names: tuple[str, ...] = (),
    max_complexity: str = "medium",
) -> tuple[ExpressionRecord, ...]:
    """Build safe expression records for a bound node variable."""

    variable = cypher_identifier(variable)
    max_rank = _COMPLEXITY_RANK.get(max_complexity, _COMPLEXITY_RANK["medium"])
    properties = tuple(cypher_identifier(name) for name in property_names if _IDENTIFIER_RE.match(name))

    records: list[ExpressionRecord] = [
        _element_id_identity(variable),
        _labels_identity(variable),
        _label_size_guard(variable),
    ]
    if properties:
        prop = properties[0]
        records.append(_null_partition(variable, prop))
        records.append(_self_equality_guarded(variable, prop))
    records.append(_boolean_combo(variable))
    return tuple(record for record in records if _COMPLEXITY_RANK.get(record.complexity, 99) <= max_rank)


def select_expression(
    variable: str,
    *,
    property_names: tuple[str, ...] = (),
    index: int = 0,
    max_complexity: str = "medium",
) -> ExpressionRecord:
    records = expression_candidates(variable, property_names=property_names, max_complexity=max_complexity)
    if not records:
        raise ValueError("no safe expression candidate is available")
    return records[index % len(records)]


def wrap_as_case_true(expression: ExpressionRecord) -> ExpressionRecord:
    rendered = f"CASE WHEN ({expression.rendered_cypher}) THEN true ELSE true END"
    ast = CaseExpr(
        condition=expression.ast,
        when_true="true",
        when_false="true",
        rendered_cypher=rendered,
        referenced_variables=expression.referenced_variables,
    )
    return ExpressionRecord(
        expr_id=f"{expression.expr_id}.case_true",
        family=expression.family,
        complexity="medium",
        risk_level=expression.risk_level,
        referenced_variables=expression.referenced_variables,
        rendered_cypher=rendered,
        proof_note=(
            expression.proof_note
            + " CASE returns true in both branches, so the expression is a tautological guard."
        ),
        ast=ast,
    )


def _variable_node(variable: str) -> ExprNode:
    return ExprNode(kind="variable", rendered_cypher=variable, referenced_variables=(variable,))


def _literal(value: str) -> ExprNode:
    return ExprNode(kind="literal", rendered_cypher=value)


def _element_id_call(variable: str) -> FunctionCall:
    return FunctionCall(
        function_name="elementId",
        arguments=(_variable_node(variable),),
        rendered_cypher=f"elementId({variable})",
        referenced_variables=(variable,),
    )


def _labels_call(variable: str) -> FunctionCall:
    return FunctionCall(
        function_name="labels",
        arguments=(_variable_node(variable),),
        rendered_cypher=f"labels({variable})",
        referenced_variables=(variable,),
    )


def _element_id_identity(variable: str) -> ExpressionRecord:
    left = _element_id_call(variable)
    right = _element_id_call(variable)
    rendered = f"{left.rendered_cypher} = {right.rendered_cypher}"
    ast = Comparison(
        left=left,
        operator="=",
        right=right,
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"identity.element_id.{variable}",
        family="identity",
        complexity="low",
        risk_level="low",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="elementId(v) is deterministic for a bound node variable and equals itself.",
        ast=ast,
    )


def _labels_identity(variable: str) -> ExpressionRecord:
    left = _labels_call(variable)
    right = _labels_call(variable)
    rendered = f"{left.rendered_cypher} = {right.rendered_cypher}"
    ast = Comparison(
        left=left,
        operator="=",
        right=right,
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"identity.labels.{variable}",
        family="identity",
        complexity="low",
        risk_level="low",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="labels(v) is deterministic for a bound node variable and equals itself.",
        ast=ast,
    )


def _label_size_guard(variable: str) -> ExpressionRecord:
    labels = _labels_call(variable)
    size_call = FunctionCall(
        function_name="size",
        arguments=(labels,),
        rendered_cypher=f"size(labels({variable}))",
        referenced_variables=(variable,),
    )
    rendered = f"{size_call.rendered_cypher} >= 0"
    ast = Comparison(
        left=size_call,
        operator=">=",
        right=_literal("0"),
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"label_membership_guarded.size_labels.{variable}",
        family="label_membership_guarded",
        complexity="low",
        risk_level="low",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="A bound node has a deterministic label list whose size is non-negative.",
        ast=ast,
    )


def _property_ref(variable: str, property_name: str) -> PropertyRef:
    rendered = f"{variable}.{property_name}"
    return PropertyRef(
        variable=variable,
        property_name=property_name,
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )


def _null_partition(variable: str, property_name: str) -> ExpressionRecord:
    prop = _property_ref(variable, property_name)
    left = NullCheck(
        expression=prop,
        is_null=True,
        rendered_cypher=f"{prop.rendered_cypher} IS NULL",
        referenced_variables=(variable,),
    )
    right = NullCheck(
        expression=prop,
        is_null=False,
        rendered_cypher=f"{prop.rendered_cypher} IS NOT NULL",
        referenced_variables=(variable,),
    )
    rendered = f"({left.rendered_cypher} OR {right.rendered_cypher})"
    ast = LogicalExpr(
        operator="OR",
        children=(left, right),
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"null_partition.{variable}.{property_name}",
        family="null_partition",
        complexity="medium",
        risk_level="low",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="A property value partitions into null and not-null without type conversion.",
        ast=ast,
    )


def _self_equality_guarded(variable: str, property_name: str) -> ExpressionRecord:
    prop = _property_ref(variable, property_name)
    equality = Comparison(
        left=prop,
        operator="=",
        right=prop,
        rendered_cypher=f"{prop.rendered_cypher} = {prop.rendered_cypher}",
        referenced_variables=(variable,),
    )
    rendered = f"coalesce({equality.rendered_cypher}, true)"
    ast = FunctionCall(
        function_name="coalesce",
        arguments=(equality, _literal("true")),
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"self_equality_guarded.{variable}.{property_name}",
        family="self_equality_guarded",
        complexity="medium",
        risk_level="medium",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="coalesce(v.prop = v.prop, true) is true for equal values and for null comparisons.",
        ast=ast,
    )


def _boolean_combo(variable: str) -> ExpressionRecord:
    left = _element_id_identity(variable)
    right = _label_size_guard(variable)
    rendered = f"(({left.rendered_cypher}) OR ({right.rendered_cypher}))"
    ast = LogicalExpr(
        operator="OR",
        children=(left.ast, right.ast),
        rendered_cypher=rendered,
        referenced_variables=(variable,),
    )
    return ExpressionRecord(
        expr_id=f"boolean_combo.identity_or_label_guard.{variable}",
        family="boolean_combo",
        complexity="medium",
        risk_level="low",
        referenced_variables=(variable,),
        rendered_cypher=rendered,
        proof_note="Both child expressions are deterministic tautologies over the bound node variable.",
        ast=ast,
    )
