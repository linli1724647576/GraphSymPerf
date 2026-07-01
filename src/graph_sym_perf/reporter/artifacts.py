"""Run artifact writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graph_sym_perf.core import CaseRecord, QueryFamily, QueryPair, RunConfig, stable_json_dumps, to_jsonable


class ArtifactWriter:
    def __init__(self, config: RunConfig, run_id: str) -> None:
        self.run_id = run_id
        self.run_dir = config.paths.artifact_path(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, relative_path: str, payload: Any) -> Path:
        path = self.run_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def write_text(self, relative_path: str, payload: str) -> Path:
        path = self.run_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return path

    def append_jsonl(self, relative_path: str, payload: Any) -> Path:
        path = self.run_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(stable_json_dumps(payload) + "\n")
        return path

    def write_query_pair(self, system_name: str, pair: QueryPair) -> dict[str, str]:
        base = f"{system_name}/{pair.case_id}"
        original = self.write_text(f"{base}/query.cypher", pair.original_query + "\n")
        transformed = self.write_text(f"{base}/query_transformed.cypher", pair.transformed_query + "\n")
        parameters = self.write_json(f"{base}/parameters.json", pair.parameters)
        return {
            "query": str(original.relative_to(self.run_dir)),
            "query_transformed": str(transformed.relative_to(self.run_dir)),
            "parameters": str(parameters.relative_to(self.run_dir)),
        }

    def write_query_family(self, system_name: str, family: QueryFamily) -> dict[str, str]:
        base = f"{system_name}/{family.case_id}"
        seed = self.write_text(f"{base}/seed_query.cypher", family.seed_query + "\n")
        parameters = self.write_json(f"{base}/parameters.json", family.parameters)
        artifacts = {
            "seed_query": str(seed.relative_to(self.run_dir)),
            "parameters": str(parameters.relative_to(self.run_dir)),
        }
        for variant in family.variants:
            path = self.write_text(f"{base}/{variant.variant_id}.cypher", variant.query + "\n")
            artifacts[f"variant_{variant.variant_id}"] = str(path.relative_to(self.run_dir))
        return artifacts

    def write_symmetry_family(self, system_name: str, family: QueryFamily) -> dict[str, str]:
        return self.write_query_family(system_name, family)

    def write_case_record(self, record: CaseRecord) -> None:
        system_name = record.system.value
        base = f"{system_name}/{record.case_id}"
        self.write_json(f"{base}/case.json", record)
        self.append_jsonl("cases.jsonl", record)
