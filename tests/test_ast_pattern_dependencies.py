"""Logical demand and full-result relations have independent evidence states."""

from dataclasses import replace

import ir
import pytest

from ast_pattern.contracts import (EmbeddedAst, GraphState, HostAst,
                                   PatternInstance, StageFailure, digest)
from ast_pattern.dependency_check import _shared_reads, check_dependencies
from population import evaluate_query
from state import GraphSpec
from symbolic import build_symbolic_skeleton, construct_state, instantiate_pair


def dependency_fixture(pattern, options=None, *, scale=5):
    skeleton = build_symbolic_skeleton(pattern, {"contexts": [], **(options or {})}, 0)
    solved = construct_state(skeleton, scale=scale, data_seed=0)
    pair = instantiate_pair(skeleton, solved)
    instance = PatternInstance("target:0", pattern, skeleton.form, "port:0",
                               tuple(pair.mutation_record["site"]["address"]))
    host = HostAst(skeleton.source)
    embedded = EmbeddedAst(host, skeleton.source, skeleton, (instance,), instance.instance_id)
    state = GraphState(solved.population.spec.to_dict(), solved.population,
                       embedded.embedded_digest, digest(solved.population.spec.to_dict()), solved)
    return pair, embedded, state


def test_short_circuit_keeps_target_valid_while_protected_work_is_masked():
    pair, embedded, state = dependency_fixture("S7", {"scalar_contexts": [], "scalar_value": 0})
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"]
    assert report.static["protected_work_masked"]
    assert report.static["logical_demand"] == "masked"
    assert report.static["logical_request_interval"] == [0, 0]
    assert report.static["input_cardinality_interval"] == [1, 1]
    assert report.static["exposure_status"] == "exposed"
    assert report.runtime["status"] == "not_run"
    assert report.runtime["physical_invocations"] == "unknown"
    assert report.static["analysis"]["symbols"]
    assert report.static["scope_binding_edges"]


@pytest.mark.parametrize("pattern", ["S5", "S9"])
def test_zero_demand_and_empty_input_are_valid_short_circuit_objectives(pattern):
    pair, embedded, state = dependency_fixture(pattern)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"]
    assert report.static["logical_demand"] == "masked"
    assert report.static["physical_invocations"] == "unknown"


@pytest.mark.parametrize("context,expected", [("count", "full"), ("exists", "bounded")])
def test_actual_row_consumers_distinguish_count_from_exists(context, expected):
    pair, embedded, state = dependency_fixture("P3", {"contexts": [context]})
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["logical_demand"] == expected
    assert report.static["row_consumption_edges"]
    assert any(step["operator"] == context.title() for step in report.static["ancestor_steps"])
    assert report.runtime["physical_invocations"] == "unknown"


def test_with_alias_definitions_and_consumers_are_retained():
    pair, embedded, state = dependency_fixture("P3", {"contexts": ["with", "count"]})
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert any(symbol["kind"] == "alias" or symbol["sources"] for symbol in report.static["analysis"]["symbols"])
    assert report.static["scope_binding_edges"]


def test_changed_graph_cannot_reuse_the_previous_semantic_certificate():
    pair, embedded, state = dependency_fixture("S7")
    with pytest.raises(StageFailure, match="contract_drift"):
        check_dependencies(pair, embedded, replace(state, graph_spec={**state.graph_spec, "data_seed": 33}))


def test_null_prefix_is_a_semantic_counterexample_not_unknown_or_empty_input():
    pair, embedded, state = dependency_fixture("S7", {"scalar_contexts": [], "scalar_value": 0})
    groups = list(state.graph_spec["node_groups"])
    groups[0] = {**groups[0], "properties": {**groups[0]["properties"], "payload": None}}
    spec = GraphSpec.from_dict({**state.graph_spec, "node_groups": groups})
    assert evaluate_query(pair.query_a, spec) != evaluate_query(pair.query_b, spec)
    population = replace(state.population, spec=spec)
    changed = replace(state, graph_spec=spec.to_dict(), population=population,
                      graph_digest=digest(spec.to_dict()))
    report = check_dependencies(pair, embedded, changed)
    assert report.static["result_relation"] in {"refuted", "unknown"}
    assert report.runtime["status"] == "not_run"


