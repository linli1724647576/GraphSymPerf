"""Every semantic truth assumption is checked after loading the accepted state."""
from dataclasses import replace
from copy import deepcopy

import pytest

import ir
from dependencies import find_mutation_sites, node_at, replace_at
from main import prepare_case
from population import evaluate_query
from query import parse_query
from state import GraphSpec
from symbolic import build_symbolic_skeleton, construct_state, instantiate_pair, _resolve, _assignment


def test_all_ancestor_guards_are_mandatory_checks_not_only_the_marked_guard():
    case = prepare_case(None, "S3", 7, 0, 3,
                        composition={"contexts": ["count", "with"], "threshold": 2,
                                     "conditional_depth": 3, "guard_kind": "count"})
    checks = case["bindings"]["goal_checks"]
    assert len(checks) >= 3
    proof = GraphSpec.from_dict(case["population_evidence"]["validation_graph_spec"])
    for check in checks:
        assert check in case["checks"] and check in case["bindings"]["required_state_checks"]
        assert evaluate_query(parse_query(check["query"]), proof) == check["expected_rows"]
        assert all(row["premise"] is True for row in check["expected_rows"])


def test_false_ancestor_truth_is_observed_by_a_real_state_query():
    skeleton = build_symbolic_skeleton("S3", {"contexts": [], "conditional_depth": 2, "guard_kind": "count"})
    concrete = _resolve(skeleton.source, _assignment(skeleton))
    outer = min(find_mutation_sites(concrete, "S3", "conditional"), key=lambda site: len(site.address))
    address = tuple(outer.guards["envelope_site"])
    original = node_at(skeleton.source, address)
    changed = replace(original, then=original.otherwise, otherwise=original.then)
    skeleton = replace(skeleton, source=replace_at(skeleton.source, address, changed),
                       mark=replace(skeleton.mark, address=(*address, "otherwise", "otherwise")))
    solved = construct_state(skeleton, scale=7, data_seed=3)
    pair = instantiate_pair(skeleton, solved)
    facts = solved.construction_record["requirements"]["obligations"]
    assert any(fact["value"] is False for fact in facts)
    checks = pair.bindings["goal_checks"]
    assert len(checks) >= 2
    assert any("= false" in check["query"] for check in checks)
    assert all(check in pair.checks for check in checks)
    assert solved.population.expected_rows


def test_requirement_record_cannot_drift_from_the_accepted_population_contract():
    skeleton = build_symbolic_skeleton("P10", {"contexts": [], "form": "chain"})
    solved = construct_state(skeleton, scale=7, data_seed=3)
    record = deepcopy(solved.construction_record)
    record["requirements"]["derivation"]["output_min_rows"] = 100
    with pytest.raises(ValueError, match="semantic requirement contract"):
        instantiate_pair(skeleton, replace(solved, construction_record=record))
