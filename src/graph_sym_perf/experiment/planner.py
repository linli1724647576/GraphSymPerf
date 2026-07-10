"""Deterministic, DBMS-free planning for schema-v2 experiments."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from graph_sym_perf.experiment.plan_models import ExperimentSpec, RunPlan, ShardTask, SourceRevision


def build_run_plan(spec: ExperimentSpec, *, repository_root: str | Path | None = None) -> RunPlan:
    root = Path(repository_root) if repository_root is not None else spec.source_path.parents[2]
    tasks = tuple(
        ShardTask(
            target=target,
            shard_index=shard_index,
            seed=_shard_seed(spec.seed, target.value, shard_index),
            accepted_query_pairs=spec.workload.accepted_query_pairs_per_shard,
            max_generated_seeds=spec.workload.max_generated_seeds_per_shard,
            max_runtime_seconds=spec.workload.max_shard_runtime_hours * 3600,
        )
        for target in spec.targets
        for shard_index in range(spec.matrix.shards_per_system)
    )
    return RunPlan(
        spec_name=spec.name,
        schema_version=spec.schema_version,
        tasks=tasks,
        total_accepted_pair_budget=spec.total_accepted_pair_budget,
        source_revision=SourceRevision(
            repository_commit=_git_output(root, "rev-parse", "HEAD"),
            gdsmith_commit=_git_output(spec.generation.checkout, "rev-parse", "HEAD"),
            gdsmith_dirty=_git_dirty(spec.generation.checkout),
        ),
    )


def write_resolved_manifest(plan: RunPlan, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(plan)
    payload["tasks"] = [
        {**task, "target": task["target"].value}
        for task in payload["tasks"]
    ]
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _shard_seed(experiment_seed: int, target: str, shard_index: int) -> int:
    digest = hashlib.sha256(f"{experiment_seed}:{target}:{shard_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _git_output(directory: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(directory), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else None


def _git_dirty(directory: Path) -> bool | None:
    output = _git_output(directory, "status", "--porcelain")
    if output is None:
        return None
    return bool(output)
