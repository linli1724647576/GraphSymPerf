"""The two algorithm stages share a constrained symbolic assignment."""

import json
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace

import pytest


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for part in value:
            yield from walk(part)


def test_recursive_skeleton_roundtrip_preserves_shared_symbols_and_typed_holes():
    from symbolic import Hole, Symbol, SymbolicSkeleton, build_symbolic_skeleton

    skeleton = build_symbolic_skeleton("S6", {"contexts": ["count", "with"], "row_limit": 3}, 2)
    assert any(isinstance(value, Hole) for value in walk(skeleton.source))
    assert any(isinstance(value, Symbol) for value in walk(skeleton.source))
    assert SymbolicSkeleton.from_data(json.loads(json.dumps(skeleton.to_data()))) == skeleton


def test_first_batch_symbolic_archive_without_new_work_ports_remains_readable():
    from symbolic import SymbolicSkeleton, build_symbolic_skeleton

    skeleton = build_symbolic_skeleton("S6", {"contexts": []})
    payload = skeleton.to_data()
    del payload["skeleton"]["intents"]
    assert SymbolicSkeleton.from_data(payload) == skeleton


def test_graph_stage_does_not_publish_or_instantiate_the_final_pair(monkeypatch):
    import symbolic

    skeleton = symbolic.build_symbolic_skeleton("S8", {"contexts": []}, 0)
    monkeypatch.setattr(symbolic, "instantiate_pair", lambda *args: pytest.fail("algorithm 2 ran during algorithm 1"))
    solved = symbolic.construct_state(skeleton, scale=9, data_seed=3)
    assert solved.construction_record["algorithm"] == 1
    assert solved.assignment
    assert solved.population.spec.counts()["nodes"] >= 9
    assert not hasattr(solved, "query_a")


def test_instantiation_rejects_a_binding_changed_after_construction():
    from symbolic import build_symbolic_skeleton, construct_state, instantiate_pair

    skeleton = build_symbolic_skeleton("S6", {"contexts": [], "row_limit": 2}, 0)
    solved = construct_state(skeleton, scale=7, data_seed=3)
    changed = dict(solved.assignment)
    changed["symbols"] = dict(changed["symbols"], unexpected=1)
    with pytest.raises(ValueError, match="binding|assignment|construction"):
        instantiate_pair(skeleton, replace(solved, assignment=changed))


def test_large_graph_cannot_accept_a_different_proof_model_and_expected_result():
    import symbolic
    from population import evaluate_query
    from state import GraphSpec

    skeleton = symbolic.build_symbolic_skeleton("P4", {"contexts": [], "scalar_contexts": []}, 0)
    solved = symbolic.construct_state(skeleton, scale=6000, data_seed=3)
    evidence = deepcopy(solved.population.evidence)
    proof = evidence["validation_graph_spec"]
    for group in proof["node_groups"]:
        if "GspRoot" in group["labels"]:
            group["properties"]["decisive"] = True
        if "keep" in group["properties"]:
            group["properties"]["keep"] = True
    observed = evaluate_query(symbolic._resolve(skeleton.source, solved.assignment), GraphSpec.from_dict(proof))
    assert observed != solved.population.expected_rows
    forged = replace(solved, population=replace(solved.population, evidence=evidence, expected_rows=observed))
    with pytest.raises(ValueError, match="population contract"):
        symbolic.instantiate_pair(skeleton, forged)


def test_same_endpoint_name_in_different_regions_does_not_share_predicate_choices():
    from symbolic import Hole, build_symbolic_skeleton

    skeleton = build_symbolic_skeleton("S6", {"contexts": []}, 0)
    predicates = {value.name for root in (skeleton.source, skeleton.replacement) for value in walk(root)
                  if isinstance(value, Hole) and value.kind == "predicate"}
    assert len(predicates) == 3


def test_scalar_hole_cannot_materialize_a_row_stream():
    from symbolic import Hole, build_symbolic_skeleton, construct_state

    skeleton = build_symbolic_skeleton("P4", {"contexts": []}, 0)
    malformed = replace(skeleton, source=Hole("bad", "scalar", (skeleton.source,)))
    with pytest.raises(ValueError, match="scalar|expression"):
        construct_state(malformed, scale=3, data_seed=3)


def test_scalar_symbol_cannot_carry_a_container():
    from symbolic import Symbol

    with pytest.raises(ValueError, match="scalar|literal"):
        Symbol("bad", "scalar", ([1, 2],))
