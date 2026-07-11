"""Run artifact writer."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from graph_sym_perf.artifacts.policy import ArtifactLevel, ArtifactPolicy
from graph_sym_perf.artifacts.schema import ArtifactReference
from graph_sym_perf.artifacts.store import ContentAddressedStore
from graph_sym_perf.core import CaseRecord, QueryFamily, QueryPair, RunConfig, stable_json_dumps, to_jsonable


def iter_jsonl_records(path: str | Path) -> Iterator[dict[str, object]]:
    """Yield append-only records one at a time without loading a shard into memory."""

    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL record at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record must be an object at {path}:{line_number}")
            yield value


class ArtifactWriter:
    def __init__(self, config: RunConfig, run_id: str, *, shard_budget_mb: int = 500) -> None:
        self.run_id = run_id
        self.run_dir = config.paths.artifact_path(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.policy = ArtifactPolicy(shard_budget_mb=shard_budget_mb)
        self.content_store = ContentAddressedStore(
            self.run_dir / "content",
            budget_bytes=self.policy.shard_budget_bytes,
        )

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

    def write_content_addressed(
        self,
        name: str,
        payload: Any,
        *,
        media_type: str = "application/json",
    ) -> ArtifactReference:
        encoded = stable_json_dumps(payload).encode("utf-8")
        return self.content_store.put(name, encoded, media_type=media_type)

    def write_nonessential_diagnostic(self, name: str, payload: Any) -> ArtifactReference | None:
        encoded = stable_json_dumps(payload).encode("utf-8")
        result = self.content_store.put_with_policy(
            name,
            encoded,
            level=ArtifactLevel.MINIMAL,
            essential=False,
            media_type="application/json",
        )
        if result.warning is not None:
            self.append_jsonl("artifact_warnings.jsonl", {"warning": result.warning, "name": name})
        return result.reference

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
        _validate_case_artifact_paths(record.artifacts)
        explanation: dict[str, object] = {}
        for name in ("oracle", "confirmation", "exploratory_oracle", "exploratory_confirmation"):
            value = getattr(record, name)
            if value is not None:
                explanation[name] = to_jsonable(value)
        explanation_ref = self.write_content_addressed(
            f"{record.system.value}-{record.case_id}-explanation",
            explanation,
        ) if explanation else None
        summary: dict[str, object] = {
            "system": record.system.value,
            "case_id": record.case_id,
            "state": record.state,
            "reason": record.reason,
            "developer_status": getattr(record.developer_status, "value", record.developer_status),
            "claim_level": getattr(record.claim_level, "value", record.claim_level),
            "artifacts": dict(record.artifacts),
        }
        if explanation_ref is not None:
            summary["explanation_ref"] = to_jsonable(explanation_ref)
        self.write_json(f"{record.system.value}/{record.case_id}/case.json", summary)
        self.append_jsonl("cases.jsonl", summary)


def _validate_case_artifact_paths(artifacts: Mapping[str, str]) -> None:
    for name, path in artifacts.items():
        if not isinstance(name, str) or not isinstance(path, str) or not name or not path:
            raise ValueError("case artifacts must contain non-empty named relative paths")
        lowered = name.lower()
        if "plan" in lowered or "profile" in lowered or "pmc" in lowered:
            raise ValueError("cases.jsonl cannot embed explanation-layer artifact payloads")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError("case artifact path must stay relative to the run directory")


__all__ = ["ArtifactWriter", "iter_jsonl_records"]
