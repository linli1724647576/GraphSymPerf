"""Offline coverage of the existing S1--S10 IR and reference replacements.

These tests check compiler contracts, not provider execution or performance.
The installed provider still has to pass the real result-semantics gate.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

import executor
from executor import CaseFailure, run_case, supported_patterns
from generator import instantiate
from gremlin import generate_gremlin
from ir import Property, QueryError
from languages import UnsupportedFeature, generate, render_saved, validate_language
from main import prepare_legacy_case as prepare_case
from mutator import mutate
from query import parse_query, projection_names
from test_executor import CASE, SETTINGS, FakeDatabase


VERIFIED = ("gremlin_null_projection",)
TARGETS = ("janusgraph", "hugegraph", "arcadedb")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("pattern", [f"S{i}" for i in range(1, 11)])
@pytest.mark.parametrize("complexity", [0, 1, 2])
def test_existing_scor_pairs_compile_without_changing_graph_or_reference_semantics(target, pattern, complexity):
    # Include all three S4 guards, LIMIT cardinalities, and nested/data-backed
    # guard variants. The generator/mutator retain ownership of these forms.
    for seed in (0, 1, 2, 17, 101, 107):
        case = instantiate(None, pattern, 3, seed, data_seed=11, complexity=complexity)
        before = deepcopy(case)
        original = case["query_a"]
        reference = mutate(original, pattern, bindings=case["bindings"])
        a = generate(original, target, "gremlin", verified_features=VERIFIED)
        b = generate(reference, target, "gremlin", verified_features=VERIFIED)
        assert case == before
        assert a.columns == b.columns == tuple((f"c{i}", name) for i, name in enumerate(projection_names(original)))
        assert "'GSP_SC_HIGH'" in a.text and "'GSP_SC_REF'" in b.text
        assert a.text.replace("'GSP_SC_HIGH'", "'GSP_SC_REF'") == b.text
        assert a.bindings == b.bindings
        assert validate_language(original, target, "gremlin", verified_features=VERIFIED) == a.required_features
        assert render_saved(original, target, "gremlin") == a
        if pattern == "S3":
            assert a.text.startswith("g.inject(1).choose(")
            assert "conditional_query" in a.required_features
        if pattern == "S6":
            assert a.text.endswith(f".limit({original.limit})")
        if pattern == "S10":
            assert a.text.endswith(".dedup()")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("condition,predicate", [("true", "__.identity()"), ("false", "__.limit(0)"), ("null", "__.limit(0)")])
def test_conditional_uses_true_only_branch_predicate_and_preserves_unselected_branch(target, condition, predicate):
    tree = parse_query(f"CYPHER 25 WHEN {condition} THEN {{ RETURN 7 AS value }} ELSE {{ RETURN 9 AS value }}")
    compiled = generate_gremlin(tree, target)
    assert compiled.text.startswith(f"g.inject(1).choose({predicate},__.project('c0')")
    assert compiled.text.count(".project('c0')") == 2
    assert compiled.columns == (("c0", "value"),)
    if target == "arcadedb":
        assert ".constant(7L)" in compiled.text and ".constant(9L)" in compiled.text
    else:
        assert list(compiled.bindings.values()) == [7, 9]


def test_conditional_branches_start_independent_query_scopes_and_keep_ordered_columns():
    tree = parse_query("CYPHER 25 WHEN COUNT { MATCH (u:Guard) } > 0 "
                       "THEN { MATCH (u:Left) RETURN u.uid AS first, 7 AS second } "
                       "ELSE { MATCH (u:Right) RETURN u.uid AS first, 9 AS second }")
    compiled = generate_gremlin(tree, verified_features=VERIFIED)
    for label in ("Guard", "Left", "Right"):
        assert f"__.V().hasLabel('{label}')" in compiled.text
    assert compiled.text.count(".project('c0','c1')") == 2
    assert compiled.columns == (("c0", "first"), ("c1", "second"))
    assert compiled.normalize_rows([{"c0": 1, "c1": 7}] * 2) == [{"first": 1, "second": 7}] * 2


def test_conditional_guard_cannot_capture_a_branch_variable():
    tree = parse_query("CYPHER 25 WHEN true THEN { MATCH (u:A) RETURN 1 AS value } ELSE { RETURN 2 AS value }")
    with pytest.raises(QueryError, match="Unbound"):
        generate_gremlin(replace(tree, condition=Property("u", "keep")), verified_features=VERIFIED)


def test_conditional_branches_must_have_identical_logical_columns():
    tree = parse_query("CYPHER 25 WHEN true THEN { RETURN 1 AS value } ELSE { RETURN 2 AS value }")
    different = parse_query("RETURN 2 AS other")
    with pytest.raises(QueryError, match="identical column names"):
        generate_gremlin(replace(tree, otherwise=different), verified_features=VERIFIED)


def test_unselected_branch_still_requires_nullable_transport_and_supported_semantics():
    nullable = parse_query("CYPHER 25 WHEN true THEN { RETURN 1 AS value } ELSE { RETURN null AS value }")
    with pytest.raises(UnsupportedFeature) as rejected:
        generate_gremlin(nullable)
    assert rejected.value.features == {"gremlin_null_projection"}
    unsupported = parse_query("CYPHER 25 WHEN true THEN { RETURN 1 AS value } "
                              "ELSE { MATCH (u:A:B) RETURN 2 AS value }")
    with pytest.raises(UnsupportedFeature) as rejected:
        generate_gremlin(unsupported, verified_features=VERIFIED)
    assert rejected.value.features == {"multi_label_conjunction"}


@pytest.mark.parametrize("pattern,seed", [("S5", 0), ("S6", 0), ("S6", 1), ("S6", 2)])
def test_existing_zero_and_positive_limits_are_preserved(pattern, seed):
    case = instantiate(None, pattern, 3, seed)
    compiled = generate_gremlin(case["query_a"], verified_features=VERIFIED)
    assert compiled.text.endswith(f".limit({case['query_a'].limit})")
    assert len(case["expected_rows"]) == case["query_a"].limit


@pytest.mark.parametrize("target", TARGETS)
def test_scor_run_coverage_is_enabled_for_all_three_gremlin_targets(target):
    expected = {f"P{i}" for i in range(1, 16)} - {"P3", "P7", "P13"}
    if target in {"janusgraph", "hugegraph"}:
        expected |= {"P3", "P13", "P16"}
    if target == "hugegraph":
        expected.remove("P12")
    expected |= {f"S{i}" for i in range(1, 11)}
    assert supported_patterns({"dbms": target, "language": "gremlin"}) == expected
    if target == "arcadedb":
        # The existing Cypher dialect still cannot run ConditionalQuery.
        assert "S3" not in supported_patterns({"dbms": target, "language": "cypher"})


def test_s3_cypher_metadata_never_sends_a_cypher_probe_to_gremlin(monkeypatch):
    target = {"dbms": "arcadedb", "language": "gremlin", "verified_features": VERIFIED}
    case = prepare_case(None, "S3", 3, 17, 11, database=target)
    assert case["required_cypher"] == 25 and "compile_error" not in case

    class Database(FakeDatabase):
        instances = []

        def check_state(self, case):
            return []

        def execute(self, query, parameters, timeout):
            self.calls.append(query)
            assert query in (case["query_a"], case["query_b"])
            assert query.startswith("g.inject(1).choose(")
            return {"rows": [{"c0": 7}], "complete": True, "elapsed_ms": 1.0}

    monkeypatch.setattr(executor, "make_database", Database)
    report = run_case(case, target, SETTINGS)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    assert all(db.calls and db.closed for db in Database.instances)


@pytest.mark.parametrize("target", [{}, {"dbms": "neo4j", "language": "cypher"}])
def test_cypher_25_probe_is_preserved_before_database_restore(target, monkeypatch):
    class Database(FakeDatabase):
        instances = []

        def execute(self, query, parameters, timeout):
            self.calls.append(query)
            assert query == "CYPHER 25 RETURN 1 AS supported"
            assert not hasattr(self, "case")
            raise CaseFailure("query_error", "unsupported-language-version")

    monkeypatch.setattr(executor, "make_database", Database)
    report = run_case({**CASE, "required_cypher": 25}, target, SETTINGS)
    assert report["status"] == "unsupported_feature"
    assert report["reason"] == "Cypher_25_required"
    assert len(Database.instances) == 1 and Database.instances[0].closed
