"""Native diagnostics retain the ordinary HTTP result cap and query bindings."""

from copy import deepcopy

import pytest

from executor import ApacheHugeGraph, JanusGraph
from main import prepare_legacy_case as prepare_case


@pytest.mark.parametrize("target,kind", [("janusgraph", JanusGraph), ("hugegraph", ApacheHugeGraph)])
@pytest.mark.parametrize("analyze", [False, True])
@pytest.mark.parametrize("pattern,seed,complexity", [("S5", 0, 0), ("S8", 0, 0), ("S3", 0, 2)])
def test_native_plan_and_execute_submit_identical_bounded_query_prefix(target, kind, analyze, pattern, seed, complexity):
    # S5 LIMIT 0 followed by the transport cap is significant to
    # EarlyLimitStrategy. The cap must precede both native plan operations.
    case = prepare_case(None, pattern, 3, seed, 11, complexity, database={
        "dbms": target, "language": "gremlin", "verified_features": ["gremlin_null_projection"]})
    db = kind.__new__(kind)
    db.settings = {"timeout_seconds": 30.0, "max_rows": 10000}
    query, bindings = case["query_a"], case["query_bindings"]["a"]
    before = deepcopy((query, bindings, case["query_ir"]))
    columns = case["output_columns"]["a"]
    expected = [{physical: row[logical] for physical, logical in columns} for row in case["expected_rows"]]
    native = {"@type": "g:TraversalMetrics" if analyze else "g:TraversalExplanation", "@value": {"native": True}}
    calls = []
    def request(script, parameters, timeout):
        calls.append((script, deepcopy(parameters), timeout))
        rows = expected if len(calls) == 1 else [native]
        return {"status": {"code": 200}, "result": {"data": rows}}, 1.0
    db._request = request
    executed = db.execute(query, bindings, 30.0)
    plan = db.explain(query, bindings, analyze=analyze)
    bounded = f"({query}).limit(10001)"
    suffix = ".profile().next()" if analyze else ".explain()"
    assert calls == [(bounded + ".toList()", bindings, 30.0), (bounded + suffix, bindings, 30.0)]
    assert executed["rows"] == expected and executed["complete"] is True
    assert plan["plan"] == [native] and plan["analyze"] is analyze
    assert (query, bindings, case["query_ir"]) == before
