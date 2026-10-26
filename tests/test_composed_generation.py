"""The real prepare/compile/archive path consumes recursively synthesized cases."""

import json
from itertools import product

import pytest

import main
from ir import from_data
from state import GraphSpec


@pytest.mark.parametrize("contexts", [
    ["exists"], ["count"], ["with"], ["count", "with"],
    ["exists", "count", "with"], ["with", "exists", "count", "with"],
])
def test_composition_uses_existing_compiler_and_verifies_both_queries(contexts):
    from population import evaluate_query

    case = main.prepare_legacy_case(None, "P3", 4, 17, 9,
                             composition={"contexts": contexts})
    assert "compile_error" not in case
    assert case["graph_spec"]["version"] == 2
    assert case["mutation_record"]["site"]["address"]
    assert case["mutation_record"]["propagation"]
    assert case["dependency_analysis"]["scopes"]
    assert case["checks"][0]["query"].startswith("SHOW INDEXES")
    assert case["query_a"] != case["query_b"]
    spec = GraphSpec.from_dict(case["graph_spec"])
    for tree in case["query_ir"].values():
        assert evaluate_query(from_data(tree), spec) == case["expected_rows"]


def test_outer_count_threshold_changes_population_not_a_pattern_recipe():
    cases = [main.prepare_legacy_case(None, "P3", 5, 0, 3,
                              composition={"contexts": ["count", "with"], "threshold": threshold})
             for threshold in (0, 2)]
    assert next(value for key, value in cases[0]["expected_rows"][0].items() if key != "u.uid") == 1
    assert next(value for key, value in cases[1]["expected_rows"][0].items() if key != "u.uid") == 3
    assert cases[0]["graph_spec"] != cases[1]["graph_spec"]


def test_expansion_preserves_answer_and_archive_protects_dependency_evidence(tmp_path):
    options = {"contexts": ["count", "with"], "threshold": 2}
    small = main.prepare_legacy_case(None, "P3", 1, 0, 3, composition=options)
    large = main.prepare_legacy_case(None, "P3", 19, 0, 3, composition=options)
    assert small["expected_rows"] == large["expected_rows"]
    settings = main.measurement_settings({})
    main.save_case(tmp_path, small, settings, {"status": "no_candidate"})
    restored, _ = main.read_case(tmp_path)
    assert restored == small
    path = tmp_path / "result.json"
    saved = json.loads(path.read_text())
    saved["case"]["dependency_analysis"]["scopes"] = []
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="extension|metadata|changed"):
        main.read_case(tmp_path)


@pytest.mark.parametrize("strip_metadata", [False, True])
def test_composed_archive_cannot_drop_the_evidence_hash(tmp_path, strip_metadata):
    case = main.prepare_legacy_case(None, "P3", 3, 0, 1, composition={"contexts": ["count", "with"]})
    main.save_case(tmp_path, case, main.measurement_settings({}), {"status": "no_candidate"})
    path = tmp_path / "result.json"
    saved = json.loads(path.read_text())
    del saved["extension_hash"]
    saved["case"]["population_evidence"]["proof"] = "changed"
    if strip_metadata:
        for field in main.COMPOSITION_EXTENSION_FIELDS:
            saved["case"].pop(field, None)
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="extension hash"):
        main.read_case(tmp_path)


def test_unverified_backends_reject_recursive_features_before_compilation():
    for database in ({"dbms": "memgraph", "language": "cypher"},
                     {"dbms": "janusgraph", "language": "gremlin"}):
        case = main.prepare_legacy_case(None, "P3", 2, 0, 1,
                                 composition={"contexts": ["count", "with"]}, database=database)
        assert case["compile_error"]["status"] == "unsupported_feature"
    bare = main.prepare_legacy_case(None, "P3", 2, 0, 1, composition={"contexts": []},
                             database={"dbms": "memgraph", "language": "cypher"})
    assert bare["compile_error"]["reason"] == "explicit_state_loading_not_verified_for_target"


def test_runtime_cannot_bypass_the_explicit_state_backend_gate(monkeypatch):
    import executor

    case = main.prepare_legacy_case(None, "P3", 3, 0, 1, composition={"contexts": []})
    monkeypatch.setattr(executor, "make_database", lambda *args: pytest.fail("unsupported target was opened"))
    report = executor.run_case(case, {"dbms": "memgraph", "language": "cypher"}, {})
    assert report["status"] == "unsupported_feature"
    assert report["reason"] == "explicit_state_loading_not_verified_for_target"


def test_large_population_remains_a_bounded_specification():
    case = main.prepare_legacy_case(None, "P3", 10**7, 0, 3,
                             composition={"contexts": ["count", "with"], "threshold": 2})
    assert "compile_error" not in case
    assert case["population_constraints"]["valid_paths"] == 3
    assert GraphSpec.from_dict(case["graph_spec"]).counts()["nodes"] >= 10**7
    assert len(json.dumps(case["graph_spec"])) < 20_000
    proof = GraphSpec.from_dict(case["population_evidence"]["validation_graph_spec"])
    assert proof.counts()["nodes"] < 100


@pytest.mark.parametrize("legacy", [{"complexity": 1}, {"state_variant": "pruning_path"}])
def test_legacy_controls_are_not_silently_ignored_by_composition(legacy):
    from generator import PatternNotApplicable

    with pytest.raises(PatternNotApplicable):
        main.prepare_legacy_case(None, "P3", 2, 0, 1, composition={"contexts": ["exists"]}, **legacy)


@pytest.mark.parametrize("contexts", [context for depth in range(4)
                                      for context in product(("exists", "count", "with"), repeat=depth)])
def test_operator_products_reuse_the_same_generation_and_mutation_rules(contexts):
    from executor import typed_bag
    from population import evaluate_query

    case = main.prepare_legacy_case(None, "P3", 6, len(contexts) % 3, 11,
                             composition={"contexts": contexts})
    assert "compile_error" not in case
    spec = GraphSpec.from_dict(case["graph_spec"])
    assert all(typed_bag(evaluate_query(from_data(tree), spec)) == typed_bag(case["expected_rows"])
               for tree in case["query_ir"].values())
