import hashlib
import json
import sys
from pathlib import Path

import pytest

from generator import (
    PatternNotApplicable,
    SeedImportError,
    abstract_seed,
    import_log,
    instantiate,
    load_seeds,
)
from mutator import find_patterns, mutate
from query import Case, Comparison, ConditionalQuery, Count, Literal, Property, parse_query, render_query


FIXTURE = Path(__file__).parent / "fixtures/gdsmith/neo4j_seeds.log"


def seeds():
    return load_seeds({"log_files": [str(FIXTURE)]})


def test_real_excerpt_provenance_and_original_lines():
    imported = seeds()
    assert len(imported) == 2
    assert [s.source["source_line"] for s in imported] == [391, 275]
    assert all(s.source["generator"] == "gdsmith" for s in imported)
    for source in imported:
        assert hashlib.sha256(source.text.encode()).hexdigest() == source.source["query_sha256"]
    assert imported[0].source["jar_sha256"] is None


PATTERNS = [(f"P{i}", 1 if i == 5 else 0) for i in range(1, 17)]


@pytest.mark.parametrize("pattern,index", PATTERNS)
def test_all_patterns_come_from_real_seed_and_roundtrip(pattern, index):
    source = seeds()[index]
    case = instantiate(source, pattern, 4, 17)
    assert case == instantiate(source, pattern, 4, 17)
    assert pattern in find_patterns(case["query_a"], bindings=case["bindings"])
    changed = mutate(case["query_a"], pattern, bindings=case["bindings"])
    assert parse_query(render_query(changed)) == changed
    assert case["source"]["raw_query"] == source.text
    assert case["source"]["derivation_preserves_original_result"] is False
    assert case["placeholders"] and "$lowCost" in case["skeleton"]
    if pattern == "P16":
        assert case["setup"] == []
    else:
        assert sum("CREATE INDEX gsp_cost_lookup" in q for q in case["setup"]) == 1
        assert any(check["expected_rows"][0].get("state") == "ONLINE" for check in case["checks"])
    assert case["expected_rows"]
    assert all(set(check) == {"query", "expected_rows"} for check in case["checks"])
    if pattern not in {"P1", "P4", "P5"}:
        assert case["source"]["seed_syntax_enriched"] is True
        assert case["source"]["enrichment_origin"] == "python_seed_adapter"
    json.dumps({k: v for k, v in case.items() if k not in {"query_a", "query_ref"}})


@pytest.mark.parametrize("pattern,index", PATTERNS)
def test_scaling_changes_work_region_not_expected_rows(pattern, index):
    small = instantiate(seeds()[index], pattern, 1, 0)
    large = instantiate(seeds()[index], pattern, 100, 0)
    assert small["expected_rows"] == large["expected_rows"]
    if pattern == "P16":
        assert small["query_a"] != large["query_a"]
        assert small["setup"] == large["setup"] == []
    else:
        assert small["query_a"] == large["query_a"]
        assert small["setup"] != large["setup"]
    assert small["checks"] != large["checks"]


def test_pattern_is_not_manufactured_when_seed_has_no_boolean_site():
    with pytest.raises(PatternNotApplicable):
        abstract_seed("MATCH (n:A) RETURN n.id", "P4")
    with pytest.raises(PatternNotApplicable):
        abstract_seed('MATCH (n:A) WHERE n.x = "AND" RETURN n.id', "P4")
    with pytest.raises(PatternNotApplicable):
        abstract_seed("MATCH (n:A) RETURN n.id", "P1")


