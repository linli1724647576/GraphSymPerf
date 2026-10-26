"""HugeGraph reset must reject lost parent metadata and terminate on no progress."""

import pytest

from executor import ApacheHugeGraph, CaseFailure
from state import GraphSpec


def adapter():
    db = ApacheHugeGraph.__new__(ApacheHugeGraph)
    db.settings = {"timeout_seconds": 2.0, "max_rows": 10000}
    return db


def parent_schema(*, lost=False):
    spec = GraphSpec("P3", 3, 0)
    children = ApacheHugeGraph._parent_edge_labels(spec)["GSP_LINK"]
    return [{"name": "GSP_LINK", "type": "NORMAL" if lost else "PARENT", "parent": None,
             "links": spec.schema()["relationships"][0]["endpoints"]}] + [
                 {"name": child["label"], "type": "NORMAL" if lost else "SUB",
                  "parent": None if lost else "GSP_LINK",
                  "links": [{key: child[key] for key in ("source_label", "target_label")}]} for child in children]


def test_lost_parent_metadata_is_rejected_before_any_delete():
    db, calls = adapter(), []
    def script(query, parameters=None):
        calls.append(query)
        if len(calls) > 1:
            pytest.fail("corrupt schema must stop before counting or deleting data")
        return parent_schema(lost=True), 1.0
    db._script = script
    with pytest.raises(CaseFailure, match="hugegraph_parent_schema_persistence_loss") as failure:
        db._reset()
    assert failure.value.stop_target is True
    assert failure.value.status == "environment_error"
    assert "remove()" not in calls[0] and ".drop()" not in calls[0]


@pytest.mark.parametrize("labels", [[], [{"name": "GSP_LINK", "type": "NORMAL", "parent": None,
                                          "links": [{"source_label": "GspLow", "target_label": "GspHigh"}]}], parent_schema()])
def test_healthy_schema_and_empty_graph_reset_complete(labels):
    db, calls = adapter(), []
    def script(query, parameters=None):
        calls.append((query, parameters))
        if "getEdgeLabels()" in query:
            return labels, 1.0
        if ".count().next()" in query:
            return [0], 1.0
        return [{"ids": [], "stalled": False}], 1.0
    db._script = script
    db._reset()
    assert len(calls) <= 5


def test_repeated_native_edge_ids_stop_with_bounded_no_progress_failure():
    db, calls = adapter(), []
    def script(query, parameters=None):
        calls.append((query, parameters))
        if "getEdgeLabels()" in query:
            return [], 1.0
        if ".count().next()" in query:
            return [8], 1.0
        return [{"ids": [str(i) for i in range(8)], "stalled": bool(parameters["gsp_previous_ids"])}], 1.0
    db._script = script
    with pytest.raises(CaseFailure, match="hugegraph_reset_no_progress") as failure:
        db._reset()
    assert failure.value.stop_target is True
    assert len(calls) <= 4


def test_initial_population_bounds_batches_even_when_native_stall_flag_is_false():
    db, calls = adapter(), []
    def script(query, parameters=None):
        calls.append((query, parameters))
        if "getEdgeLabels()" in query:
            return [], 1.0
        if ".count().next()" in query:
            return [1], 1.0
        # A distinct ID every time still cannot extend a dedicated reset forever.
        return [{"ids": [str(len(calls))], "stalled": False}], 1.0
    db._script = script
    with pytest.raises(CaseFailure, match="hugegraph_reset_batch_budget_exceeded"):
        db._reset()
    assert len(calls) <= 4
