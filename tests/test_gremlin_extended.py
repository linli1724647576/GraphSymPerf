"""Compiler-side contracts; independent real-provider oracles live next door."""
from copy import deepcopy
from dataclasses import replace

import pytest

from generator import instantiate, load_seeds
from gremlin import generate_gremlin
from languages import UnsupportedFeature
from mutator import mutate
from query import parse_query

VERIFIED = ("gremlin_null_projection",)
TARGETS = ("janusgraph", "hugegraph")
SEED = load_seeds({"log_files": ["tests/fixtures/gdsmith/neo4j_seeds.log"]})[0]


def compiled(text, target="janusgraph"):
    return generate_gremlin(parse_query(text), target, verified_features=VERIFIED)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("pattern", ("P3", "P13", "P16"))
@pytest.mark.parametrize("complexity", (0, 1, 2))
def test_existing_pairs_compile_without_mutating_ir_or_graph_spec(target, pattern, complexity):
    for seed in (0, 1, 2, 3, 17, 54):
        case = instantiate(SEED, pattern, 4, seed, data_seed=11, complexity=complexity)
        before = deepcopy(case)
        changed = mutate(case["query_a"], pattern, bindings=case["bindings"])
        a = generate_gremlin(case["query_a"], target, verified_features=VERIFIED)
        b = generate_gremlin(changed, target, verified_features=VERIFIED)
        assert case == before
        assert a.columns == b.columns
        assert a.text != b.text or a.bindings != b.bindings
        assert "simplePath" not in a.text + b.text
        assert "->" not in a.text + b.text  # no Groovy lambda
        if pattern == "P3":
            assert ".repeat(" in a.text and "select(Pop.all," in a.text
            assert ".count().is(P.eq(1))" in a.text
            assert ".dedup(" not in a.text
        elif pattern == "P13":
            assert a.text.count(".project('b0')") == 4
        else:
            assert a.text.count(".unfold().as(") == 1 if complexity == 0 else a.text.count(".unfold().as(") in (1, 2)
            assert "sack(Operator.sum)" in a.text
            assert ".math(" not in a.text
            assert all(not isinstance(value, list) for value in a.bindings.values())


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("pattern,variant", (("P3", "pruning_path"), ("P13", "multi_low")))
def test_existing_state_variants_remain_available(target, pattern, variant):
    case = instantiate(SEED, pattern, 4, 17, state_variant=variant)
    a = generate_gremlin(case["query_a"], target, verified_features=VERIFIED)
    b = generate_gremlin(mutate(case["query_a"], pattern, bindings=case["bindings"]), target,
                         verified_features=VERIFIED)
    assert a.columns == b.columns
    assert "gremlin_null_projection" in a.required_features


def test_zero_hop_path_binds_end_and_keeps_single_start_node():
    text = compiled("MATCH p=(u:A)-[:R*0..0]-(v) RETURN u.uid,v.uid,length(p) AS n").text
    assert ".repeat(" not in text
    assert "project('_nodes','_length')" in text
    assert "select(Pop.all," in text
    assert "__.constant(0L)" in text


def test_optional_where_is_inside_coalesce_and_null_guard_covers_later_access():
    text = compiled("MATCH (u:A) OPTIONAL MATCH (u)-[r:R]->(v) WHERE v.keep=true "
                    "OPTIONAL MATCH (v)-[:S]->(w) RETURN v.uid,r.weight,w.uid,v.uid IS NULL AS n").text
    assert ".filter(__.select('_gsp_v2').values('keep').is(P.eq(" in text
    assert ".coalesce(__.select('_gsp_v2').is(P.neq(null)).outE('S')" in text
    assert "coalesce(__.is(P.neq(null)).values('weight'),__.constant(null))" in text
    assert ".hasNot('uid')" not in text
    assert ".dedup(" not in text


@pytest.mark.parametrize("expression,start,step,times", [
    ("range(1,6,2)", 1, 2, 2),
    ("range(6,1,-2)", 6, -2, 2),
    ("range(9007199254740993,9007199254740995)", 9007199254740993, 1, 2),
    ("range(9223372036854775805,9223372036854775807)", 9223372036854775805, 1, 2),
    ("range(-9223372036854775808,-9223372036854775806)", -9223372036854775808, 1, 2),
])
def test_range_retains_exact_long_arithmetic_and_server_list_construction(expression, start, step, times):
    text = compiled(f"RETURN {expression} AS values").text
    assert f"constant({start}L)" in text and f"constant({step}L)" in text
    assert f".until(__.is(P.eq({start + step * times}L)))" in text
    assert ".times(" not in text and ".fold()" in text
    assert ".math(" not in text


