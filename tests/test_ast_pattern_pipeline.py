"""The public AST-first path, phase archive integrity and explicit compatibility."""

from copy import deepcopy
import json

import pytest
import yaml

import main
from ast_pattern.contracts import StageFailure
from ast_pattern.pipeline import generate_case, reproduce_generation, validate_stage_records
from executor import typed_bag
from ir import from_data
from population import evaluate_query
from state import GraphSpec


PATTERNS = [*(f"P{i}" for i in range(1, 14)), *(f"S{i}" for i in range(1, 11))]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_every_paper_rule_uses_six_stages_from_the_public_default(pattern):
    case = main.prepare_case(None, pattern, 7, 0, 3)
    assert case["method_version"] == "ast-pattern-v1"
    assert case["source"]["generator"] == "ast_first"
    assert not case.get("compile_error"), case.get("compile_error")
    assert [r["stage"] for r in validate_stage_records(case)] == [f"M{i}" for i in range(1, 7)]
    assert case["dependency_report"]["static"]["target_valid"] is True
    assert case["dependency_report"]["runtime"]["status"] == "not_run"
    spec = GraphSpec.from_dict(case["graph_spec"])
    for side in ("a", "b"):
        assert typed_bag(evaluate_query(from_data(case["query_ir"][side]), spec)) == typed_bag(case["expected_rows"])


@pytest.mark.parametrize("form", ["independent", "shared_anchor", "chain"])
def test_p10_forms_are_selected_after_the_same_random_host(form):
    case = main.prepare_case(None, "P10", 5, 0, 3, ast_options={"pattern_options": {"form": form}})
    assert case["pattern_form"] == form
    assert case["stage_records"][0]["stage"] == "M1"


def test_ast_entry_never_invokes_old_complete_skeleton_or_template_generation(monkeypatch):
    import generator
    import symbolic
    monkeypatch.setattr(generator, "instantiate", lambda *a, **k: pytest.fail("legacy template path"))
    monkeypatch.setattr(symbolic, "build_symbolic_skeleton", lambda *a, **k: pytest.fail("pattern-first skeleton"))
    case = main.prepare_case(None, "S7", 5, 0, 3)
    assert case["mutation_direction"] == "L_to_H"
    assert case["side_roles"] == {"a": "L", "b": "H"}
    assert case["reference_side"] == "a"


def test_generation_replay_reproduces_every_stage_digest():
    case = generate_case("P3", 3, 0, 3)
    assert reproduce_generation(case)["stage_records"] == case["stage_records"]


def test_bounded_failures_preserve_stage_evidence_and_do_not_fallback(monkeypatch):
    import ast_pattern.state_builder as builder
    monkeypatch.setattr(builder, "construct_graph_state", lambda *a, **k: (_ for _ in ()).throw(
        StageFailure("M3", "budget_exhausted", "explicit test budget")))
    with pytest.raises(StageFailure) as rejected:
        generate_case("P3", 3, 0, 3, options={"max_attempts": 2})
    assert rejected.value.code == "generation_budget_exhausted"
    failures = rejected.value.evidence["failures"]
    assert len(failures) == 2
    assert all(f["stage"] == "M3" and [s["stage"] for s in f["stages"]] == ["M1", "M2"] for f in failures)


def test_archive_format_four_preserves_phase_artifacts_and_runtime(tmp_path):
    case = main.prepare_case(None, "S7", 3, 0, 3)
    main.save_case(tmp_path, case, {}, {"status": "no_candidate"})
    assert json.loads((tmp_path / "result.json").read_text())["format_version"] == 4
    restored, _ = main.read_case(tmp_path)
    assert restored == case
    assert {f"M{i}" for i in range(1, 7)} <= {p.name.split("-")[0] for p in tmp_path.glob("M*.json")}
    assert (tmp_path / "M3-state.json").is_file()
    assert (tmp_path / "M3-bindings.json").is_file()
    assert (tmp_path / "M6-runtime.json").is_file()


@pytest.mark.parametrize("filename", ["M1-host.json", "M2-embedding.json", "M3-state.json",
                                      "M3-bindings.json", "M4-instantiation.json", "M5-pair.json",
                                      "M6-dependencies.json", "M6-runtime.json"])
def test_archive_rejects_missing_or_changed_stage_files(tmp_path, filename):
    case = main.prepare_case(None, "P3", 3, 0, 3)
    main.save_case(tmp_path, case, {}, {"status": "no_candidate"})
    (tmp_path / filename).write_text("{}\n")
    with pytest.raises(ValueError, match="artifact"):
        main.read_case(tmp_path)


@pytest.mark.parametrize("field", ["method_version", "stage_records", "dependency_report", "side_roles"])
def test_new_archive_cannot_drop_method_contracts(tmp_path, field):
    case = main.prepare_case(None, "P3", 3, 0, 3)
    main.save_case(tmp_path, case, {}, {})
    record = json.loads((tmp_path / "result.json").read_text())
    record["case"].pop(field)
    (tmp_path / "result.json").write_text(json.dumps(record))
    with pytest.raises(ValueError):
        main.read_case(tmp_path)


def test_config_defaults_to_ast_method_and_keeps_old_routes_explicit(tmp_path):
    path = tmp_path / "config.yaml"
    config = {"database": {"dedicated": True, "allow_reset": True}, "patterns": ["P3"]}
    path.write_text(yaml.safe_dump(config))
    result = main.read_config(path)
    assert result["generation"]["method"] == "ast-pattern-v1"
    assert result["pattern_catalog"] == "paper-v1"
    config["generation"] = {"composition": {"contexts": []}}
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="explicit"):
        main.read_config(path)


def test_compatibility_entry_requires_explicit_method():
    legacy = main.prepare_case(None, "P3", 3, 0, 3, method="legacy-v1")
    assert legacy["graph_spec"]["version"] == 1
    assert "method_version" not in legacy
    symbolic = main.prepare_case(None, "P3", 3, 0, 3, method="symbolic-v1", composition={"contexts": []})
    assert symbolic["source"]["generation_method"] == "recursive_symbolic"
    with pytest.raises(ValueError, match="legacy"):
        main.prepare_case(None, "P3", 3, 0, 3, composition={"contexts": []})


def test_recompile_during_replay_preserves_format_four(tmp_path, monkeypatch):
    case = main.prepare_case(None, "P3", 3, 0, 3)
    source = tmp_path / "source"
    main.save_case(source, case, {}, {"status": "no_candidate"})
    monkeypatch.setattr(main, "run_case", lambda *args, **kwargs: {"status": "no_candidate"})
    config = {"database": {"dbms": "neo4j", "verified_features": ["all"]},
              "output": str(tmp_path / "replay")}
    assert main.replay(source, config) == 0
    path = next((tmp_path / "replay").rglob("result.json"))
    archived = json.loads(path.read_text())
    assert archived["format_version"] == 4
    replayed, _ = main.read_case(path.parent)
    assert replayed["stage_records"] == case["stage_records"]
    assert replayed["source_input_hash"] == main.case_digest(case)


@pytest.mark.parametrize("field", ["construction_record", "instantiation_record", "mutation_record", "side_roles"])
def test_stage_chain_binds_public_case_contracts(field):
    case = main.prepare_case(None, "P3", 3, 0, 3)
    case[field] = {"changed": True}
    with pytest.raises(ValueError):
        validate_stage_records(case)
