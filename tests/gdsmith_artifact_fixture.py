from __future__ import annotations

import hashlib
import json
from pathlib import Path


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
