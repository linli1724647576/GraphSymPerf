"""Offline IR/compiler contracts; provider semantics are checked separately."""

from dataclasses import replace
from pathlib import Path

import pytest

from generator import instantiate, load_seeds
from gremlin import generate_gremlin
from ir import Alias, Literal, QueryError
from languages import UnsupportedFeature
from mutator import mutate
from query import parse_query


VERIFIED = ("gremlin_null_projection",)
FIXTURE = Path(__file__).parent / "fixtures/gdsmith/neo4j_seeds.log"


def compile_query(text):
    return generate_gremlin(parse_query(text), verified_features=VERIFIED)


def test_property_projection_requires_verified_null_transport_and_keeps_duplicate_rows():
    query = parse_query("MATCH (u:A) RETURN u.absent, u.uid AS id")
    with pytest.raises(UnsupportedFeature) as rejected:
        generate_gremlin(query)
    assert rejected.value.features == {"gremlin_null_projection"}
    compiled = generate_gremlin(query, verified_features=VERIFIED)
    assert compiled.normalize_rows([{"c0": None, "c1": 7}] * 2) == [{"u.absent": None, "id": 7}] * 2
    with pytest.raises(QueryError):
        compiled.normalize_rows([{"c1": 7}])
    assert ".coalesce(__.values('absent'),__.constant(null))" in compiled.text
    assert ".dedup()" not in compiled.text


def test_literals_are_bindings_and_cannot_turn_into_script_operations():
    malicious = "${System.exit(1)}'; g.V().drop(); //\n"
    query = parse_query("RETURN 1 AS result")
    query = replace(query, returns=(Alias(Literal(malicious), "result"),))
    compiled = generate_gremlin(query)
    assert malicious not in compiled.text
    assert malicious in compiled.bindings.values()
    assert "drop" not in compiled.text and "$" not in compiled.text


@pytest.mark.parametrize("value", [None, False, True, -(2**63), 2**63 - 1, 1.5, "a'$b\\c"])
def test_scalar_bindings_preserve_python_type(value):
    query = replace(parse_query("RETURN 1 AS result"), returns=(Alias(Literal(value), "result"),))
    compiled = generate_gremlin(query, verified_features=VERIFIED)
    assert list(compiled.bindings.values()) == [value]
    assert type(next(iter(compiled.bindings.values()))) is type(value)


@pytest.mark.parametrize("text,feature", [
    ("MATCH (u:A:B) RETURN u.uid", "multi_label_conjunction"),
    ("RETURN range(1, COUNT { MATCH (u) }) AS values", "gremlin_dynamic_range"),
])
def test_semantic_gaps_are_rejected_instead_of_approximate_translation(text, feature):
    with pytest.raises(UnsupportedFeature) as rejected:
        compile_query(text)
    assert feature in rejected.value.features


def test_unbound_variable_is_rejected_before_target_generation():
    query = parse_query("MATCH (u) RETURN u.uid")
    with pytest.raises(QueryError, match="Unbound"):
        generate_gremlin(replace(query, returns=(replace(query.returns[0], variable="missing"),)), verified_features=VERIFIED)


def test_graph_entity_projection_is_rejected_by_common_ir():
    with pytest.raises(QueryError, match="graph properties"):
        compile_query("MATCH (u) RETURN u")


def test_nullable_negation_uses_false_test_not_complement_of_true_test():
    compiled = compile_query("MATCH (u) WHERE NOT (u.keep = true) RETURN u.uid")
    assert "values('keep').is(P.neq(" in compiled.text
    # With an absent property values() produces no value: UNKNOWN remains
    # excluded, whereas not(values().is(eq(true))) would accept it.
    assert "__.not(" not in compiled.text


@pytest.mark.parametrize("operator", ["=", "<>"])
def test_comparison_with_null_never_passes_where(operator):
    compiled = compile_query(f"MATCH (u) WHERE u.keep {operator} null RETURN u.uid")
    assert ".filter(__.limit(0))" in compiled.text


