"""Goal-driven generation through the existing disposable-Neo4j pipeline."""

import os
from dataclasses import fields, is_dataclass, replace

import pytest

from executor import Neo4j, check_case, measurement_settings, run_case, typed_bag
from main import prepare_legacy_case as prepare_case, read_case, save_case
from symbolic import Symbol


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


def check_loaded_pair(case, config, protocol):
    assert "compile_error" not in case, case.get("compile_error")
    assert case["expected_rows"]
    database = Neo4j(config, protocol)
    try:
        database.restore(case)
        check_case(database, case, protocol)
        for side in ("a", "b"):
            result = database.execute(case[f"query_{side}"], case["parameters"], 30)
            assert result["complete"] is True
            assert typed_bag(result["rows"]) == typed_bag(case["expected_rows"])
    finally:
        database.close()


@pytest.mark.parametrize("length", [2, 3, 5])
@pytest.mark.parametrize("contexts,threshold", [((), 0), (("count", "with"), 9)])
def test_variable_length_chain_and_outer_quantity_goals_materialize(length, contexts, threshold):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P10", 7, 0, 3, database=config,
                        composition={"contexts": list(contexts), "threshold": threshold,
                                     "form": "chain", "chain_length": length})
    check_loaded_pair(case, config, protocol)


@pytest.mark.parametrize("guard_kind", ["property", "count", "exists"])
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("contexts,threshold", [((), 0), (("count", "with"), 2)])
def test_nested_conditionals_and_nonconstant_guards_materialize(guard_kind, depth, contexts, threshold):
    config, protocol = configuration(), settings()
    case = prepare_case(None, "S3", 7, 0, 3, database=config,
                        composition={"contexts": list(contexts), "threshold": threshold,
                                     "conditional_depth": depth, "guard_kind": guard_kind})
    assert "WHEN " in case["query_a"]
    assert case["query_a"].count("CYPHER 25") == 1
    check_loaded_pair(case, config, protocol)


def test_same_label_chain_keeps_distinct_binding_domains_after_database_loading(monkeypatch):
    import symbolic

    original = symbolic.build_symbolic_skeleton("P10", {"contexts": [], "form": "chain", "chain_length": 4}, 0)

    def rename(value):
        if isinstance(value, Symbol) and value.kind == "identifier" and value.name.startswith("label:") and value.name != "label:anchor":
            return replace(value, candidates=("Shared",))
        if is_dataclass(value):
            return replace(value, **{field.name: rename(getattr(value, field.name)) for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(rename(item) for item in value)
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        return value

    skeleton = rename(original)
    monkeypatch.setattr(symbolic, "build_symbolic_skeleton", lambda *args, **kwargs: skeleton)
    config, protocol = configuration(), settings()
    case = prepare_case(None, "P10", 7, 0, 3, database=config,
                        composition={"contexts": [], "form": "chain", "chain_length": 4})
    check_loaded_pair(case, config, protocol)


@pytest.mark.parametrize("pattern,extra", [
    ("P10", {"form": "chain", "chain_length": 5}),
    ("S3", {"conditional_depth": 2, "guard_kind": "count"}),
    ("S3", {"conditional_depth": 3, "guard_kind": "exists"}),
])
def test_semantic_goal_cases_run_save_and_replay_without_new_generation(tmp_path, pattern, extra):
    config, protocol = configuration(), settings()
    case = prepare_case(None, pattern, 7, 0, 3, database=config,
                        composition={"contexts": ["count", "with"], "threshold": 9, **extra})
    assert case["expected_rows"]
    report = run_case(case, config, protocol)
    assert report["status"] == "no_candidate", report
    assert report["discovery"]["samples"]
    save_case(tmp_path, case, protocol, report)
    restored, saved_protocol = read_case(tmp_path)
    assert restored == case
    repeated = run_case(restored, config, saved_protocol)
    assert repeated["status"] == "no_candidate", repeated
    assert repeated["discovery"]["samples"]