def test_counterfactual_matching_work_is_not_reported_as_a_physical_call_count():
    pair, embedded, state = dependency_fixture("P3")
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["potential_matching_reads"]["a"]
    assert all(read["filter_applied"] is False for read in report.static["potential_matching_reads"]["a"])
    assert report.static["physical_invocations"] == "unknown"


@pytest.mark.parametrize("pattern,options", [
    ("P10", {"form": "independent"}), ("P10", {"form": "shared_anchor"}),
    ("P10", {"form": "chain"}), ("P11", {"match_state": "both"}), ("P8", {}),
])
def test_matching_clause_targets_have_their_actual_input_domain_recorded(pattern, options):
    pair, embedded, state = dependency_fixture(pattern, options)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"]
    assert report.static["exposure_status"] == "exposed"
    assert report.static["input_cardinality_interval"][1] > 0


@pytest.mark.parametrize("prefix,premise,target_valid,demand", [
    (0, "satisfied", False, "masked"), (None, "refuted", True, "full"),
])
def test_inner_p3_dependency_on_an_existing_outer_s7_is_explicit(prefix, premise, target_valid, demand):
    pair, embedded, state = dependency_fixture("P3")
    def wrap(tree):
        return ir.Query(None, None, (ir.Alias(ir.Call("coalesce", (
            ir.Literal(prefix), ir.Count(body=tree))), "value"),))
    left, right = wrap(pair.query_a), wrap(pair.query_b)
    site_address = ("returns", 0, "expression", "args", 1, "body", *pair.mutation_record["site"]["address"])
    record = {**pair.mutation_record, "site": {**pair.mutation_record["site"], "address": list(site_address)}}
    pair = replace(pair, query_a=left, query_b=right, mutation_record=record,
                   bindings={**pair.bindings, "mutation_site": list(site_address)})
    embedded = replace(embedded, tree=left, host=HostAst(left))
    state = replace(state, embedded_digest=embedded.embedded_digest,
                    population=replace(state.population, expected_rows=evaluate_query(left, state.spec)))
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"] is target_valid
    assert report.static["logical_demand"] == demand
    outer = next(item for item in report.static["context_patterns"] if item["pattern_id"] == "S7")
    assert outer["premise_status"] == premise
    assert outer["semantic_input_count"] == 1
    assert outer["exposure_status"] == "exposed"
    assert outer["logical_demand"] == demand
    assert outer["physical_invocations"] == "unknown"
    assert outer in report.static["cross_pattern_effects"]


@pytest.mark.parametrize("pattern,options", [("P3", {}), ("P10", {"form": "shared_anchor"})])
def test_large_state_counts_are_scoped_to_the_compressed_proof_model(pattern, options):
    pair, embedded, state = dependency_fixture(pattern, options, scale=10000)
    assert state.spec.counts()["nodes"] > 5000
    report = check_dependencies(pair, embedded, state)
    static = report.static
    assert static["result_relation"] == "proved", static
    assert static["target_valid"]
    assert static["input_observation_scope"] == "certified_proof_graph"
    assert static["input_cardinality_interval"] == [0, None]
    assert static["logical_request_interval"] == [0, None]
    assert static["distinct_input_bindings"] is None
    assert static["input_multiplicities"] is None
    assert static["proof_model_input_cardinality_interval"][1] >= 1
    assert static["proof_model_input_multiplicities"]
    assert static["target_valid_scope"] == "M3_preserved_construction_witness"
    assert static["target_entry_evidence"]["witness_certificate"]
    assert static["target_entry_evidence"]["cardinality_certified"] is False
    for reads in static["potential_matching_reads"].values():
        for read in reads:
            assert read["candidate_count_scope"] == "certified_proof_graph"
            assert read["full_graph_candidate_interval"] == [0, None]
            assert "proof_model_total_candidates" in read
            assert "total_candidates" not in read
    for context in static["context_patterns"]:
        assert context["semantic_input_count"] is None
        assert context["semantic_input_cardinality_interval"] == [0, None]
        assert context["input_observation_scope"] == "certified_proof_graph"


