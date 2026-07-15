from __future__ import annotations

import hashlib
import json
from pathlib import Path

import graph_sym_perf.experiment.lifecycle as lifecycle_module
import graph_sym_perf.experiment.planner as planner_module
from graph_sym_perf.experiment.spec import load_experiment_spec


def verified_gdsmith_environment(root: Path, source_commit: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    jar = root / "GDsmith.jar"
    jar.write_bytes(b"planner-fixture")
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    jar.with_suffix(jar.suffix + ".manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "source_commit": source_commit,
                "compatibility_guards": [
                    "reject_multi_label_node_patterns",
                    "render_safe_double_quoted_literals",
                    "reject_unsafe_double_quoted_tokens",
                    "jdbc_query_timeout_60s",
                    "jdbc_result_row_cap_1000",
                    "neo4j_transaction_timeout_30s",
                    "neo4j_result_row_cap_1000",
                    "memgraph_transaction_timeout_30s",
                    "exclude_unbuildable_tugraph_provider",
                    "shade_runnable_jar",
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "GRAPHSYMPERF_GDSMITH_JAR": str(jar),
        "GRAPHSYMPERF_GDSMITH_JAR_SHA256": "sha256:" + digest,
    }


def assume_schema_v3_sources_clean(monkeypatch) -> None:
    """Isolate tests from the intentionally dirty live GDsmith checkout."""

    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    known_roots = {
        spec.source_path.parents[1].resolve(),
        spec.generation.checkout.resolve(),
    }
    planner_git_dirty = planner_module._git_dirty
    lifecycle_git_dirty = lifecycle_module._git_dirty

    def planner_clean(path: Path):
        return False if path.resolve() in known_roots else planner_git_dirty(path)

    def lifecycle_clean(path: Path):
        return False if path.resolve() in known_roots else lifecycle_git_dirty(path)

    monkeypatch.setattr(planner_module, "_git_dirty", planner_clean)
    monkeypatch.setattr(lifecycle_module, "_git_dirty", lifecycle_clean)
