from __future__ import annotations

from graph_sym_perf.adapters.arcadedb import ArcadeDBAdapter, _arcade_plan_payload, _arcade_rows
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.plan import normalize_plan


def test_arcadedb_rows_use_result_payload() -> None:
    rows = _arcade_rows({"result": [{"a": 1}, ["x", 2], "value"]})

    assert rows == ({"a": 1}, {"col1": "x", "col2": 2}, {"value": "value"})


def test_arcadedb_profile_payload_is_normalized() -> None:
    payload = {"result": [{"operator": "Node By Label Scan", "rows": 5}, {"operator": "Filter", "rows": 2}]}

    plan = normalize_plan(TargetSystem.ARCADEDB, _arcade_plan_payload(payload))

    assert [node.operator_family for node in plan.nodes] == ["Scan", "Filter"]
    assert plan.metrics["rows"] == 7.0


def test_arcadedb_replay_filter_rejects_only_known_unsupported_fragments() -> None:
    adapter = object.__new__(ArcadeDBAdapter)

    assert adapter.gdsmith_query_is_replayable("MATCH (n:L0)-[r:T0]->(m:L1) WHERE r.id > 0 RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WITH n MATCH (m) RETURN m")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) OPTIONAL MATCH (n)-->(m) RETURN m")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) UNWIND [1] AS x RETURN x")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) CALL { RETURN 1 AS x } RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE EXISTS { MATCH (n)-->() } RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) RETURN n UNION MATCH (m) RETURN m")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n)<-[]-(m) RETURN n")