def test_self_loop_correction_keeps_parallel_edges_without_global_dedup():
    compiled = compile_query("MATCH (u)-[r:R]-(v) RETURN u.uid, v.uid, r.weight")
    assert ".outE('R')" in compiled.text and ".inE('R')" in compiled.text
    assert ".outV().where(P.neq(" in compiled.text
    assert "bothE" not in compiled.text and "dedup" not in compiled.text


def test_rebound_endpoint_and_relationship_are_filtered_not_overwritten():
    compiled = compile_query("MATCH (u)-[r:R]->(v) MATCH (v)-[r:R]->(u) RETURN u.uid, v.uid")
    assert compiled.text.count(".where(P.eq(") == 2
    assert compiled.text.count(".as(") == 3


def test_single_edge_self_binding_checks_identity():
    compiled = compile_query("MATCH (u)-[:R]->(u) RETURN u.uid")
    assert ".inV().where(P.eq(" in compiled.text


def test_only_requested_distinct_removes_duplicates_before_limit():
    compiled = compile_query("MATCH (u) RETURN DISTINCT u.uid LIMIT 2")
    assert compiled.text.endswith(".dedup().limit(2)")


def test_distinct_mixed_numeric_projection_requires_verified_equality():
    query = "MATCH (u) RETURN DISTINCT CASE WHEN u.keep = true THEN 1 ELSE 1.0 END AS value"
    with pytest.raises(UnsupportedFeature) as rejected:
        compile_query(query)
    assert rejected.value.features == {"gremlin_numeric_distinct"}


def test_union_all_retains_branch_order_and_bag():
    compiled = compile_query("RETURN 1 AS x UNION ALL RETURN 2 AS x UNION ALL RETURN 1 AS x")
    assert list(compiled.bindings.values()) == [1, 2, 1]
    assert ".union(" in compiled.text and "dedup" not in compiled.text
    assert compiled.columns == (("c0", "x"),)


def test_return_only_query_keeps_one_input_and_boolean_false_is_not_empty():
    compiled = compile_query("RETURN false AS flag, null AS missing")
    assert compiled.text.startswith("g.inject(1).project(")
    assert list(compiled.bindings.values()) == [False, None]
    assert ".limit(0)" not in compiled.text


@pytest.mark.parametrize("text,step", [
    ("MATCH (u) RETURN COUNT { MATCH (u)-[:R]->(v) } AS n", ".count()"),
    ("MATCH (u) RETURN EXISTS { MATCH (u)-[:R]->(v) } AS yes", "__.constant(false)"),
    ("MATCH (u) RETURN CASE WHEN u.keep = true THEN 7 ELSE 3 END AS value", "__.choose("),
    ("MATCH (u) WHERE coalesce(u.keep, false) = true RETURN u.uid", "__.coalesce("),
    ("MATCH (u) WHERE u.uid = u.uid RETURN u.uid", ".flatMap("),
])
def test_finite_correlated_and_scalar_fragment_compiles(text, step):
    assert step in compile_query(text).text


@pytest.mark.parametrize("pattern", ["P1", "P2", "P4", "P5", "P6", "P8", "P9", "P10", "P11", "P12", "P14", "P15"])
def test_generated_pattern_pair_has_stable_logical_columns_and_explicit_order(pattern):
    seed = load_seeds({"log_files": [str(FIXTURE)]})[1 if pattern == "P5" else 0]
    case = instantiate(seed, pattern, 4, 17)
    original = generate_gremlin(case["query_a"], verified_features=VERIFIED)
    changed = generate_gremlin(mutate(case["query_a"], pattern, bindings=case["bindings"]), verified_features=VERIFIED)
    assert original.columns == changed.columns
    assert original.text != changed.text or original.bindings != changed.bindings


@pytest.mark.parametrize("pattern", ["P7"])
def test_out_of_fragment_patterns_are_not_counted_as_valid_coverage(pattern):
    seed = load_seeds({"log_files": [str(FIXTURE)]})[0]
    case = instantiate(seed, pattern, 4, 17)
    with pytest.raises(UnsupportedFeature):
        generate_gremlin(case["query_a"], verified_features=VERIFIED)