def test_nullable_all_tests_false_before_unknown_without_reconstructing_items():
    text = compiled("RETURN all(x IN [null,0,2] WHERE x>0) AS ok").text
    assert text.count(".union(") == 1
    assert text.count(".unfold().as(") == 2
    assert text.index("__.constant(false)") < text.rindex("__.constant(null)")


@pytest.mark.parametrize("expression", ("range(1,2147483649)", "range(1,COUNT { MATCH (u) })"))
def test_range_rejects_unbounded_repeat_or_dynamic_endpoints_explicitly(expression):
    with pytest.raises(UnsupportedFeature) as rejected:
        compiled(f"RETURN {expression} AS values")
    assert rejected.value.features & {"gremlin_range_repeat_bound", "gremlin_dynamic_range"}


def test_non_trail_paths_stay_explicitly_outside_the_fragment():
    tree = parse_query("MATCH p=(u)-[:R*1..3]-(v) RETURN length(p) AS n")
    for mode in ("walk", "acyclic"):
        changed = replace(tree, pattern=replace(tree.pattern, relationship=replace(tree.pattern.relationship, path_mode=mode)))
        with pytest.raises(UnsupportedFeature) as rejected:
            generate_gremlin(changed, verified_features=VERIFIED)
        assert rejected.value.features == {"gremlin_path_mode_" + mode}


def test_multi_label_p7_stays_rejected():
    with pytest.raises(UnsupportedFeature) as rejected:
        compiled("MATCH (u:A:B) RETURN u.uid")
    assert rejected.value.features == {"multi_label_conjunction"}


def test_distinct_numeric_lists_remain_rejected_without_equivalence_proof():
    with pytest.raises(UnsupportedFeature) as rejected:
        compiled("MATCH (u) RETURN DISTINCT CASE WHEN u.keep=true THEN [1] ELSE [1.0] END AS value")
    assert rejected.value.features == {"gremlin_numeric_distinct"}


def test_range_above_short_loop_counter_uses_value_termination():
    text = compiled("RETURN size(range(1,100000)) AS n").text
    assert ".until(__.is(P.eq(100000L)))" in text
    assert ".times(" not in text


def test_path_rejects_bound_that_overflows_oldest_traverser_counter():
    with pytest.raises(UnsupportedFeature) as rejected:
        compiled("MATCH p=(u)-[:R*1..32768]-(v) RETURN length(p) AS n")
    assert rejected.value.features == {"gremlin_path_hop_bound"}


def test_hugegraph_nullable_entities_use_internal_sentinel_but_projection_returns_null():
    text = compiled("MATCH (u:A) OPTIONAL MATCH (u)-[r:R]->(v) "
                    "OPTIONAL MATCH (v)-[:S]->(w) "
                    "RETURN u.uid,v.uid,r.weight,w.uid,v.uid IS NULL AS missing", "hugegraph").text
    assert ".project('b0','b1').by(__.constant(false)).by(__.constant(false))" in text
    assert "select('_gsp_v2').is(P.neq(false)).outE('S')" in text
    assert "coalesce(__.is(P.neq(false)).values('uid'),__.constant(null))" in text
    assert "coalesce(__.is(P.neq(false)).values('weight'),__.constant(null))" in text
    assert "__.constant(null).as(" not in text


def test_hugegraph_nullable_path_sentinel_is_guarded_for_length_and_nodes():
    text = compiled("MATCH (u:A) OPTIONAL MATCH p=(u)-[:R*1..2]-(v) "
                    "RETURN length(p) AS n,all(x IN nodes(p) WHERE x.keep=true) AS ok", "hugegraph").text
    assert ".is(P.neq(false)).select('_length')" in text
    assert ".is(P.neq(false)).select('_nodes')" in text
    assert "__.constant(null).as(" not in text


def test_hugegraph_nullable_all_binder_boxes_null_and_unboxes_scalar_reads():
    text = compiled("RETURN all(x IN [true,null] WHERE x) AS ok", "hugegraph").text
    assert ".unfold().project('_item').by(__.identity()).as(" in text
    assert "select('_gsp_v0').select('_item').is(P.eq(true))" in text
    ordinary = compiled("RETURN all(x IN ([0]+range(1,3)) WHERE x>0) AS ok", "hugegraph").text
    assert "project('_item')" not in ordinary


def test_hugegraph_null_list_concat_has_no_nullable_temporary_label():
    text = compiled("RETURN null + [1] AS value", "hugegraph").text
    assert "project('_left','_right')" in text
    assert ".as(" not in text
