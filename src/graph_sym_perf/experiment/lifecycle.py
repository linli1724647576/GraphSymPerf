"""Formal run lifecycle around an immutable plan and isolated shard workers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from graph_sym_perf.experiment.plan_models import ExperimentSpec
from graph_sym_perf.experiment.planner import build_run_plan, resolved_manifest_payload, write_resolved_manifest
from graph_sym_perf.experiment.state import ManifestMismatchError
from graph_sym_perf.experiment.supervisor import RunSummary, ShardSupervisor


@dataclass(frozen=True)
class LifecycleResult:
    run_id: str
    run_dir: Path
    summary: RunSummary


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    total: int
    completed: int
    incomplete: int
    failed: int
    active: int
    invalid: int


class ExperimentLifecycle:
    def __init__(
        self,
        output_root: str | Path = "runs",
        *,
        process_isolation: bool = True,
        hard_timeout_seconds: float | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.process_isolation = process_isolation
        self.hard_timeout_seconds = hard_timeout_seconds

    def run(self, spec: ExperimentSpec, worker: Callable[[object], dict], *, run_id: str | None = None) -> LifecycleResult:
        plan = build_run_plan(spec)
        identifier = run_id or f"{spec.name}-{uuid4().hex[:12]}"
        run_dir = self.output_root / identifier
        if run_dir.exists():
            raise FileExistsError(f"run id already exists: {identifier}")
        run_dir.mkdir(parents=True)
        write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec),
        ).run(plan, worker)
        self._write_summary(identifier, run_dir, summary)
        return LifecycleResult(identifier, run_dir, summary)

    def resume(
        self,
        spec: ExperimentSpec,
        worker: Callable[[object], dict],
        *,
        run_id: str,
    ) -> LifecycleResult:
        run_dir = self.output_root / run_id
        if not (run_dir / "resolved_manifest.json").is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        plan = build_run_plan(spec)
        stored_manifest = json.loads((run_dir / "resolved_manifest.json").read_text(encoding="utf-8"))
        if stored_manifest != resolved_manifest_payload(plan):
            raise ManifestMismatchError("run resume refused: immutable manifest does not match current plan")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec),
        ).resume(plan, worker)
        self._write_summary(run_id, run_dir, summary)
        return LifecycleResult(run_id, run_dir, summary)

    def status(self, run_id: str) -> RunStatus:
        run_dir = self.output_root / run_id
        manifest_path = run_dir / "resolved_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tasks = manifest.get("tasks", []) if isinstance(manifest, dict) else []
        total = len(tasks) if isinstance(tasks, list) else 0
        counts = {"completed": 0, "incomplete": 0, "failed_final": 0, "active": 0, "invalid": 0}
        checkpoint_count = 0
        for checkpoint_path in (run_dir / "shards").glob("*/checkpoint.json"):
            checkpoint_count += 1
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                state = checkpoint["state"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                counts["invalid"] += 1
                continue
            if state in {"completed", "incomplete", "failed_final"}:
                counts[state] += 1
            elif state in {
                "planned",
                "generating",
                "loading",
                "building_accepted_pairs",
                "screening",
                "validating",
                "reducing",
                "failed_retryable",
            }:
                counts["active"] += 1
            else:
                counts["invalid"] += 1
        counts["active"] += max(0, total - checkpoint_count)
        return RunStatus(
            run_id=run_id,
            total=total,
            completed=counts["completed"],
            incomplete=counts["incomplete"],
            failed=counts["failed_final"],
            active=counts["active"],
            invalid=counts["invalid"],
        )

    def _supervisor_options(self, spec: ExperimentSpec) -> dict[str, object]:
        return {
            "retry_limit": int(spec.scheduling["shard_retry_limit"]),
            "process_isolation": self.process_isolation,
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "heartbeat_seconds": float(spec.scheduling["heartbeat_seconds"]),
            "stale_after_seconds": float(spec.scheduling["stale_after_seconds"]),
            "max_parallel_generation_systems": int(spec.scheduling["max_parallel_generation_systems"]),
            "max_parallel_measurement_systems": int(spec.scheduling["max_parallel_measurement_systems"]),
            "max_parallel_shards_per_system": int(spec.scheduling["max_parallel_shards_per_system"]),
        }

    @staticmethod
    def _write_summary(identifier: str, run_dir: Path, summary: RunSummary) -> None:
        (run_dir / "run_summary.json").write_text(
            json.dumps(
                {
                    "run_id": identifier,
                    "completed_count": len(summary.completed),
                    "incomplete_count": len(summary.incomplete),
                    "failed_count": len(summary.failed),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


__all__ = ["ExperimentLifecycle", "LifecycleResult", "RunStatus"]
