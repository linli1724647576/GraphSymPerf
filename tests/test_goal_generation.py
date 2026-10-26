"""Semantic goals, explicit sites and bindings share the public two-stage path."""

import json
from dataclasses import fields, is_dataclass, replace

import pytest

import ir
from dependencies import find_mutation_sites, node_at, replace_at
from executor import typed_bag
from main import prepare_case
from population import evaluate_query
from query import parse_query
from state import GraphSpec
from symbolic import Symbol, SymbolicSkeleton, build_symbolic_skeleton, construct_state, instantiate_pair


def walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from walk(item, (*address, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, (*address, key))


def assert_bags(pair, solved):
    expected = typed_bag(solved.population.expected_rows)
    for query in (pair.query_a, pair.query_b):
        ir.validate_query(query)
        assert ir.to_data(parse_query(query.render())) == ir.to_data(query)
        assert typed_bag(evaluate_query(query, solved.population.spec)) == expected
    assert pair.query_a != pair.query_b


def assert_public_case(case):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"], "an active output goal must not be accepted as an empty bag"
    graph = GraphSpec.from_dict(case["graph_spec"])
    for side in ("a", "b"):
        query = ir.from_data(case["query_ir"][side])
        assert typed_bag(evaluate_query(query, graph)) == typed_bag(case["expected_rows"])
        assert ir.to_data(parse_query(case[f"query_{side}"])) == ir.to_data(query)
    first, second = case["construction_record"], case["instantiation_record"]
    assert first["algorithm"] == 1 and second["algorithm"] == 2
    assert first["graph_digest"] == second["graph_digest"]
    assert first["assignment_digest"] == second["uses_construction_assignment"]


@pytest.mark.parametrize("pattern,options", [
    ("P10", {"form": "chain", "chain_length": 3}),
    ("S3", {"conditional_depth": 2, "guard_kind": "count"}),
])
def test_goal_skeleton_declares_observations_and_work_without_per_edge_quantities(pattern, options):
    skeleton = build_symbolic_skeleton(pattern, {"contexts": [], **options}, 0)
    assert skeleton.mark is not None
    assert skeleton.goals
    assert not any(item.get("kind") == "quantity" for item in skeleton.intents)
    assert not any(isinstance(value, (ir.Node, ir.Edge)) for _, value in walk(skeleton.goals))
    assert all(not ({"exact_matches", "candidate_matches", "valid", "growth", "subject"} & set(value))
               for _, value in walk(skeleton.goals) if isinstance(value, dict))
    restored = SymbolicSkeleton.from_data(json.loads(json.dumps(skeleton.to_data())))
    assert restored == skeleton


@pytest.mark.parametrize("length", [2, 3, 5])
def test_chain_length_is_syntax_and_every_segment_reuses_binding_population(length):
    case = prepare_case(None, "P10", 7, 0, 3,
                        composition={"contexts": [], "form": "chain", "chain_length": length})
    assert_public_case(case)
    query = ir.from_data(case["query_ir"]["a"])
    parts = ir.clauses(query)
    assert len(parts) == length
    assert all(isinstance(part, ir.Match) and isinstance(part.pattern, ir.Edge) for part in parts)
    for first, second in zip(parts, parts[1:]):
        assert first.pattern.right.variable == second.pattern.left.variable
    before = [part.pattern for part in parts]
    after = [part.pattern for part in ir.clauses(ir.from_data(case["query_ir"]["b"]))]
    assert len(after) == length
    assert sorted(map(repr, before)) == sorted(map(repr, after))
    assert len([1 for left, right in zip(before, after) if left != right]) == 2
    assert len(case["expected_rows"][0]) == length + 1


def test_default_chain_grammar_can_sample_more_than_one_structural_length():
    lengths = set()
    for seed in range(6):
        case = prepare_case(None, "P10", 7, seed, 3,
                            composition={"contexts": [], "form": "chain"})
        assert_public_case(case)
        lengths.add(len(ir.clauses(ir.from_data(case["query_ir"]["a"]))))
    assert len(lengths) > 1
    assert min(lengths) >= 2


def test_same_label_chain_uses_origin_correspondence_instead_of_schema_guessing():
    skeleton = build_symbolic_skeleton("P10", {"contexts": [], "form": "chain", "chain_length": 4}, 0)

    def shared_labels(value):
        if isinstance(value, Symbol) and value.kind == "identifier" and value.name.startswith("label:") and value.name != "label:anchor":
            return replace(value, candidates=("Shared",))
        if is_dataclass(value):
            return replace(value, **{field.name: shared_labels(getattr(value, field.name)) for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(shared_labels(item) for item in value)
        if isinstance(value, dict):
            return {key: shared_labels(item) for key, item in value.items()}
        return value

    skeleton = shared_labels(skeleton)
    solved = construct_state(skeleton, scale=7, data_seed=3)
    pair = instantiate_pair(skeleton, solved)
    assert_bags(pair, solved)
    assert solved.population.expected_rows
    assert {node.labels for _, node in walk(pair.query_a) if isinstance(node, ir.Node) and not node.properties} == {("Shared",)}
    for row in solved.population.expected_rows:
        assert len(set(row.values())) == len(row), "same schema does not identify the chain's distinct binding domains"


@pytest.mark.parametrize("predicate_kind", ["none", "numeric"])
def test_chain_work_goal_uses_the_actual_match_tests_instead_of_a_keep_recipe(predicate_kind):
    skeleton = build_symbolic_skeleton("P10", {"contexts": [], "form": "chain", "chain_length": 4}, 0)
    clauses = ir.clauses(skeleton.source)
    changed = []
    for index, clause in enumerate(clauses):
        predicate = (None if predicate_kind == "none" else
                     ir.Comparison(ir.Property(clause.pattern.right.variable, "score"), "=", ir.Literal(index + 10)))
        changed.append(replace(clause, where=predicate))
    source = ir.with_clauses(skeleton.source, tuple(changed))
    skeleton = replace(skeleton, source=source)
    solved = construct_state(skeleton, scale=7, data_seed=3)
    pair = instantiate_pair(skeleton, solved)
    assert_bags(pair, solved)
    assert solved.population.expected_rows
    assert all(not (isinstance(node, ir.Property) and node.key == "keep") for _, node in walk(pair.query_a))


@pytest.mark.parametrize("pattern,extra,threshold", [
    ("P10", {"form": "chain", "chain_length": 3}, 9),
    ("S3", {"conditional_depth": 2, "guard_kind": "count"}, 2),
])
def test_outer_count_threshold_propagates_inward_instead_of_accepting_empty_output(pattern, extra, threshold):
    low = prepare_case(None, pattern, 7, 0, 3,
                       composition={"contexts": ["count", "with"], "threshold": 0, **extra})
    high = prepare_case(None, pattern, 7, 0, 3,
                        composition={"contexts": ["count", "with"], "threshold": threshold, **extra})
    assert_public_case(low)
    assert_public_case(high)
    assert any(type(value) is int and value > threshold
               for row in high["expected_rows"] for key, value in row.items() if not key.endswith(".uid"))
    assert GraphSpec.from_dict(high["graph_spec"]).counts()["nodes"] > GraphSpec.from_dict(low["graph_spec"]).counts()["nodes"]


@pytest.mark.parametrize("guard_kind", ["property", "count", "exists"])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_nested_s3_keeps_native_conditionals_and_constructs_real_guard_requirements(guard_kind, depth):
    case = prepare_case(None, "S3", 7, 0, 3,
                        composition={"contexts": ["count", "with"], "threshold": 2,
                                     "conditional_depth": depth, "guard_kind": guard_kind})
    assert_public_case(case)
    before = ir.from_data(case["query_ir"]["a"])
    after = ir.from_data(case["query_ir"]["b"])
    source_when = [(path, node) for path, node in walk(before) if isinstance(node, ir.ConditionalQuery)]
    target_when = [(path, node) for path, node in walk(after) if isinstance(node, ir.ConditionalQuery)]
    assert len(source_when) == len(target_when) == depth
    assert all(not isinstance(node.condition, ir.Literal) for _, node in source_when)
    assert case["query_a"].count("CYPHER 25") == 1
    assert case["query_a"].count("WHEN ") == depth
    assert case["bindings"]["premise_checks"]
    assert all("(true = true)" not in check["query"] for check in case["bindings"]["premise_checks"])


def test_explicit_mark_can_select_the_outer_or_inner_s3_without_requiring_a_unique_law():
    skeleton = build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 2,
                                               "guard_kind": "property"}, 0)
    initial = construct_state(skeleton, scale=7, data_seed=3)
    original = instantiate_pair(skeleton, initial).query_a
    sites = find_mutation_sites(original, "S3", "conditional")
    assert len(sites) == 2
    observed = set()
    for site in sites:
        marked = replace(skeleton, mark=replace(skeleton.mark, address=site.address))
        solved = construct_state(marked, scale=7, data_seed=3)
        pair = instantiate_pair(marked, solved)
        assert_bags(pair, solved)
        selected = tuple(pair.mutation_record["site"]["address"])
        assert selected == site.address
        assert node_at(pair.query_a, selected) != node_at(pair.query_b, selected)
        assert replace_at(pair.query_b, selected, node_at(pair.query_a, selected)) == pair.query_a
        observed.add(selected)
    assert len(observed) == 2


def test_negated_exists_guard_propagates_a_false_requirement_to_its_graph_read():
    skeleton = build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 1,
                                               "guard_kind": "exists"}, 0)
    assert isinstance(skeleton.source, ir.ConditionalQuery)
    skeleton = replace(skeleton, source=replace(skeleton.source,
                                                condition=ir.Negation(skeleton.source.condition)))
    solved = construct_state(skeleton, scale=7, data_seed=3)
    pair = instantiate_pair(skeleton, solved)
    assert_bags(pair, solved)
    assert solved.population.expected_rows
    assert isinstance(pair.query_a.condition, ir.Negation)
    check = ir.Query(None, None, (ir.Alias(pair.query_a.condition, "condition"),
                                 ir.Alias(pair.query_a.condition.expression, "exists")))
    assert evaluate_query(check, solved.population.spec) == [{"condition": True, "exists": False}]


def test_wrong_explicit_mark_is_rejected_before_graph_construction():
    skeleton = build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 2}, 0)
    malformed = replace(skeleton, mark=replace(skeleton.mark, address=("not_a_real_site",)))
    with pytest.raises(ValueError, match="mark|site|address"):
        construct_state(malformed, scale=7, data_seed=3)


def test_older_skeleton_payload_without_mark_and_goals_remains_readable():
    skeleton = build_symbolic_skeleton("S6", {"contexts": []}, 0)
    payload = skeleton.to_data()
    payload["skeleton"].pop("mark", None)
    payload["skeleton"].pop("goals", None)
    assert SymbolicSkeleton.from_data(payload) == skeleton
