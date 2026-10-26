"""Paper S6/S8 travel through generation, population, mutation and archival."""

import json
from dataclasses import fields, is_dataclass, replace
from itertools import product

import pytest

from executor import typed_bag
from ir import RowLimit, UnionAll, from_data
from generator import PatternNotApplicable
from main import COMPOSITION_EXTENSION_FIELDS, measurement_settings, prepare_case, read_case, save_case
from population import evaluate_query
from state import GraphSpec


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from walk(item)


def test_s6_keeps_q0_union_and_returns_the_requested_constant_bag():
    case = prepare_case(None, "S6", 7, 0, 3,
                        composition={"contexts": [], "row_limit": 3})

    assert "compile_error" not in case
    assert case["pattern_catalog"] == "paper-v1"
    assert case["paper_pattern"] == "S6"
    trees = {side: from_data(case["query_ir"][side]) for side in ("a", "b")}
    assert all(isinstance(tree, RowLimit) and tree.limit == 3 for tree in trees.values())
    assert all(isinstance(tree.body, UnionAll) for tree in trees.values())
    unions = {side: [node for node in walk(tree) if isinstance(node, UnionAll)]
              for side, tree in trees.items()}
    assert all(len(parts) == 1 for parts in unions.values())
    assert unions["a"][0].branches[0] == unions["b"][0].branches[0]
    assert unions["a"][0].branches[1] != unions["b"][0].branches[1]
    assert all("UNION ALL" in case[f"query_{side}"] for side in trees)

    spec = GraphSpec.from_dict(case["graph_spec"])
    results = [evaluate_query(tree, spec) for tree in trees.values()]
    assert len(case["expected_rows"]) == 3
    assert all(row == case["expected_rows"][0] for row in case["expected_rows"])
    assert all(typed_bag(rows) == typed_bag(case["expected_rows"]) for rows in results)


@pytest.mark.parametrize("pattern", ["S6", "S8"])
def test_symbolic_skeleton_roundtrip_keeps_the_same_construction_assignment(pattern):
    from symbolic import (Hole, Symbol, SymbolicSkeleton, build_symbolic_skeleton,
                          construct_state, instantiate_pair)

    options = {"contexts": []}
    if pattern == "S6":
        options["row_limit"] = 3
    skeleton = build_symbolic_skeleton(pattern, options, 0)
    saved = json.loads(json.dumps(skeleton.to_data()))
    restored = SymbolicSkeleton.from_data(saved)
    assert restored.to_data() == saved
    assert any(isinstance(node, Hole) for node in walk(restored))
    assert any(isinstance(node, Symbol) for node in walk(restored))

    solved = construct_state(restored, scale=7, data_seed=3)
    pair = instantiate_pair(restored, solved)
    assert restored.to_data() == saved
    assert pair.instantiation_record["uses_construction_assignment"] == solved.construction_record["assignment_digest"]
    assert pair.instantiation_record["graph_digest"] == solved.construction_record["graph_digest"]
    assert pair.mutation_record["direction"] == "L_to_H"
    with pytest.raises(ValueError, match="changed"):
        instantiate_pair(restored, replace(solved, assignment={"changed": True}))


@pytest.mark.parametrize("pattern", ["P3", "P4", "P10", "S6", "S8"])
def test_all_composition_entries_keep_the_accepted_model_through_compilation(pattern):
    case = prepare_case(None, pattern, 7, 0, 3, composition={"contexts": ["count", "with"]})
    assert "compile_error" not in case
    construction, instantiation = case["construction_record"], case["instantiation_record"]
    assert construction["algorithm"] == 1
    assert instantiation["algorithm"] == 2
    assert construction["skeleton_digest"] == instantiation["skeleton_digest"]
    assert construction["assignment_digest"] == instantiation["uses_construction_assignment"]
    assert construction["graph_digest"] == instantiation["graph_digest"]
    assert instantiation["unchanged_graph"] is True
    assert case["mutation_record"]["direction"] == ("L_to_H" if pattern.startswith("S") else "symmetry")
    spec = GraphSpec.from_dict(case["graph_spec"])
    for tree in case["query_ir"].values():
        assert typed_bag(evaluate_query(from_data(tree), spec)) == typed_bag(case["expected_rows"])


@pytest.mark.parametrize("pattern", ["S6", "S8"])
def test_short_circuit_ignores_different_replacement_bags_because_q0_is_unchanged(pattern):
    options = {"contexts": []}
    if pattern == "S6":
        options["row_limit"] = 3
    case = prepare_case(None, pattern, 11, 0, 3, composition=options)
    trees = [from_data(case["query_ir"][side]) for side in ("a", "b")]
    unions = [next(node for node in walk(tree) if isinstance(node, UnionAll)) for tree in trees]
    spec = GraphSpec.from_dict(case["graph_spec"])
    witness_rows = evaluate_query(unions[0].branches[0], spec)
    assert len(witness_rows) == (3 if pattern == "S6" else 1)
    assert unions[0].branches[0] == unions[1].branches[0]
    assert len(evaluate_query(unions[0].branches[1], spec)) == 1
    assert len(evaluate_query(unions[1].branches[1], spec)) == 11
    assert typed_bag(evaluate_query(trees[0], spec)) == typed_bag(evaluate_query(trees[1], spec))
    if pattern == "S8":
        assert case["expected_rows"] == [{"u.uid": 1, "value": True}]


