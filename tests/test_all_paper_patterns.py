"""Every paper rule uses the public symbolic construction and query-pair path."""

import json
from dataclasses import fields, is_dataclass, replace

import pytest

from executor import typed_bag
from ir import Alias, Edge, Match, Node, Property, clauses, from_data, to_data, validate_query
from main import prepare_case
from population import evaluate_query
from query import parse_query, projection_names
from state import GraphSpec


PAPER_PATTERNS = tuple(f"P{i}" for i in range(1, 14)) + tuple(f"S{i}" for i in range(1, 11))
CONTEXTS = (("count",), ("exists",), ("with",), ("with", "exists", "count", "with"))


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk(item)


def assert_constructed_pair(case, pattern):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["pattern_catalog"] == "paper-v1"
    assert case["pattern"] == case["paper_pattern"] == pattern
    assert case["graph_spec"]["version"] == 2
    assert case["graph_spec"]["pattern"] == "AST"
    assert case["symbolic_skeleton"]
    construction, instantiation = case["construction_record"], case["instantiation_record"]
    assert construction["algorithm"] == 1
    assert instantiation["algorithm"] == 2
    assert construction["skeleton_digest"] == instantiation["skeleton_digest"]
    assert construction["assignment_digest"] == instantiation["uses_construction_assignment"]
    assert construction["graph_digest"] == instantiation["graph_digest"]
    assert instantiation["unchanged_graph"] is True
    assert case["mutation_record"]["direction"] == ("L_to_H" if pattern.startswith("S") else "symmetry")
    assert case["mutation_record"]["propagation"]
    graph = GraphSpec.from_dict(case["graph_spec"])
    observed = []
    for side in ("a", "b"):
        tree = from_data(case["query_ir"][side])
        validate_query(tree)
        assert to_data(parse_query(case[f"query_{side}"])) == to_data(tree)
        observed.append(typed_bag(evaluate_query(tree, graph)))
    assert observed[0] == observed[1] == typed_bag(case["expected_rows"])
    assert (case["query_a"] != case["query_b"]
            or case.get("query_bindings", {}).get("a") != case.get("query_bindings", {}).get("b"))


@pytest.mark.parametrize("pattern", PAPER_PATTERNS)
def test_every_paper_pattern_constructs_a_standalone_pair_in_one_shared_state(pattern):
    case = prepare_case(None, pattern, 7, 0, 3, composition={"contexts": []})
    assert_constructed_pair(case, pattern)


@pytest.mark.parametrize("pattern", PAPER_PATTERNS)
@pytest.mark.parametrize("contexts", CONTEXTS)
def test_every_paper_pattern_preserves_full_bags_in_legal_recursive_contexts(pattern, contexts):
    case = prepare_case(None, pattern, 7, 0, 3,
                        composition={"contexts": list(contexts), "threshold": 0})
    assert_constructed_pair(case, pattern)


@pytest.mark.parametrize("form", ["independent", "shared_anchor", "chain"])
def test_p10_explicit_form_selects_the_actual_binding_structure(form):
    case = prepare_case(None, "P10", 7, 0, 3, composition={"contexts": [], "form": form})
    assert_constructed_pair(case, "P10")
    assert case["pattern_form"] == form
    before, after = [clauses(from_data(case["query_ir"][side])) for side in ("a", "b")]
    changed = [index for index, (left, right) in enumerate(zip(before, after)) if left != right]
    assert len(before) == len(after) and len(changed) == 2
    first, second = (before[index].pattern for index in changed)
    first_variables = {node.variable for node in walk(first) if isinstance(node, Node)}
    second_variables = {node.variable for node in walk(second) if isinstance(node, Node)}
    if form == "independent":
        assert first_variables.isdisjoint(second_variables)
    elif form == "shared_anchor":
        assert len(first_variables & second_variables) == 1
        assert first_variables - second_variables and second_variables - first_variables
    else:
        assert isinstance(first, Edge) and isinstance(second, Edge)
        assert first.right.variable == second.left.variable
        assert first.left.variable != second.right.variable


