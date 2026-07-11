"""Versioned artifact contracts."""

import json
from pathlib import Path

import pytest

from graph_sym_perf.core import QueryPair, RunConfig, RunPaths
from graph_sym_perf.reporter.artifacts import ArtifactWriter, _validate_case_artifact_paths, iter_jsonl_records


def test_query_pair_artifact_contains_both_queries_and_parameters(tmp_path) -> None:
    config = RunConfig(
        source_path="baseline",
        paths=RunPaths(artifact_dir=str(tmp_path / "{run_id}")),
        systems=(),
        dataset=None,  # type: ignore[arg-type]  # ArtifactWriter does not consume dataset.
        query_generation=None,  # type: ignore[arg-type]
        equivalence=None,  # type: ignore[arg-type]
        confirmation=None,  # type: ignore[arg-type]
    )
    pair = QueryPair(
        case_id="case-1",
        original_query="MATCH (n) RETURN n",
        transformed_query="MATCH (n) RETURN n",
        parameters={"limit": 1},
        transform_rule="baseline",
        proof_obligation="baseline characterization",
    )

    paths = ArtifactWriter(config, "baseline-run").write_query_pair("neo4j", pair)

    run_dir = tmp_path / "baseline-run"
    assert set(paths) == {"query", "query_transformed", "parameters"}
    assert (run_dir / paths["query"]).read_text(encoding="utf-8").strip() == pair.original_query
    assert (run_dir / paths["query_transformed"]).read_text(encoding="utf-8").strip() == pair.transformed_query
    assert json.loads((run_dir / paths["parameters"]).read_text(encoding="utf-8")) == {"limit": 1}
    assert all(not Path(relative).is_absolute() for relative in paths.values())


def test_content_addressed_payload_is_deduplicated_and_jsonl_reader_streams(tmp_path) -> None:
    config = RunConfig(
        source_path="contract",
        paths=RunPaths(artifact_dir=str(tmp_path / "{run_id}")),
        systems=(),
        dataset=None,  # type: ignore[arg-type]
        query_generation=None,  # type: ignore[arg-type]
        equivalence=None,  # type: ignore[arg-type]
        confirmation=None,  # type: ignore[arg-type]
    )
    writer = ArtifactWriter(config, "run")
    first = writer.write_content_addressed("plan", {"operator": "Scan"})
    second = writer.write_content_addressed("same-plan", {"operator": "Scan"})
    assert first.content_hash == second.content_hash
    assert first.relative_path == second.relative_path

    writer.append_jsonl("cases.jsonl", {"case_id": "a"})
    writer.append_jsonl("cases.jsonl", {"case_id": "b"})
    records = iter_jsonl_records(writer.run_dir / "cases.jsonl")
    assert iter(records) is records
    assert [record["case_id"] for record in records] == ["a", "b"]


def test_cases_stream_rejects_explanation_payloads_and_escaping_paths() -> None:
    with pytest.raises(ValueError, match="explanation-layer"):
        _validate_case_artifact_paths({"profile_samples": "inline.json"})
    with pytest.raises(ValueError, match="relative"):
        _validate_case_artifact_paths({"query": "../outside.cypher"})