def test_s6_limit_changes_witness_population_and_the_complete_result_bag():
    cases = [prepare_case(None, "S6", 11, 0, 3,
                          composition={"contexts": [], "row_limit": limit}) for limit in (2, 5)]
    for case, limit in zip(cases, (2, 5)):
        tree = from_data(case["query_ir"]["a"])
        assert isinstance(tree, RowLimit) and tree.limit == limit
        spec = GraphSpec.from_dict(case["graph_spec"])
        assert len(evaluate_query(tree.body.branches[0], spec)) == limit
        assert len(case["expected_rows"]) == limit
    assert cases[0]["construction_record"]["graph_digest"] != cases[1]["construction_record"]["graph_digest"]


@pytest.mark.parametrize("pattern,contexts,threshold,expected", [
    ("S6", ["count", "with"], 2, 3),
    ("S8", ["with", "exists", "count", "with"], 0, 1),
])
def test_nested_consumers_preserve_the_short_circuit_envelope(pattern, contexts, threshold, expected):
    options = {"contexts": contexts, "threshold": threshold}
    if pattern == "S6":
        options["row_limit"] = 3
    case = prepare_case(None, pattern, 11, 0, 3, composition=options)
    assert len(case["expected_rows"]) == 1
    row = case["expected_rows"][0]
    assert row["u.uid"] == 1
    assert [value for name, value in row.items() if name != "u.uid"] == [expected]
    assert len(case["mutation_record"]["propagation"]) > 1
    spec = GraphSpec.from_dict(case["graph_spec"])
    for value in case["query_ir"].values():
        tree = from_data(value)
        assert len([node for node in walk(tree) if isinstance(node, UnionAll)]) == 1
        assert typed_bag(evaluate_query(tree, spec)) == typed_bag(case["expected_rows"])


@pytest.mark.parametrize("pattern,threshold,extra", [("S6", 3, {"row_limit": 3}), ("S8", 1, {})])
def test_outer_count_conflict_is_rejected_instead_of_adding_success_rows(pattern, threshold, extra):
    with pytest.raises(PatternNotApplicable, match="satisf|conflict|compatible"):
        prepare_case(None, pattern, 11, 0, 3,
                     composition={"contexts": ["count", "with"], "threshold": threshold, **extra})


@pytest.mark.parametrize("pattern,row_limit", [("S8", 3), ("S6", 0), ("S6", True)])
def test_limit_parameter_is_typed_and_belongs_only_to_s6(pattern, row_limit):
    with pytest.raises(PatternNotApplicable, match="row_limit"):
        prepare_case(None, pattern, 3, 0, 1, composition={"contexts": [], "row_limit": row_limit})


@pytest.mark.parametrize("field", ["symbolic_skeleton", "construction_record", "instantiation_record"])
def test_archival_roundtrip_protects_each_symbolic_phase_record(tmp_path, field):
    case = prepare_case(None, "S6", 7, 0, 3, composition={"contexts": [], "row_limit": 3})
    save_case(tmp_path, case, measurement_settings({}), {"status": "no_candidate"})
    restored, _ = read_case(tmp_path)
    assert restored == case
    path = tmp_path / "result.json"
    archive = json.loads(path.read_text())
    archive["case"][field] = {"changed": True}
    path.write_text(json.dumps(archive))
    with pytest.raises(ValueError, match="metadata|changed|extension"):
        read_case(tmp_path)


def test_symbolic_archive_cannot_remove_phase_records_and_evidence_hash(tmp_path):
    case = prepare_case(None, "S8", 7, 0, 3, composition={"contexts": []})
    save_case(tmp_path, case, measurement_settings({}), {"status": "no_candidate"})
    path = tmp_path / "result.json"
    archive = json.loads(path.read_text())
    archive.pop("extension_hash")
    for field in COMPOSITION_EXTENSION_FIELDS:
        archive["case"].pop(field, None)
    path.write_text(json.dumps(archive))
    with pytest.raises(ValueError, match="extension hash"):
        read_case(tmp_path)


CONTEXT_PRODUCTS = [context for depth in range(4)
                    for context in product(("exists", "count", "with"), repeat=depth)]
DEEP_CONTEXTS = [
    ("with", "exists", "count", "with", "count", "exists", "with", "count"),
    ("count", "with", "with", "exists", "with", "count", "with", "exists"),
]


@pytest.mark.parametrize("pattern", ["S6", "S8"])
@pytest.mark.parametrize("contexts", CONTEXT_PRODUCTS + DEEP_CONTEXTS)
def test_recursive_operator_products_keep_union_and_complete_typed_bags(pattern, contexts):
    options = {"contexts": list(contexts), "threshold": 0}
    if pattern == "S6":
        options["row_limit"] = 3
    case = prepare_case(None, pattern, 11, 0, 3, composition=options)
    assert "compile_error" not in case
    spec = GraphSpec.from_dict(case["graph_spec"])
    trees = [from_data(case["query_ir"][side]) for side in ("a", "b")]
    unions = []
    for tree in trees:
        found = [node for node in walk(tree) if isinstance(node, UnionAll)]
        assert len(found) == 1
        unions.append(found[0])
        assert typed_bag(evaluate_query(tree, spec)) == typed_bag(case["expected_rows"])
    assert unions[0].branches[0] == unions[1].branches[0]
    assert len(evaluate_query(unions[0].branches[0], spec)) == (3 if pattern == "S6" else 1)
    assert len(evaluate_query(unions[0].branches[1], spec)) == 1
    assert len(evaluate_query(unions[1].branches[1], spec)) == 11
    assert case["mutation_record"]["direction"] == "L_to_H"
