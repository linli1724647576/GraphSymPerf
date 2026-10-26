"""Public generation of paper P4/P10, including complete multiset obligations."""

from copy import deepcopy
from dataclasses import fields, is_dataclass
import json
from math import prod

import pytest

import main
from executor import typed_bag
from generator import PatternNotApplicable
from ir import (Alias, Boolean, Comparison, Edge, Exists, Literal, Match, Node,
                Property, Query, Relationship, clauses, from_data, to_data, validate_query)
from population import evaluate_query, synthesize_state
from query import parse_query
from state import GraphSpec


def make_case(pattern, contexts=(), *, threshold=0, scale=7, seed=0):
    return main.prepare_legacy_case(None, pattern, scale, seed, 9,
                             composition={"contexts": list(contexts), "threshold": threshold})


def walk(value):
    yield value
    if isinstance(value, tuple):
        for item in value:
            yield from walk(item)
    elif is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))


def assert_complete_bags(case):
    assert "compile_error" not in case
    spec = GraphSpec.from_dict(case["graph_spec"])
    expected = typed_bag(case["expected_rows"])
    for side in ("a", "b"):
        query = from_data(case["query_ir"][side])
        validate_query(query)
        assert to_data(parse_query(case[f"query_{side}"])) == to_data(query)
        assert typed_bag(evaluate_query(query, spec)) == expected
    assert case["query_a"] != case["query_b"]
    assert case["graph_spec"]["version"] == 2
    assert case["graph_spec"]["pattern"] == "AST"
    assert case["dependency_analysis"]["scopes"]
    assert case["mutation_record"]["propagation"]
    assert case["population_evidence"]["validation_graph_spec"]


@pytest.mark.parametrize("pattern,legacy", [("P4", "P4"), ("P10", "P11")])
def test_composition_selects_paper_catalog_without_relabeling_the_old_graph_recipe(pattern, legacy):
    case = make_case(pattern)
    assert case["pattern"] == case["paper_pattern"] == pattern
    assert case["legacy_pattern"] == legacy
    assert case["pattern_catalog"] == "paper-v1"
    if pattern == "P10":
        assert case["pattern_form"] == "shared_anchor"
    assert_complete_bags(case)


@pytest.mark.parametrize("pattern,contexts,threshold", [
    ("P4", (), 0), ("P4", ("count",), 0), ("P4", ("with",), 0),
    ("P4", ("exists", "count", "with"), 0),
    ("P10", (), 0), ("P10", ("count",), 2), ("P10", ("with",), 2),
    ("P10", ("exists", "count", "with"), 0),
])
def test_full_ast_bags_agree_for_each_supported_context(pattern, contexts, threshold):
    assert_complete_bags(make_case(pattern, contexts, threshold=threshold))


def test_paper_p10_is_shared_anchor_and_counts_the_cartesian_branch_product():
    case = make_case("P10", ("count", "with"), threshold=2)
    query = from_data(case["query_ir"]["a"])
    kernels = []
    for value in walk(query):
        if not isinstance(value, Query):
            continue
        parts = clauses(value)
        if (len(parts) == 3 and all(isinstance(part, Match) for part in parts)
                and isinstance(parts[0].pattern, Node)
                and all(isinstance(part.pattern, Edge) for part in parts[1:])):
            kernels.append(parts)
    assert len(kernels) == 1
    anchor, left, right = (part.pattern for part in kernels[0])
    assert left.left.variable == right.left.variable == anchor.variable
    assert left.right.variable != right.right.variable
    components = case["population_constraints"]["components"]
    assert len(components) == 2
    matched_rows = prod(component["valid_matches"] for component in components)
    assert matched_rows > 2
    assert next(value for key, value in case["expected_rows"][0].items() if key != "u.uid") == matched_rows
    derivation = case["construction_record"]["requirements"]["derivation"]
    assert any(bound["minimum"] > 2 for bound in derivation["cardinality_bounds"])
    assert case["population_evidence"]["required_matching_checks"]
    assert_complete_bags(case)


def test_p10_threshold_changes_joint_population_rather_than_only_recording_a_number():
    low = make_case("P10", ("count", "with"), threshold=0)
    high = make_case("P10", ("count", "with"), threshold=2)
    amounts = [prod(item["valid_matches"] for item in case["population_constraints"]["components"])
               for case in (low, high)]
    assert amounts[0] >= 2  # the shared work goal also requires a repeated input
    assert amounts[1] > 2
    assert low["graph_spec"] != high["graph_spec"]
    assert low["expected_rows"] != high["expected_rows"]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_p4_population_makes_the_cheap_operand_decisive_and_the_other_operand_expensive(seed):
    case = make_case("P4", seed=seed)
    tree = from_data(case["query_ir"]["a"])
    condition = next(value for value in walk(tree) if isinstance(value, Boolean)
                     and any(isinstance(term, Exists) for term in value.terms))
    expensive = next(term for term in condition.terms if isinstance(term, Exists))
    cheap = next(term for term in condition.terms if term is not expensive)
    spec = GraphSpec.from_dict(case["graph_spec"])
    probe = Query(expensive.pattern.left, None,
                  (Alias(cheap, "cheap"), Alias(expensive, "expensive")))
    assert evaluate_query(probe, spec) == [{"cheap": False, "expensive": False}]
    component, = case["population_constraints"]["components"]
    assert component["valid_matches"] == 0
    assert component["candidate_matches"] - component["valid_matches"] == case["scale"]


