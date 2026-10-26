"""Expanded M1 expressions retain typed ports, NULL and operator restrictions."""

from dataclasses import fields, is_dataclass

import pytest

import ir
from ast_pattern import host_generator
from ast_pattern.contracts import Port, ScopeState, TerminalSlot


def walk(value):
    yield value
    if isinstance(value, (TerminalSlot, Port)):
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def builder(seed, operators, *, port_kind="predicate", comparisons=None, capabilities=None, arity=3):
    grammar = host_generator._grammar({"port_kinds": [port_kind]})
    grammar["expression_operators"] = tuple(operators)
    grammar["comparison_operators"] = tuple(comparisons or ("=", "<>", "<", "<=", ">", ">="))
    limits = host_generator._budget(None) | {"expression_arity": arity}
    return host_generator._Builder(grammar, capabilities or {}, limits, seed)


def validate(expression, *, node=False):
    concrete = host_generator._validation_tree(expression)
    query = ir.Query(ir.Node("n", ("Host",)) if node else None, None,
                     (ir.Alias(concrete, "value"),))
    ir.validate_query(query)


@pytest.mark.parametrize("operator", ["=", "<>", "<", "<=", ">", ">="])
def test_comparison_candidates_are_used_instead_of_fixed_equality_pair(operator):
    generated = builder(4, ("comparison", "leaf"), port_kind="scalar", comparisons=(operator,))
    expression = generated.expression("boolean", ScopeState(), 1, need_port=True)
    assert isinstance(expression, ir.Comparison)
    assert expression.operator == operator
    assert sum(isinstance(part, Port) for part in walk(expression)) == 1
    validate(expression)


def test_comparison_capability_intersects_the_grammar():
    for seed in range(15):
        generated = builder(seed, ("comparison", "leaf"), port_kind="scalar",
                            comparisons=("=", "<", ">"), capabilities={"comparison_operators": (">",)})
        expression = generated.expression("boolean", ScopeState(), 1, need_port=True)
        assert expression.operator == ">"
        validate(expression)


def test_both_null_tests_can_observe_a_typed_port():
    expressions = [builder(seed, ("null_test", "leaf")).expression(
        "boolean", ScopeState(), 1, need_port=True) for seed in range(40)]
    expressions = [expression for expression in expressions if not isinstance(expression, Port)]
    assert {expression.operator for expression in expressions} == {"IS NULL", "IS NOT NULL"}
    for expression in expressions:
        assert expression.right == ir.Literal(None)
        assert sum(isinstance(part, Port) for part in walk(expression)) == 1
        assert host_generator._symbol_type(expression, ScopeState()) == "boolean"
        validate(expression)


@pytest.mark.parametrize("expected", ["integer", "boolean"])
def test_null_is_an_explicit_leaf_without_poisoning_terminal_domains(expected):
    expressions = [builder(seed, ("leaf",)).expression(expected, ScopeState(), 0) for seed in range(80)]
    assert ir.Literal(None) in expressions
    assert any(isinstance(expression.value, TerminalSlot) for expression in expressions)
    for expression in expressions:
        if isinstance(expression.value, TerminalSlot):
            assert None not in expression.value.candidates
            assert expression.value.type == expected
        else:
            assert expression.value is None
            assert host_generator._symbol_type(expression, ScopeState()) == "null"
        validate(expression)


@pytest.mark.parametrize("operator", ["and", "or", "coalesce"])
def test_nary_expressions_obey_arity_and_keep_exactly_one_port(operator):
    expected = "integer" if operator == "coalesce" else "boolean"
    kind = "scalar" if expected == "integer" else "predicate"
    arities = set()
    for seed in range(50):
        generated = builder(seed, (operator, "leaf"), port_kind=kind)
        expression = generated.expression(expected, ScopeState(), 1, need_port=True)
        if isinstance(expression, Port):
            continue
        parts = expression.args if isinstance(expression, ir.Call) else expression.terms
        arities.add(len(parts))
        assert 2 <= len(parts) <= 3
        assert sum(isinstance(part, Port) for part in walk(expression)) == 1
        validate(expression)
    assert arities == {2, 3}


@pytest.mark.parametrize("operator", ["and", "or", "coalesce"])
def test_binary_arity_budget_is_honored(operator):
    expected = "integer" if operator == "coalesce" else "boolean"
    kind = "scalar" if expected == "integer" else "predicate"
    for seed in range(15):
        generated = builder(seed, (operator, "leaf"), port_kind=kind, arity=2)
        expression = generated.expression(expected, ScopeState(), 1, need_port=True)
        if not isinstance(expression, Port):
            parts = expression.args if isinstance(expression, ir.Call) else expression.terms
            assert len(parts) == 2


def test_ordered_comparisons_never_require_an_unknown_property_type():
    scope = ScopeState(visible_symbols=(("n", "node"),))
    for seed in range(50):
        generated = builder(seed, ("comparison", "leaf", "case", "coalesce", "count"),
                            port_kind="scalar", comparisons=("<", "<=", ">", ">="))
        expression = generated.expression("boolean", scope, 2, need_port=True)
        validate(expression, node=True)
        assert sum(isinstance(part, Port) for part in walk(expression)) == 1
