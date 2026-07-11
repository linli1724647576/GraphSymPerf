"""Public experiment command facade used by every external entry point."""

from __future__ import annotations

from pathlib import Path

from graph_sym_perf.experiment.doctor_v2 import run_doctor
from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle, LifecycleResult, RunStatus
from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.config import migrate_legacy_config_to_v2
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import FormalRuntimeFirstShardWorker


class ExperimentNotReady(RuntimeError):
    """Raised when a formal run is attempted without a passing doctor report."""


class ExperimentCommands:
    """Stable public boundary around planning and the durable run lifecycle."""

    def plan(self, *, config: str | Path, output: str | Path | None = None):
        plan = build_run_plan(load_experiment_spec(config))
        if output is not None:
            write_resolved_manifest(plan, output)
        return plan

    def doctor(self, *, config: str | Path, output_root: str | Path | None = None):
        options = {} if output_root is None else {"output_root": output_root}
        return run_doctor(config, **options)

    def run(self, *, config: str | Path, output_root: str | Path = "runs") -> LifecycleResult:
        spec = load_experiment_spec(config)
        report = self.doctor(config=config, output_root=output_root)
        if not report.ready:
            raise ExperimentNotReady("doctor is not ready; inspect the generated doctor report")
        return ExperimentLifecycle(output_root).run(spec, FormalRuntimeFirstShardWorker(spec))

    def resume(
        self,
        *,
        config: str | Path,
        output_root: str | Path,
        run_id: str,
    ) -> LifecycleResult:
        spec = load_experiment_spec(config)
        report = self.doctor(config=config, output_root=output_root)
        if not report.ready:
            raise ExperimentNotReady("doctor is not ready; resume cannot verify the original environment")
        return ExperimentLifecycle(output_root).resume(
            spec,
            FormalRuntimeFirstShardWorker(spec),
            run_id=run_id,
        )

    def status(self, *, output_root: str | Path, run_id: str) -> RunStatus:
        return ExperimentLifecycle(output_root).status(run_id)

    def migrate_config(
        self,
        *,
        source: str | Path,
        destination: str | Path,
        template: str | Path,
    ) -> Path:
        return migrate_legacy_config_to_v2(source, destination, template_path=template)


__all__ = ["ExperimentCommands", "ExperimentNotReady"]
