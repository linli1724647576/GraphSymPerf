from datetime import date, datetime

from graph_sym_perf.core import to_jsonable
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
    serialized_node = to_jsonable(node)
    serialized_relationship = to_jsonable(relationship)

    assert serialized_node["__graph_type__"] == "node"
    assert serialized_relationship["__graph_type__"] == "relationship"
    assert not compare_typed_results(((serialized_node,),), (({"name": "Ada"},),), ordered=False).equivalent
    assert not compare_typed_results(((serialized_node,),), ((to_jsonable(FakeNode(name="Grace")),),), ordered=False).equivalent


def test_temporal_and_bytes_values_do_not_collapse_into_plain_strings() -> None:
    assert not compare_typed_results(
        ((to_jsonable(date(2026, 7, 10)),),),
        (("2026-07-10",),),
        ordered=False,
    ).equivalent
    assert not compare_typed_results(
        ((to_jsonable(datetime(2026, 7, 10, 12, 30)),),),
        (("2026-07-10T12:30:00",),),
        ordered=False,
    ).equivalent
    assert not compare_typed_results(((to_jsonable(b"abc"),),), (("616263",),), ordered=False).equivalent
