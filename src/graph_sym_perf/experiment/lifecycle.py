"""Formal run lifecycle around an immutable plan and isolated shard workers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from graph_sym_perf.experiment.plan_models import ExperimentSpec
from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.supervisor import RunSummary, ShardSupervisor


@dataclass(frozen=True)
class LifecycleResult:
    run_id: str
    run_dir: Path
    summary: RunSummary


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
        summary = ShardSupervisor(
            run_dir,
            retry_limit=int(spec.scheduling["shard_retry_limit"]),
            process_isolation=self.process_isolation,
            hard_timeout_seconds=self.hard_timeout_seconds,
        ).run(plan, worker)
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
        return LifecycleResult(identifier, run_dir, summary)