def test_population_uses_renamed_integer_p4_predicates_without_a_pattern_identifier():
    anchor = Node("origin", ("Origin",), (("token", Literal(17)),))
    edge = Edge(anchor, Relationship(type="LINK_TO_CANDIDATE"), Node("candidate", ("Candidate",)))
    cheap = Comparison(Property("origin", "mode"), "=", Literal(7))
    expensive = Exists(edge, Comparison(Property("candidate", "code"), "=", Literal(11)))
    query = Query(anchor, None, (Property("origin", "uid"),
                                Alias(Boolean("AND", (cheap, expensive)), "answer")))
    result = synthesize_state(query, scale=9, data_seed=7)
    assert evaluate_query(query, result.spec) == result.expected_rows
    assert evaluate_query(Query(anchor, None, (Alias(cheap, "cheap"), Alias(expensive, "expensive"))),
                          result.spec) == [{"cheap": False, "expensive": False}]
    assert all(type(value) is int for node in result.spec.iter_nodes()
               for key, value in node["properties"].items() if key in {"mode", "code"})
    assert result.spec.to_dict()["pattern"] == "AST"


def test_renamed_integer_shared_branches_keep_joint_cardinality_through_three_contexts():
    from composition import compose_query
    from dependencies import find_mutation_sites
    from mutator import mutate

    anchor = Node("origin", ("Origin",), (("token", Literal(17)),))
    left = Edge(anchor, Relationship(type="FIRST_LINK"), Node("first", ("First",)))
    right = Edge(anchor, Relationship(type="SECOND_LINK"), Node("second", ("Second",)))
    kernel = Query(anchor, None,
                   (Property("origin", "uid"), Property("first", "uid"), Property("second", "uid")),
                   matches=(Match(left, Comparison(Property("first", "code"), "=", Literal(7))),
                            Match(right, Comparison(Property("second", "code"), "=", Literal(11)))))
    query = compose_query(kernel, anchor, ("count", "exists", "with"), threshold=2)
    result = synthesize_state(query, scale=9, data_seed=7)
    site, = find_mutation_sites(query, "P10", "shared_anchor")
    changed = mutate(query, "P11", bindings={"paper_pattern": "P10", "form": "shared_anchor",
                                             "legacy_pattern": "P11", "mutation_site": list(site.address)})
    assert prod(item["valid_matches"] for item in result.constraints["components"]) > 2
    for candidate in (query, changed):
        assert typed_bag(evaluate_query(candidate, result.spec)) == typed_bag(result.expected_rows)


@pytest.mark.parametrize("pattern,contexts", [
    ("P4", ("count",)), ("P4", ("with",)),
    ("P4", ("exists", "count", "with")),
    ("P10", ("exists", "count", "with")),
])
def test_scalar_or_existence_wrapper_does_not_invent_three_rows(pattern, contexts):
    with pytest.raises(PatternNotApplicable):
        make_case(pattern, contexts, threshold=2)


@pytest.mark.parametrize("pattern", ["P4", "P10"])
def test_population_expansion_preserves_the_complete_bag(pattern):
    options = ("count", "with")
    small = make_case(pattern, options, scale=1)
    larger = make_case(pattern, options, scale=31)
    assert typed_bag(small["expected_rows"]) == typed_bag(larger["expected_rows"])
    assert GraphSpec.from_dict(larger["graph_spec"]).counts()["nodes"] > GraphSpec.from_dict(small["graph_spec"]).counts()["nodes"]
    assert_complete_bags(larger)


@pytest.mark.parametrize("pattern", ["P4", "P10"])
def test_catalog_dependency_and_population_evidence_survive_archiving(tmp_path, pattern):
    case = make_case(pattern, ("count", "with"))
    protocol = main.measurement_settings({})
    main.save_case(tmp_path, case, protocol, {"status": "no_candidate"})
    restored, _ = main.read_case(tmp_path)
    assert restored == case
    saved = json.loads((tmp_path / "result.json").read_text())
    saved["case"]["pattern_catalog"] = "legacy"
    (tmp_path / "result.json").write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="metadata|changed|hash"):
        main.read_case(tmp_path)


def test_composition_config_accepts_only_the_implemented_paper_patterns(tmp_path):
    config = {"database": {"dbms": "neo4j", "uri": "bolt://unused:7687", "database": "neo4j",
                            "dedicated": True, "allow_reset": True},
              "patterns": ["P3", "P4", "P10"], "generation": {"method": "symbolic-v1", "composition": {"contexts": []}}}
    filename = tmp_path / "config.yaml"
    filename.write_text(json.dumps(config))
    assert main.read_config(filename)["patterns"] == ["P3", "P4", "P10"]
    invalid = deepcopy(config)
    invalid["patterns"] = ["P14"]
    filename.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        main.read_config(filename)


def test_no_composition_keeps_legacy_p10_independent_match_semantics():
    case = main.prepare_legacy_case(None, "P10", 3, 0, 9)
    query = from_data(case["query_ir"]["a"])
    parts = clauses(query)
    assert len(parts) == 2
    assert case["graph_spec"]["version"] == 1
    assert case["graph_spec"]["pattern"] == "P10"
    assert case.get("pattern_catalog") != "paper-v1"