def test_import_rejects_writes_and_deduplicates(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text(
        "CREATE (n);\nMATCH (n) DELETE n RETURN n;\nMATCH (n) RETURN n.id;\nMATCH (n) RETURN n.id;\n"
    )
    assert len(import_log(log)) == 1


def test_tampered_fixture_is_not_given_gdsmith_provenance(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text("MATCH (n) RETURN n.id;\n")
    log.with_suffix(".source.json").write_text(json.dumps({"fixture_sha256": "wrong"}))
    with pytest.raises(SeedImportError):
        import_log(log)


def test_successful_command_cannot_pass_stale_logs_as_new_generation(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text("MATCH (n) RETURN n.id;\n")
    with pytest.raises(SeedImportError, match="refresh"):
        load_seeds({"log_files": [str(log)], "command": [sys.executable, "-c", "pass"]})


def test_inverse_relationship_contract_includes_both_directions():
    case = instantiate(seeds()[0], "P2", 5, 1)
    assert case["bindings"]["inverse_relationship"] == {
        "forward_type": "GSP_FORWARD",
        "reverse_type": "GSP_REVERSE",
        "pair_key": "pair",
    }
    inverse_checks = [check for check in case["checks"] if "inverse_count <> 1" in check["query"]]
    assert len(inverse_checks) == 2
    assert all(check["expected_rows"] == [{"count": 0}] for check in inverse_checks)
    assert "f:GSP_FORWARD" in inverse_checks[0]["query"]
    assert "f:GSP_REVERSE" in inverse_checks[1]["query"]


def test_optional_and_case_cover_null_without_losing_witness_rows():
    optional = instantiate(seeds()[0], "P13", 5, 1)
    assert optional["expected_rows"] == [
        {"u.uid": 1, "v.uid": 10, "w.uid": 20},
        {"u.uid": 2, "v.uid": 11, "w.uid": None},
        {"u.uid": 3, "v.uid": None, "w.uid": 21},
    ]
    case = instantiate(seeds()[0], "P15", 5, 1)
    assert case["bindings"]["condition_values"] == [True, False, None]
    assert case["expected_rows"][-1] == {"u.uid": 3, "value": False}


def test_quantifier_records_predicate_only_cost_boundary():
    case = instantiate(seeds()[0], "P16", 5, 1)
    assert case["expected_rows"] == [{"ok": False}]
    assert case["bindings"]["high_list_range"] == [1, 20]
    assert "list construction remains required" in case["bindings"]["pruning_scope"]
    assert case["checks"][0]["expected_rows"] == [{"count": 20}]


def test_new_syntax_is_explicit_enrichment_not_claimed_gdsmith_output():
    source = seeds()[1]
    assert "CASE" not in source.text and "UNION" not in source.text
    for pattern in ("P3", "P8", "P14", "P15", "P16"):
        skeleton = abstract_seed(source, pattern)
        assert skeleton.source["raw_query"] == source.text
        assert skeleton.source["seed_syntax_enriched"]
        assert any(step.startswith("derive_") for step in skeleton.source["derivation_steps"])


@pytest.mark.parametrize("scale,seed", [(0, 0), (-1, 0), (True, 0), (1, -1), (1, True)])
def test_invalid_population_parameters_fail_explicitly(scale, seed):
    with pytest.raises(ValueError):
        instantiate(seeds()[0], "P1", scale, seed)


def test_unimplemented_pattern_fails_instead_of_falling_back():
    with pytest.raises(PatternNotApplicable, match="not implemented"):
        instantiate(seeds()[0], "P17", 1, 0)


def test_external_failure_does_not_disclose_argv_or_process_output(tmp_path):
    log = tmp_path / "seed.log"
    secret = "not-a-real-secret-for-redaction-test"
    with pytest.raises(SeedImportError) as caught:
        load_seeds(
            {
                "log_files": [str(log)],
                "command": [sys.executable, "-c", f"print({secret!r}); raise SystemExit(7)"],
            }
        )
    assert secret not in str(caught.value)
    assert "code 7" in str(caught.value)


def test_external_command_must_be_argv_not_shell_text(tmp_path):
    with pytest.raises(ValueError, match="argv"):
        load_seeds({"log_files": [str(tmp_path / "seed.log")], "command": "java -jar seed.jar"})


SCOR_PATTERNS = [f"S{i}" for i in range(1, 11)]
SCOR_PAPER_IDS = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2"]


@pytest.mark.parametrize("pattern,paper_id", list(zip(SCOR_PATTERNS, SCOR_PAPER_IDS)))
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_scor_population_is_explicit_graph_adaptation_with_reference_only_mutation(
    pattern, paper_id, seed
):
    source = seeds()[0]
    case = instantiate(source, pattern, 2, seed)
    assert case == instantiate(source, pattern, 2, seed)
    assert pattern in find_patterns(case["query_a"], bindings=case["bindings"])
    changed = mutate(case["query_a"], pattern, bindings=case["bindings"])
    assert parse_query(render_query(changed)) == changed
    assert render_query(changed) == render_query(case["query_a"]).replace(
        "GSP_SC_HIGH", "GSP_SC_REF"
    )
    assert case["source"]["raw_query"] == source.text
    assert case["source"]["oracle_mode"] == "high_cost_reference_replacement"
    assert case["source"]["paper_doi"] == "10.1145/3802061"
    assert case["source"]["paper_table"] == "Table 1"
    assert case["source"]["paper_pattern"] == paper_id
    assert case["source"]["seed_syntax_enriched"]
    assert case["source"]["derivation_preserves_original_result"] is False
    contract = case["bindings"]["scor"]
    assert contract["pattern"] == pattern and contract["paper_pattern"] == paper_id
    assert contract["high_matches"] == 8 and contract["reference_matches"] == 3
    assert bool(contract.get("all_candidates_match")) == (pattern == "S8")
    assert any(slot["role"] == "high" for slot in case["placeholders"])
    assert all(slot["kind"] != "structural" for slot in case["placeholders"])
    assert all(set(check) == {"query", "expected_rows"} for check in case["checks"])
    json.dumps({key: value for key, value in case.items() if key != "query_a"})


@pytest.mark.parametrize("pattern", SCOR_PATTERNS)
def test_scor_scale_only_expands_high_region_and_preserves_result(pattern):
    small = instantiate(seeds()[1], pattern, 1, 2)
    large = instantiate(seeds()[1], pattern, 100, 2)
    assert small["query_a"] == large["query_a"]
    assert small["expected_rows"] == large["expected_rows"]
    assert small["setup"][-1] == large["setup"][-1]  # fixed reference region
    assert small["setup"][-2] != large["setup"][-2]  # scalable high region
    assert small["bindings"]["scor"]["reference_matches"] == 3
    assert large["bindings"]["scor"]["high_matches"] == 400
    reference_checks = [check for check in large["checks"] if "GSP_SC_REF" in check["query"]]
    assert len(reference_checks) == 2
    assert all(check["expected_rows"] == [{"count": 3}] for check in reference_checks)
    assert any("v.mark = true AND v.key = u.key" in check["query"] for check in reference_checks)


def test_scor_native_conditional_is_distinct_from_case_and_declares_cypher25():
    native = instantiate(seeds()[0], "S3", 1, 0)
    assert isinstance(native["query_a"], ConditionalQuery)
    assert native["required_cypher"] == 25
    assert render_query(native["query_a"]).startswith("CYPHER 25 WHEN true")
    assert "native Cypher 25" in native["source"]["graph_adaptation"]


@pytest.mark.parametrize("seed,condition", [(0, True), (1, False), (2, None)])
def test_scor_case_true_false_null_variants_always_leave_count_unselected(seed, condition):
    case = instantiate(seeds()[0], "S4", 1, seed)
    expression = case["query_a"].returns[0].expression
    assert isinstance(expression, Case) and expression.condition == Literal(condition)
    selected = expression.then if condition is True else expression.otherwise
    unselected = expression.otherwise if condition is True else expression.then
    assert selected == Literal(7) and isinstance(unselected, Count)
    assert case["expected_rows"] == [{"value": 7}]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_scor_limit_n_has_enough_reference_rows_and_preserves_bag_multiplicity(seed):
    case = instantiate(seeds()[0], "S6", 1, seed)
    limit = 1 + seed % 3
    assert case["query_a"].limit == limit
    assert case["bindings"]["scor"]["reference_matches"] >= limit
    assert case["bindings"]["scor"]["high_matches"] >= limit
    assert case["expected_rows"] == [{"value": 1}] * limit


@pytest.mark.parametrize("pattern", ["S5", "S9", "S10"])
@pytest.mark.parametrize("seed", [0, 1])
def test_scor_empty_results_have_nonempty_regions_without_projection_aggregates(pattern, seed):
    case = instantiate(seeds()[0], pattern, 2, seed)
    assert case["expected_rows"] == []
    assert case["bindings"]["scor"]["reference_matches"] > 0
    assert "COUNT {" not in render_query(case["query_a"])
    if pattern == "S10":
        assert case["query_a"].distinct
        assert case["bindings"]["scor"]["native_set_operator"] is False
        assert "not native Cypher set operators" in case["source"]["graph_adaptation"]
        assert render_query(case["query_a"]).count("IS NULL") == 2


def test_scor_where_false_variant_reports_its_actual_skeleton_control():
    case = instantiate(seeds()[0], "S5", 1, 1)
    assert case["query_a"].limit is None and case["query_a"].where == Literal(False)
    assert "WHERE $lowCostBool" in case["skeleton"]
    assert case["placeholders"][0]["kind"] == "boolean_expression"


def test_unknown_scor_pattern_fails_without_reusing_another_builder():
    with pytest.raises(PatternNotApplicable):
        instantiate(seeds()[0], "S11", 1, 0)


@pytest.mark.parametrize('pattern', ['S1', 'S2'])
def test_scor_boolean_work_is_exact_counting_not_just_an_existence_test(pattern):
    case = instantiate(seeds()[0], pattern, 3, 0)
    expensive = case['query_a'].returns[0].expression.terms[1]
    assert isinstance(expensive, Comparison) and isinstance(expensive.left, Count)
    assert expensive.operator == '=' and expensive.right == Property('u', 'expected_count')
    assert any(check['expected_rows'] == [{'uid': 1, 'key': 1, 'expected_count': 12}]
               for check in case['checks'])


@pytest.mark.parametrize("pattern", [f"P{i}" for i in range(1, 17)] + SCOR_PATTERNS)
def test_builtin_role_pools_have_real_query_variation_and_reuse_one_checked_state(pattern):
    cases = [instantiate(None, pattern, 3, seed, data_seed=0) for seed in range(30)]
    assert len({render_query(case["query_a"]) for case in cases}) > 1
    assert len({tuple(case["setup"]) for case in cases}) == 1
    assert len({json.dumps(case["checks"], sort_keys=True) for case in cases}) == 1
    for case in cases:
        assert case["data_seed"] == case["source"]["data_seed"] == 0
        assert case["source"]["generator"] == "builtin_template"
        assert case["source"]["raw_query"] is None
        assert case["source"]["generation_mode"] == "builtin_template"
        assert case["source"]["template_id"] == case["template_id"]
        assert case["role_choices"] == case["source"]["role_choices"]
        assert case["variant"] == case["bindings"]["variant"]
        assert pattern in find_patterns(case["query_a"], bindings=case["bindings"])
        changed = mutate(case["query_a"], pattern, bindings=case["bindings"])
        assert parse_query(render_query(changed)) == changed
        if pattern.startswith("S"):
            assert case["reference_side"] == "b"


def test_data_seed_changes_layout_without_resampling_query_choices():
    first = instantiate(None, "P4", 3, 17, data_seed=0)
    second = instantiate(None, "P4", 3, 17, data_seed=1)
    assert first["query_a"] == second["query_a"]
    assert first["query_ref"] == second["query_ref"]
    assert first["template_id"] == second["template_id"]
    assert first["setup"] != second["setup"]
    assert first["expected_rows"] == second["expected_rows"]


@pytest.mark.parametrize("data_seed", [-1, True, "0"])
def test_invalid_data_seed_is_rejected(data_seed):
    with pytest.raises(ValueError, match="data_seed"):
        instantiate(None, "P1", 1, 0, data_seed=data_seed)


@pytest.mark.parametrize("pattern", ["P4", "P5", "P15", "P16"])
def test_pattern_reference_has_an_explicit_decisive_fact_and_keeps_symmetry_pair(pattern):
    case = instantiate(None, pattern, 4, 17, data_seed=0)
    reference = case["query_ref"]
    assert reference != case["query_a"]
    assert parse_query(render_query(reference)) == reference
    assert case["reference_side"] == "ref"
    assert case["reference_reason"]
    assert mutate(case["query_a"], pattern, bindings=case["bindings"]) != reference
    if pattern != "P16":
        setup = case["setup"][-1]
        assert "coalesce(u.keep, false)" in setup
        assert "GSP_REFERENCE" in setup
        assert "GSP_REFERENCE" in render_query(reference)
        assert any("NOT EXISTS" in check["query"] for check in case["checks"])


@pytest.mark.parametrize("pattern", ["P1", "P2", "P3", "P6", "P7", "P8", "P9", "P10", "P11", "P12", "P13", "P14"])
def test_pattern_without_a_decisive_fact_is_not_given_an_answer_label_reference(pattern):
    case = instantiate(None, pattern, 4, 17, data_seed=0)
    assert "query_ref" not in case
    assert all("REFERENCE" not in statement for statement in case["setup"])


@pytest.mark.parametrize("complexity", [1, 2])
@pytest.mark.parametrize("pattern", [f"P{i}" for i in range(1, 17)] + SCOR_PATTERNS)
def test_complexity_sampling_is_reproducible_keeps_state_and_preserves_pattern_contract(pattern, complexity):
    cases = [instantiate(None, pattern, 3, seed, data_seed=0, complexity=complexity)
             for seed in range(100, 116)]
    assert len({tuple(case["setup"]) for case in cases}) == 1
    assert len({json.dumps(case["checks"], sort_keys=True) for case in cases}) == 1
    for case in cases:
        assert case == instantiate(None, pattern, 3, case["seed"], data_seed=0, complexity=complexity)
        assert case["complexity"] == case["source"]["complexity"] == complexity
        assert case["source"]["role_sampling"] == "independent_seeded_random"
        assert pattern in find_patterns(case["query_a"], bindings=case["bindings"])
        changed = mutate(case["query_a"], pattern, bindings=case["bindings"])
        assert parse_query(render_query(changed)) == changed
        if pattern.startswith("S"):
            spec = case["bindings"]["scor"]
            assert spec["guard_check"] in case["checks"]
            assert spec["guard_cardinality_check"] in case["checks"]


@pytest.mark.parametrize("pattern", ["P4", "P5", "P15", "P16", "S1", "S2", "S3", "S4", "S7"])
def test_nested_space_expands_actual_query_shapes_not_only_labels_or_seeds(pattern):
    plain = {render_query(instantiate(None, pattern, 3, seed, data_seed=0)["query_a"])
             for seed in range(100, 180)}
    nested = [instantiate(None, pattern, 3, seed, data_seed=0, complexity=2)
              for seed in range(100, 180)]
    assert len({render_query(case["query_a"]) for case in nested}) > len(plain)
    assert len({case["template_id"] for case in nested}) == len({render_query(case["query_a"]) for case in nested})


@pytest.mark.parametrize("complexity", [-1, 3, True, "2"])
def test_invalid_complexity_does_not_silently_change_generation(complexity):
    with pytest.raises(ValueError, match="complexity"):
        instantiate(None, "P4", 1, 0, complexity=complexity)
