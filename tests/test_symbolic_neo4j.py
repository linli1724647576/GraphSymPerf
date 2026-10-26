"""Execute paper S6/S8 only on an explicitly supplied disposable Neo4j target."""

import os
from dataclasses import fields, is_dataclass

import pytest

from executor import CaseFailure, Neo4j, check_case, measurement_settings, run_case, typed_bag
from ir import Alias, Count, Query, UnionAll, from_data
from main import prepare_case, read_case, save_case
from query import render_query
from state import GraphSpec


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no dedicated Neo4j test target supplied")]


def configuration():
    return {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
            "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
            "dedicated": True, "allow_reset": True}


def settings():
    return measurement_settings({"discovery_pairs": 2, "discovery_warmups": 0,
                                 "confirmation_pairs": 4, "confirmation_warmups": 0,
                                 "control_pairs": 2, "bootstrap_samples": 100,
                                 "screen_ratio": 1_000_000, "timeout_seconds": 30})


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def options(pattern, *, contexts=(), threshold=0):
    result = {"contexts": list(contexts), "threshold": threshold}
    if pattern == "S6":
        result["row_limit"] = 3
    return result


@pytest.mark.parametrize("pattern", ["S6", "S8"])
def test_symbolic_short_circuit_pair_loads_twice_and_preserves_complete_results(pattern):
    config = configuration()
    protocol = settings()
    case = prepare_case(None, pattern, 13, 0, 3, composition=options(pattern), database=config)
    assert "compile_error" not in case
    assert "UNION ALL" in case["query_a"]
    assert "UNION ALL" in case["query_b"]
    assert len(case["expected_rows"]) == (3 if pattern == "S6" else 1)

    database = Neo4j(config, protocol)
    try:
        for _ in range(2):
            database.restore(case)
            check_case(database, case, protocol)
            for side in ("a", "b"):
                result = database.execute(case[f"query_{side}"], case["parameters"], 30)
                assert result["complete"] is True
                assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
        tree = from_data(case["query_ir"]["a"])
        witness = next(node for node in walk(tree) if isinstance(node, UnionAll)).branches[0]
        relation = witness.pattern.relationship.type
        assert relation.replace("_", "").isalnum()
        database.execute(f"MATCH ()-[r:{relation}]->() DELETE r", {}, 30)
        with pytest.raises(CaseFailure, match="cost_check"):
            check_case(database, case, protocol)
    finally:
        database.close()


@pytest.mark.parametrize("pattern,contexts,threshold,expected", [
    ("S6", ["count", "with"], 2, 3),
    ("S8", ["with", "exists", "count", "with"], 0, 1),
])
def test_symbolic_nested_consumers_keep_the_original_union_witness(pattern, contexts, threshold, expected):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 13, 0, 3,
                        composition=options(pattern, contexts=contexts, threshold=threshold), database=config)
    assert "compile_error" not in case
    assert len(case["expected_rows"]) == 1
    row = case["expected_rows"][0]
    assert row["u.uid"] == 1
    assert [value for name, value in row.items() if name != "u.uid"] == [expected]
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
    finally:
        database.close()


@pytest.mark.parametrize("pattern", ["S6", "S8"])
def test_symbolic_pair_runs_archives_and_replays_the_same_construction(tmp_path, pattern):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 13, 0, 3, composition=options(pattern), database=config)
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path)
    assert restored == case
    for field in ("symbolic_skeleton", "construction_record", "instantiation_record", "mutation_record"):
        assert restored[field] == case[field]
    replayed = run_case(restored, config, saved_protocol)
    assert replayed["status"] == "no_candidate", replayed
    assert replayed["discovery"]["samples"]


@pytest.mark.parametrize("pattern", ["S6", "S8"])
def test_large_high_region_contains_ten_thousand_valid_matches_with_restored_indexes(pattern):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 10_000, 0, 3, composition=options(pattern), database=config)
    spec = GraphSpec.from_dict(case["graph_spec"])
    assert spec.counts()["nodes"] >= 10_000
    trees = [from_data(case["query_ir"][side]) for side in ("a", "b")]
    unions = [next(node for node in walk(tree) if isinstance(node, UnionAll)) for tree in trees]
    witness, low, high = unions[0].branches[0], unions[0].branches[1], unions[1].branches[1]
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for branch, expected in ((witness, 3 if pattern == "S6" else 1), (low, 1), (high, 10_000)):
            count = Query(branch.pattern.left, None, (Alias(Count(body=branch), "n"),))
            result = database.execute(render_query(count), {}, 30)
            assert result["complete"] is True
            assert result["rows"] == [{"n": expected}]
        actual = database.execute("SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties "
                                  "WHERE type <> 'LOOKUP' RETURN name, state, labelsOrTypes, properties", {}, 30)
        expected_indexes = [{"name": index["name"], "state": "ONLINE", "labelsOrTypes": [index["label"]],
                             "properties": [index["property"]]} for index in spec.indexes]
        assert actual["complete"] is True
        assert sorted(actual["rows"], key=lambda row: row["name"]) == sorted(expected_indexes, key=lambda row: row["name"])
    finally:
        database.close()
