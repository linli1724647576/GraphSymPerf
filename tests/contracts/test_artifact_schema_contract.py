"""Characterization contracts for legacy artifact writing.

Task 10 will replace this surface with versioned content-addressed artifacts.
Until then, a query pair must still be written with its two query texts and
parameter payload so migration can account for every legacy record.
"""

from graph_sym_perf.core import QueryPair, RunConfig, RunPaths
from graph_sym_perf.reporter.artifacts import ArtifactWriter


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
    assert '"limit": 1' in (run_dir / paths["parameters"]).read_text(encoding="utf-8")
