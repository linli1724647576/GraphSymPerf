"""M1 matching topology expands into budgeted, independently scoped MATCH atoms."""

from dataclasses import fields, is_dataclass

import pytest

import ir
from ast_pattern import host_generator as host
from ast_pattern.contracts import ScopeState, StageFailure, TerminalSlot
from population import evaluate_query, synthesize_relational_state


SHAPES = ("node", "edge", "chain", "branch", "converge", "cycle", "disconnected")
FEATURES = ("multi_label", "relationship_alternatives", "property_maps", "named_relationship")


def builder(shape, *, seed=0, features=(), edges=3, capabilities=None):
    grammar = host._grammar(None) | {"match_shapes": (shape,), "match_features": tuple(features)}
    return host._Builder(grammar, capabilities or {}, host._budget(None) | {"match_edges": edges}, seed)


def walk(value):
    yield value
    if isinstance(value, TerminalSlot):
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def concrete_query(clauses):
    return host._validation_tree(ir.Query(None, None, (ir.Alias(ir.Literal(1), "value"),), pipeline=clauses))


@pytest.mark.parametrize("shape", SHAPES)
def test_each_matching_shape_has_a_nonempty_constructible_graph(shape):
    instance = builder(shape)
    clauses = instance.matches(ScopeState(), max_clauses=3)
    query = concrete_query(clauses)
    ir.validate_query(query)
    result = synthesize_relational_state(query, alternatives=(query,), scale=3, data_seed=0,
                                        min_result_rows=1)
    assert evaluate_query(query, result.spec) == [{"value": 1}]
    assert len(clauses) <= 3
    assert any(item["production_id"] == f"match/{shape}" for item in instance.trace)


@pytest.mark.parametrize("shape", SHAPES[2:])
def test_compound_shape_sharing_matches_its_declared_topology(shape):
    clauses = builder(shape).matches(ScopeState(), max_clauses=3)
    pairs = [(part.pattern.left.variable, part.pattern.right.variable) for part in clauses]
    assert all(left != right for left, right in pairs)
    if shape == "chain":
        assert all(pairs[i][1] == pairs[i + 1][0] for i in range(len(pairs) - 1))
    elif shape == "branch":
        assert len({left for left, _ in pairs}) == 1
        assert len({right for _, right in pairs}) == len(pairs)
    elif shape == "converge":
        assert len({right for _, right in pairs}) == 1
        assert len({left for left, _ in pairs}) == len(pairs)
    elif shape == "cycle":
        assert pairs[-1][1] == pairs[0][0]
        assert all(pairs[i][1] == pairs[i + 1][0] for i in range(len(pairs) - 1))
    else:
        assert len({name for pair in pairs for name in pair}) == 2 * len(pairs)


@pytest.mark.parametrize("shape", ("chain", "branch", "converge", "disconnected"))
def test_shape_uses_both_shared_edge_budget_and_remaining_clause_budget(shape):
    for seed in range(10):
        assert len(builder(shape, seed=seed, edges=5).matches(ScopeState(), max_clauses=2)) == 2
    with pytest.raises(StageFailure, match="generation_exhausted"):
        builder(shape, edges=1).matches(ScopeState(), max_clauses=5)


def test_cycle_requires_three_independent_match_clauses():
    with pytest.raises(StageFailure, match="generation_exhausted"):
        builder("cycle").matches(ScopeState(), max_clauses=2)
    assert len(builder("cycle").matches(ScopeState(), max_clauses=3)) == 3


@pytest.mark.parametrize("shape", SHAPES)
def test_optional_shape_preserves_each_optional_clause_and_nullable_exports(shape):
    clauses = builder(shape).matches(ScopeState(), optional=True, max_clauses=3)
    scope = ScopeState()
    for clause in clauses:
        assert clause.optional
        scope = host.advance_scope(clause, scope)
    assert scope.visible_symbols
    assert all(kind.endswith("?") for _, kind in scope.visible_symbols)


def test_matching_features_are_reachable_and_have_concrete_population_semantics():
    observed = set()
    for seed in range(30):
        clauses = builder("edge", seed=seed, features=FEATURES).matches(ScopeState(), max_clauses=1)
        edge = clauses[0].pattern
        if len(edge.left.labels) > 1 or len(edge.right.labels) > 1:
            observed.add("multi_label")
        if edge.relationship.alternatives:
            observed.add("relationship_alternatives")
        if edge.left.properties or edge.right.properties or edge.relationship.properties:
            observed.add("property_maps")
        if edge.relationship.variable:
            observed.add("named_relationship")
        query = concrete_query(clauses)
        result = synthesize_relational_state(query, alternatives=(query,), scale=2, data_seed=0,
                                            min_result_rows=1)
        assert evaluate_query(query, result.spec) == [{"value": 1}]
    assert observed == set(FEATURES)


def test_capabilities_disable_shape_and_feature_expansion():
    instance = builder("edge", features=FEATURES,
                       capabilities={"match_shapes": ["node"], "match_features": []})
    with pytest.raises(StageFailure, match="generation_exhausted"):
        instance.matches(ScopeState(), max_clauses=3)
    instance = builder("edge", features=FEATURES, capabilities={"match_features": []})
    edge = instance.matches(ScopeState(), max_clauses=1)[0].pattern
    assert not edge.relationship.variable and not edge.relationship.alternatives
    assert len(edge.left.labels) == len(edge.right.labels) == 1
    assert not any(value.properties for value in (edge.left, edge.right, edge.relationship))


def test_match_atom_keeps_the_existing_single_clause_boundary():
    clause = builder("edge").match(ScopeState())
    assert isinstance(clause, ir.Match) and isinstance(clause.pattern, ir.Edge)


def test_seed_replays_compound_choices_and_counts_each_ast_occurrence():
    first, second = builder("cycle", seed=7, features=FEATURES), builder("cycle", seed=7, features=FEATURES)
    a, b = first.matches(ScopeState()), second.matches(ScopeState())
    assert a == b and first.trace == second.trace and first.slots == second.slots
    assert sum(is_dataclass(value) and not isinstance(value, TerminalSlot) for value in walk(a)) == first.used_nodes
