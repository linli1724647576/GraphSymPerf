from __future__ import annotations

from pathlib import Path

from graph_sym_perf.app.mining import _render_profile_alias_text
from graph_sym_perf.core import ExecutionResult, FeedbackConfig, PlanRecord, QueryCase, TargetSystem, TransformConfig
from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.experiment.runner import (
    _prepare_known_issue_setup,
    _write_known_replay_summary_artifacts,
    load_known_replay_cases,
)
from graph_sym_perf.feedback import initialize_feedback_state
from graph_sym_perf.feedback.scheduler import TransformScheduler
from graph_sym_perf.plan import make_plan_record
from graph_sym_perf.reporter.artifacts import ArtifactWriter
from graph_sym_perf.transforms.bool_redundancy import generate_bool_redundancy_family


def test_load_known_replay_cases_reads_transform_and_target_fields(tmp_path: Path) -> None:
    cases_path = tmp_path / "known_cases.jsonl"
    cases_path.write_text(
        "\n".join(
            [
                (
                    '{"case_id":"neo4j_13783_fixed","template_name":"replay_known_issue","query":"MATCH (n0) WHERE n0.id > 0 RETURN n0",'
                    '"parameters":{},"base_predicate":"n0.id > 0","safe_expression":"(n0.id = n0.id)",'
                    '"transform_family":"bool_redundancy","transform_rule":"or_false_and","target_system":"neo4j",'
                    '"setup_path":"fixtures/repro.cypher.txt","expected_nodes":128,"expected_relationships":95,'
                    '"developer_status":"fixed","issue_url":"https://github.com/neo4j/neo4j/issues/13783"}'
                ),
                ""
            ]
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "replay_manifest.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "experiment_id: replay_known_test",
                "output_root: runs/replay_known_test",
                "systems:",
                "  targets:",
                "    - neo4j",
                "source:",
                "  kind: cases_jsonl",
                f"  cases_path: {cases_path.as_posix()}",
                "  base_run_config: config/graph_sym_perf.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cases = load_known_replay_cases(manifest_path)

    assert len(cases) == 1
    assert cases[0].transform_family == "bool_redundancy"
    assert cases[0].transform_rule == "or_false_and"
    assert cases[0].target_system == "neo4j"
    assert cases[0].setup_path == "fixtures/repro.cypher.txt"
    assert cases[0].expected_nodes == 128
    assert cases[0].expected_relationships == 95
    assert cases[0].developer_status == "fixed"
    assert cases[0].issue_url == "https://github.com/neo4j/neo4j/issues/13783"


def test_scheduler_honors_case_pinned_transform() -> None:
    config = TransformConfig(
        enabled_families=("bool_redundancy", "symmetry"),
        enabled_rules=("or_false_and", "pattern_order_symmetry"),
    )
    scheduler = TransformScheduler(
        config,
        FeedbackConfig(enabled=False),
        initialize_feedback_state(config, FeedbackConfig(enabled=False)),
    )
    case = QueryCase(
        case_id="neo4j_13783_fixed",
        template_name="replay_known_issue",
        query="MATCH (n0) WHERE n0.id > 0 RETURN n0",
        parameters={},
        base_predicate="n0.id > 0",
        safe_expression="(n0.id = n0.id)",
        transform_family="bool_redundancy",
        transform_rule="or_false_and",
    )

    decision = scheduler.choose_for_case(case)

    assert decision.family == "bool_redundancy"
    assert decision.enabled_rules == ("or_false_and",)
    assert decision.reason == "case_pinned_transform"


def test_bool_redundancy_issue_case_uses_explicit_rule() -> None:
    case = QueryCase(
        case_id="neo4j_13814_wontfix",
        template_name="replay_known_issue",
        query="MATCH (n1:L0) WHERE n1.id > 0 RETURN n1",
        parameters={},
        base_predicate="n1.id > 0",
        safe_expression="(elementId(n1) = elementId(n1))",
        source="known_issue",
        transform_rule="or_expr_null_contradiction",
    )

    family = generate_bool_redundancy_family(case)

    assert len(family.variants) == 1
    assert family.variants[0].transform_rule == "or_expr_null_contradiction"
    assert "IS NULL AND" in family.variants[0].query


def test_prepare_known_issue_setup_resets_loads_and_verifies_counts(tmp_path: Path, monkeypatch) -> None:
    setup_path = tmp_path / "fixture.cypher.txt"
    setup_path.write_text("CREATE (n {id: 1});\nCREATE (n {id: 2});\n", encoding="utf-8")
    manifest_path = tmp_path / "replay_manifest.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "experiment_id: replay_known_test",
                "output_root: runs/replay_known_test",
                "systems:",
                "  targets:",
                "    - memgraph",
                "source:",
                "  kind: cases_jsonl",
                "  cases_path: known_cases.jsonl",
                "  base_run_config: config/graph_sym_perf.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = load_experiment_manifest(manifest_path)
    monkeypatch.setenv("GRAPHSYMPERF_REPLAY_ALLOW_DESTRUCTIVE", "1")

    reset_calls: list[str] = []

    def fake_reset(adapter) -> None:
        reset_calls.append(adapter.target.value)

    monkeypatch.setattr("graph_sym_perf.experiment.runner._reset_adapter_for_known_issue_setup", fake_reset)

    class FakeAdapter:
        target = TargetSystem.MEMGRAPH

        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, query, parameters, query_kind, **kwargs):
            del parameters, query_kind, kwargs
            self.queries.append(query)
            if query == "MATCH (n) RETURN count(n) AS nodes":
                return ExecutionResult(
                    system=self.target,
                    query_kind="count_nodes",
                    rows=({"nodes": 2},),
                    elapsed_ms=0.0,
                    summary={},
                )
            if query == "MATCH ()-[r]->() RETURN count(r) AS relationships":
                return ExecutionResult(
                    system=self.target,
                    query_kind="count_rels",
                    rows=({"relationships": 0},),
                    elapsed_ms=0.0,
                    summary={},
                )
            return ExecutionResult(
                system=self.target,
                query_kind="setup",
                rows=(),
                elapsed_ms=0.0,
                summary={},
            )

    case = QueryCase(
        case_id="memgraph_4208_pending",
        template_name="replay_known_issue",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
        setup_path=setup_path.name,
        expected_nodes=2,
        expected_relationships=0,
    )

    adapter = FakeAdapter()
    _prepare_known_issue_setup(adapter, manifest, case)

    assert reset_calls == ["memgraph"]
    assert adapter.queries[:2] == ["CREATE (n {id: 1})", "CREATE (n {id: 2})"]
    assert adapter.queries[-2:] == [
        "MATCH (n) RETURN count(n) AS nodes",
        "MATCH ()-[r]->() RETURN count(r) AS relationships",
    ]


def test_write_known_replay_summary_artifacts_emits_expected_files(tmp_path: Path) -> None:
    class DummyPaths:
        def artifact_path(self, run_id: str) -> Path:
            return tmp_path / run_id

    class DummyConfig:
        paths = DummyPaths()

    writer = ArtifactWriter(DummyConfig(), "shard_000")
    case = QueryCase(
        case_id="neo4j_13783_fixed",
        template_name="replay_known_issue",
        query="MATCH (n) RETURN n",
        parameters={},
        base_predicate="true",
        safe_expression="true",
    )
    family = generate_bool_redundancy_family(case, TransformConfig(enabled_rules=("or_false_and",)))
    record = _annotate_case_record_for_test(family)

    _write_known_replay_summary_artifacts(
        writer,
        {"cases_assigned": 1, "equivalence_passed": 1, "runtime_confirmed": 0, "profile_supported_candidates": 0, "unique_issue_count": 0},
        [record],
    )

    assert (writer.run_dir / "replay_summary.json").exists()
    markdown = (writer.run_dir / "replay_summary.md").read_text(encoding="utf-8")
    assert "Known Issue Replay Summary" in markdown
    assert "neo4j_13783_fixed" in markdown


def test_render_profile_alias_text_contains_metrics_and_plan_text() -> None:
    plan = make_plan_record(TargetSystem.NEO4J, "seed_explain", "NodeByLabelScan")
    profile = type(
        "ProfileStub",
        (),
        {
            "query_kind": "seed",
            "available": True,
            "median_ms": 12.5,
            "samples_ms": (12.0, 13.0),
            "metrics": {"db_hits": 42.0},
            "plan": plan,
            "profile_plan": plan.normalized_plan,
        },
    )()

    text = _render_profile_alias_text(profile, label="base")

    assert "# GraphSymPerf Replay Profile: base" in text
    assert "- db_hits: 42.0" in text
    assert "NodeByLabelScan" in text


def _annotate_case_record_for_test(family):
    from graph_sym_perf.app.mining import _case_record

    return _case_record(
        TargetSystem.NEO4J,
        family,
        (),
        None,
        None,
        {},
        "equivalent_no_candidate",
        "NO_SYMMETRY_VIOLATION_SIGNAL",
    )