def test_large_short_circuit_zero_demand_uses_a_growth_stable_prefix_certificate():
    pair, embedded, state = dependency_fixture("S7", {"scalar_contexts": [], "scalar_value": 0}, scale=10000)
    report = check_dependencies(pair, embedded, state)
    static = report.static
    assert static["input_observation_scope"] == "certified_proof_graph"
    assert static["input_cardinality_interval"] == [0, None]
    assert static["logical_request_interval"] == [0, 0]
    assert static["logical_demand"] == "masked"
    assert static["global_mask_evidence"]["kind"] == "M3_growth_stable_nonnull_prefix"
    assert static["proof_model_input_cardinality_interval"] == [1, 1]


def shared_node_fixture(outer_labels=("Common",), target_labels=("Rare", "Common")):
    child = ir.Query(ir.Node("target", target_labels), None, (ir.Literal(1),))
    tree = ir.Query(ir.Node("outer", outer_labels), None, (ir.Alias(ir.Count(body=child), "n"),))
    spec = GraphSpec("AST", 1, 0, version=2, node_groups=(
        {"count": 1, "labels": ["Rare", "Common", "Other"], "properties": {"uid": 1, "keep": True}},
        {"count": 1, "labels": ["Common"], "properties": {"uid": 2, "keep": False}},
    ), edge_groups=())
    return tree, ("returns", 0, "expression", "body", "pattern"), spec


@pytest.mark.parametrize("outer_labels", [("Common",), (), ("Other",)])
def test_node_scan_overlap_uses_actual_multilabel_domains(outer_labels):
    tree, address, spec = shared_node_fixture(outer_labels)
    shared = _shared_reads(tree, address, spec)
    outer = next(item for item in shared if item["address"] == ["pattern"])
    assert outer["overlap_status"] == "observed"
    assert outer["domain_observation_scope"] == "full_graph"
    assert outer["domain_intersection_interval"] == [1, 1]
    assert outer["shared_entity_uid_sample"] == [1]
    assert outer["physical_effect"] == "unknown"


def test_different_label_names_do_not_prove_node_domains_disjoint_without_graph_evidence():
    tree, address, _ = shared_node_fixture(("Other",))
    outer = next(item for item in _shared_reads(tree, address) if item["address"] == ["pattern"])
    assert outer["target_labels"] == ["Rare", "Common"]
    assert outer["outer_labels"] == ["Other"]
    assert outer["overlap_status"] == "unknown"
    assert outer["domain_intersection_interval"] == [0, None]


def test_property_reads_follow_entity_alias_identity_and_are_deduplicated():
    child = ir.Query(ir.Node("saved", ("Common",)), None, (ir.Alias(ir.Property("saved", "keep"), "v"),))
    tree = ir.Query(None, None, (
        ir.Alias(ir.Count(body=child), "n"), ir.Alias(ir.Property("saved", "keep"), "keep")),
        pipeline=(ir.Match(ir.Node("original", ("Common",))),
                  ir.With((ir.Alias(ir.Variable("original"), "saved"),))))
    _, _, spec = shared_node_fixture()
    address = ("returns", 0, "expression", "body")
    shared = _shared_reads(tree, address, spec)
    property_reads = [item for item in shared if item["target_read_kind"] == "property"
                      and item["outer_read_kind"] == "property"]
    assert len(property_reads) == 1
    assert property_reads[0]["shared_entity_binding"] is True
    assert property_reads[0]["property_keys"] == ["keep"]
    assert property_reads[0]["domain_intersection_interval"] == [2, 2]
    assert property_reads[0]["physical_effect"] == "unknown"


def reframe_pair_fixture(pair, embedded, state, left, right, address):
    record = {**pair.mutation_record, "site": {**pair.mutation_record["site"], "address": list(address)}}
    pair = replace(pair, query_a=left, query_b=right, mutation_record=record,
                   bindings={**pair.bindings, "mutation_site": list(address)})
    instances = tuple(replace(item, site_address=address) for item in embedded.instances)
    embedded = replace(embedded, tree=left, host=HostAst(left), instances=instances)
    proof = (state.spec if state.spec.counts()["nodes"] <= 5000 else
             GraphSpec.from_dict(state.population.evidence["validation_graph_spec"]))
    state = replace(state, embedded_digest=embedded.embedded_digest,
                    population=replace(state.population, expected_rows=evaluate_query(left, proof)))
    return pair, embedded, state


