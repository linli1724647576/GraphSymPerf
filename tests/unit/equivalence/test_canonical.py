from datetime import date, datetime

from graph_sym_perf.core import to_typed_value
from graph_sym_perf.equivalence.checker import compare_typed_results


class FakeNode(dict):
    labels = frozenset({"Person"})
    element_id = "node-1"


class FakeRelationship(dict):
    type = "KNOWS"
    element_id = "rel-1"
    start_node_element_id = "node-1"
    end_node_element_id = "node-2"


def test_unordered_results_use_a_typed_multiset_with_duplicate_counts() -> None:
    equal = compare_typed_results(((1,), (1,), (None,)), ((None,), (1,), (1,)), ordered=False)
    unequal_type = compare_typed_results(((1,),), ((1.0,),), ordered=False)
    unequal_count = compare_typed_results(((1,), (1,)), ((1,),), ordered=False)

    assert equal.equivalent
    assert not unequal_type.equivalent
    assert not unequal_count.equivalent


def test_ordered_results_preserve_sequence_and_nested_values() -> None:
    equal = compare_typed_results((({"x": [None, ""]},),), (({"x": [None, ""]},),), ordered=True)
    reordered = compare_typed_results(((1,), (2,)), ((2,), (1,)), ordered=True)

    assert equal.equivalent
    assert not reordered.equivalent


def test_missing_null_empty_and_numeric_types_remain_distinct() -> None:
    assert not compare_typed_results(({"x": None},), ({},), ordered=False).equivalent
    assert not compare_typed_results(({"x": []},), ({"x": ""},), ordered=False).equivalent
    assert not compare_typed_results(({"x": 1},), ({"x": 1.0},), ordered=False).equivalent


def test_graph_entities_keep_their_type_and_content_after_adapter_serialization() -> None:
    node = FakeNode(name="Ada")
    relationship = FakeRelationship(since=2026)
    serialized_node = to_typed_value(node)
    serialized_relationship = to_typed_value(relationship)

    assert not compare_typed_results(((serialized_node,),), (({"name": "Ada"},),), ordered=False).equivalent
    assert not compare_typed_results(((serialized_node,),), ((to_typed_value(FakeNode(name="Grace")),),), ordered=False).equivalent


def test_real_node_never_collides_with_a_same_field_user_map() -> None:
    node = to_typed_value(FakeNode(name="Ada"))
    lookalike = {
        "kind": "node",
        "element_id": "node-1",
        "labels": ["Person"],
        "properties": {"name": "Ada"},
    }

    assert not compare_typed_results(((node,),), ((lookalike,),), ordered=False).equivalent


def test_temporal_and_bytes_values_do_not_collapse_into_plain_strings() -> None:
    assert not compare_typed_results(
        ((to_typed_value(date(2026, 7, 10)),),),
        (("2026-07-10",),),
        ordered=False,
    ).equivalent
    assert not compare_typed_results(
        ((to_typed_value(datetime(2026, 7, 10, 12, 30)),),),
        (("2026-07-10T12:30:00",),),
        ordered=False,
    ).equivalent
    assert not compare_typed_results(((to_typed_value(b"abc"),),), (("616263",),), ordered=False).equivalent


def test_temporal_and_bytes_never_collide_with_tag_shaped_user_maps() -> None:
    assert not compare_typed_results(
        ((to_typed_value(date(2026, 7, 10)),),),
        (({"__typed_value__": "date", "value": "2026-07-10"},),),
        ordered=False,
    ).equivalent
    assert not compare_typed_results(
        ((to_typed_value(b"abc"),),),
        (({"__typed_value__": "bytes", "value": "616263"},),),
        ordered=False,
    ).equivalent