@pytest.mark.parametrize("form", ["independent", "chain"])
@pytest.mark.parametrize("contexts", CONTEXTS)
def test_other_p10_forms_reuse_the_same_recursive_context_pipeline(form, contexts):
    case = prepare_case(None, "P10", 7, 0, 3,
                        composition={"contexts": list(contexts), "form": form})
    assert_constructed_pair(case, "P10")
    assert case["pattern_form"] == form


@pytest.mark.parametrize("match_state,left_missing,right_missing", [
    ("both", False, False), ("left_only", False, True),
    ("right_only", True, False), ("neither", True, True),
])
def test_p11_optional_branches_keep_all_four_null_extension_states(match_state, left_missing, right_missing):
    case = prepare_case(None, "P11", 7, 0, 3,
                        composition={"contexts": [], "match_state": match_state})
    assert_constructed_pair(case, "P11")
    tree = from_data(case["query_ir"]["a"])
    optional = [part for part in clauses(tree) if isinstance(part, Match) and part.optional]
    assert len(optional) == 2
    endpoints = [{part.pattern.left.variable, part.pattern.right.variable} for part in optional]
    anchor, = endpoints[0] & endpoints[1]
    branch_variables = [next(iter(variables - {anchor})) for variables in endpoints]
    rows = case["expected_rows"]
    assert rows, "the mandatory anchor must retain a NULL-extended row when optional matches are absent"
    for variable, missing in zip(branch_variables, (left_missing, right_missing)):
        names = []
        for expression, name in zip(tree.returns, projection_names(tree)):
            expression = expression.expression if isinstance(expression, Alias) else expression
            if isinstance(expression, Property) and expression.variable == variable and expression.key == "uid":
                names.append(name)
        assert names, "the generated pair must expose optional endpoint identities to check NULL extension"
        assert all((row[name] is None) is missing for row in rows for name in names)


@pytest.mark.parametrize("pattern,extra", [
    ("P6", {}), ("P7", {}), ("P10", {"form": "chain"}),
    ("P11", {"match_state": "left_only"}), ("S1", {}), ("S9", {}),
])
def test_identifier_renaming_uses_actual_typed_ast_without_schema_word_recipes(pattern, extra):
    from symbolic import Symbol, SymbolicSkeleton, build_symbolic_skeleton, construct_state, instantiate_pair

    skeleton = build_symbolic_skeleton(pattern, {"contexts": [], **extra}, 0)
    identifiers = sorted({candidate for node in walk(skeleton)
                          if isinstance(node, Symbol) and node.kind == "identifier"
                          for candidate in node.candidates})
    assert identifiers
    renamed_identifiers = {value: f"Renamed_{index:03d}" for index, value in enumerate(identifiers)}

    def rename(value):
        if isinstance(value, Symbol) and value.kind == "identifier":
            return replace(value, candidates=tuple(renamed_identifiers[item] for item in value.candidates))
        if is_dataclass(value):
            return replace(value, **{field.name: rename(getattr(value, field.name)) for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(rename(item) for item in value)
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        return value

    renamed = rename(skeleton)
    restored = SymbolicSkeleton.from_data(json.loads(json.dumps(renamed.to_data())))
    assert restored == renamed
    assert {candidate for node in walk(restored)
            if isinstance(node, Symbol) and node.kind == "identifier"
            for candidate in node.candidates} == set(renamed_identifiers.values())
    original_model = construct_state(skeleton, scale=7, data_seed=3)
    renamed_model = construct_state(restored, scale=7, data_seed=3)
    assert original_model.population.spec.counts() == renamed_model.population.spec.counts()
    original_pair = instantiate_pair(skeleton, original_model)
    renamed_pair = instantiate_pair(restored, renamed_model)
    bags = []
    for pair, solved in ((original_pair, original_model), (renamed_pair, renamed_model)):
        expected = typed_bag(solved.population.expected_rows)
        for tree in (pair.query_a, pair.query_b):
            saved = json.loads(json.dumps(to_data(tree)))
            assert to_data(from_data(saved)) == saved
            assert typed_bag(evaluate_query(tree, solved.population.spec)) == expected
        bags.append(expected)
    assert bags[0] == bags[1]