@pytest.mark.parametrize("pattern", ["P3", "P4", "S7"])
def test_count_of_rows_does_not_demand_an_unused_return_expression(pattern):
    options = {"scalar_contexts": []} if pattern in {"P4", "S7"} else {}
    pair, embedded, state = dependency_fixture(pattern, options)
    old_address = tuple(pair.mutation_record["site"]["address"])
    def wrap(tree):
        body = ir.Query(None, None, (ir.Alias(ir.Count(body=tree), "discarded"),))
        return ir.Query(None, None, (ir.Alias(ir.Count(body=body), "n"),))
    left, right = wrap(pair.query_a), wrap(pair.query_b)
    address = ("returns", 0, "expression", "body", "returns", 0, "expression", "body", *old_address)
    pair, embedded, state = reframe_pair_fixture(pair, embedded, state, left, right, address)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"] is False
    assert report.static["logical_demand"] == "masked"
    assert report.static["value_dependency"] == "unused"
    assert report.static["logical_request_interval"] == [0, 0]
    assert report.static["proof_model_syntactic_entry_count"] == 1
    assert any(step.get("reason") == "unobserved_projection" for step in report.static["ancestor_steps"])
    if pattern == "S7":
        assert report.static["protected_work_masked"] is False


@pytest.mark.parametrize("consumer,distinct,valid", [("count", False, False), ("count", True, True), ("exists", True, False)])
def test_distinct_return_values_are_needed_by_count_but_not_exists(consumer, distinct, valid):
    pair, embedded, state = dependency_fixture("P4", {"scalar_contexts": []})
    expression = ir.Count if consumer == "count" else ir.Exists
    def wrap(tree):
        return ir.Query(None, None, (ir.Alias(expression(body=replace(tree, distinct=distinct)), "n"),))
    address = ("returns", 0, "expression", "body", *pair.mutation_record["site"]["address"])
    pair, embedded, state = reframe_pair_fixture(pair, embedded, state, wrap(pair.query_a), wrap(pair.query_b), address)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"] is valid
    if not valid:
        assert report.static["logical_demand"] == "masked"


@pytest.mark.parametrize("used_in_where", [False, True])
def test_with_alias_liveness_reaches_filters_and_drops_unused_values(used_in_where):
    pair, embedded, state = dependency_fixture("P4", {"scalar_contexts": []})
    suffix = tuple(pair.mutation_record["site"]["address"])[3:]
    def wrap(tree):
        scalar = tree.returns[1].expression
        clause = ir.With((ir.Alias(scalar, "observed"),),
                         where=ir.Comparison(ir.Variable("observed"), "=", ir.Literal(False)) if used_in_where else None)
        body = ir.Query(None, None, (ir.Literal(1),),
                        pipeline=(ir.Match(tree.pattern, tree.where), clause))
        return ir.Query(None, None, (ir.Alias(ir.Count(body=body), "n"),))
    address = ("returns", 0, "expression", "body", "pipeline", 1, "projections", 0, "expression", *suffix)
    pair, embedded, state = reframe_pair_fixture(pair, embedded, state, wrap(pair.query_a), wrap(pair.query_b), address)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["target_valid"] is used_in_where
    assert (report.static["logical_demand"] == "masked") is not used_in_where


def test_large_dead_projection_has_zero_demand_by_syntax_not_by_sample_size():
    pair, embedded, state = dependency_fixture("P3", scale=10000)
    def wrap(tree):
        body = ir.Query(None, None, (ir.Alias(ir.Count(body=tree), "unused"),))
        return ir.Query(None, None, (ir.Alias(ir.Count(body=body), "n"),))
    left, right = wrap(pair.query_a), wrap(pair.query_b)
    address = ("returns", 0, "expression", "body", "returns", 0, "expression", "body", *pair.mutation_record["site"]["address"])
    # Only one scalar result is returned at every graph scale. The existing M3
    # path/growth certificate remains scoped to its original matching domain.
    pair, embedded, state = reframe_pair_fixture(pair, embedded, state, left, right, address)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved", report.static
    assert report.static["logical_request_interval"] == [0, 0]
    assert report.static["global_mask_evidence"]["kind"] == "syntactic_unobserved_projection"
    assert report.static["target_valid"] is False

