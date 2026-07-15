"""Public experiment command facade used by every external entry point."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from graph_sym_perf.core import TargetSystem, to_jsonable
from graph_sym_perf.experiment.doctor_v2 import run_doctor, run_doctor_spec
from graph_sym_perf.experiment.campaign import CampaignLifecycle, campaign_identity
from graph_sym_perf.experiment.lifecycle import ExperimentLifecycle, LifecycleResult, RunStatus
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    FrozenMapping,
    ValidatedYieldExperimentSpec,
    freeze_value,
)
from graph_sym_perf.experiment.planner import build_rule_plan, build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.selection import load_pilot_selection
from graph_sym_perf.config import migrate_legacy_config_to_v2
from graph_sym_perf.config import environment_with_dotenv
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import FormalRuntimeFirstShardWorker
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


class ExperimentNotReady(RuntimeError):
    """Raised when a formal run is attempted without a passing doctor report."""


class GatePrerequisiteError(RuntimeError):
    """Raised when a larger live gate has no verified predecessor."""


class ExperimentCommands:
    """Stable public boundary around planning and the durable run lifecycle."""

    def plan(
        self,
        *,
        config: str | Path,
        output: str | Path | None = None,
        phase: str | CampaignPhase | None = None,
        selection: str | Path | None = None,
    ):
        spec = load_experiment_spec(config)
        if isinstance(spec, ValidatedYieldExperimentSpec):
            if phase is None:
                raise ValueError("schema-v3 planning requires --phase")
            parsed_phase = phase if isinstance(phase, CampaignPhase) else CampaignPhase(str(phase))
            selected_cells: tuple[tuple[str, int], ...] = ()
            if parsed_phase is CampaignPhase.P2:
                if selection is None:
                    raise ValueError("P2 planning requires --selection")
                pilot = load_pilot_selection(
                    selection,
                    expected_campaign_id=campaign_identity(spec),
                    expected_max_cells=spec.campaign.p2.max_rule_scale_cells,
                )
                if pilot.status != "selected" or not pilot.cells:
                    raise ValueError("P2 planning requires a successful non-empty selection")
                selected_cells = tuple((cell.rule_id, cell.graph_scale) for cell in pilot.cells)
            elif selection is not None:
                raise ValueError("--selection is only valid for P2")
            plan = build_rule_plan(
                spec,
                parsed_phase,
                selected_cells=selected_cells,
                environment=_formal_environment(config),
            )
        else:
            if phase is not None or selection is not None:
                raise ValueError("phase selection requires a schema-v3 config")
            plan = build_run_plan(spec, environment=_formal_environment(config))
        if output is not None:
            write_resolved_manifest(plan, output)
        return plan

    def doctor(self, *, config: str | Path, output_root: str | Path | None = None):
        options = {} if output_root is None else {"output_root": output_root}
        return run_doctor(config, environment=_formal_environment(config), **options)

    def run(
        self,
        *,
        config: str | Path,
        output_root: str | Path = "runs",
        phase: str | CampaignPhase | None = None,
        run_id: str | None = None,
        parent_run: str | None = None,
        selection: str | Path | None = None,
    ):
        spec = load_experiment_spec(config)
        environment = _formal_environment(config)
        report = run_doctor(config, environment=environment, output_root=output_root)
        if not report.ready:
            raise ExperimentNotReady("doctor is not ready; inspect the generated doctor report")
        if isinstance(spec, ValidatedYieldExperimentSpec):
            if phase is None or run_id is None:
                raise ValueError("schema-v3 run requires --phase and --run-id")
            parsed_phase = phase if isinstance(phase, CampaignPhase) else CampaignPhase(str(phase))
            return CampaignLifecycle(output_root, environment=environment).run(
                spec,
                FormalRuntimeFirstShardWorker(spec, environment=environment),
                phase=parsed_phase,
                run_id=run_id,
                parent_run=parent_run,
                selection=selection,
            )
        if any(value is not None for value in (phase, run_id, parent_run, selection)):
            raise ValueError("phase ancestry arguments require a schema-v3 config")
        return ExperimentLifecycle(output_root, environment=environment).run(
            spec,
            FormalRuntimeFirstShardWorker(spec, environment=environment),
        )

    def select(
        self,
        *,
        config: str | Path,
        campaign_root: str | Path,
        p1_run: str,
        output: str | Path,
    ):
        spec = load_experiment_spec(config)
        if not isinstance(spec, ValidatedYieldExperimentSpec):
            raise ValueError("pilot selection requires a schema-v3 config")
        maximum = spec.campaign.p2.max_rule_scale_cells
        if maximum is None:
            raise ValueError("campaign.p2.max_rule_scale_cells is required")
        selection = CampaignLifecycle(campaign_root).create_selection(
            p1_run=p1_run,
            output=output,
            max_cells=maximum,
            expected_campaign_id=campaign_identity(spec),
        )
        return selection

    def report(self, *, config: str | Path, campaign_root: str | Path) -> dict[str, object]:
        spec = load_experiment_spec(config)
        if not isinstance(spec, ValidatedYieldExperimentSpec):
            raise ValueError("campaign report requires a schema-v3 config")
        report = CampaignLifecycle(campaign_root).report()
        expected = campaign_identity(spec)
        for phase in report["phases"]:
            outcome = phase.get("outcome", {})
            if outcome.get("campaign_id") != expected:
                raise ValueError("campaign report contains mixed campaign identities")
        return report

    def resume(
        self,
        *,
        config: str | Path,
        output_root: str | Path,
        run_id: str,
    ) -> LifecycleResult:
        spec = load_experiment_spec(config)
        environment = _formal_environment(config)
        report = run_doctor(config, environment=environment, output_root=output_root)
        if not report.ready:
            raise ExperimentNotReady("doctor is not ready; resume cannot verify the original environment")
        return ExperimentLifecycle(output_root, environment=environment).resume(
            spec,
            FormalRuntimeFirstShardWorker(spec, environment=environment),
            run_id=run_id,
        )

    def status(self, *, output_root: str | Path, run_id: str) -> RunStatus:
        return ExperimentLifecycle(output_root).status(run_id)

    def gate(
        self,
        *,
        config: str | Path,
        target: str,
        accepted_pairs: int,
        output_root: str | Path = "runs/gates",
    ) -> LifecycleResult:
        environment = _formal_environment(config)
        spec = _gate_spec(load_experiment_spec(config), target, accepted_pairs)
        gate_plan = build_run_plan(spec, environment=environment)
        if accepted_pairs > 10 and not _previous_gate_complete(
            output_root,
            target=spec.targets[0],
            accepted_pairs={100: 10, 1000: 100}[accepted_pairs],
            expected_source_revision=to_jsonable(gate_plan.source_revision),
        ):
            raise GatePrerequisiteError(
                f"{target} {accepted_pairs}-pair gate requires a verified predecessor"
            )
        report = run_doctor_spec(spec, environment=environment, output_root=output_root)
        if not report.ready:
            raise ExperimentNotReady("gate doctor is not ready; no gate shard was started")
        return ExperimentLifecycle(output_root, environment=environment).run(
            spec,
            FormalRuntimeFirstShardWorker(spec, environment=environment),
        )

    def migrate_config(
        self,
        *,
        source: str | Path,
        destination: str | Path,
        template: str | Path,
    ) -> Path:
        return migrate_legacy_config_to_v2(source, destination, template_path=template)


def _formal_environment(config: str | Path) -> dict[str, str]:
    config_path = Path(config).resolve()
    return environment_with_dotenv(None, dotenv_path=config_path.parent.parent / ".env")


def _gate_spec(spec, target: str, accepted_pairs: int):
    parsed = TargetSystem.parse(target)
    if parsed not in spec.targets:
        raise ValueError(f"target is not in the formal experiment: {target}")
    if accepted_pairs not in {10, 100, 1000}:
        raise ValueError("accepted_pairs must be one of 10, 100, or 1000")
    rules_catalog = to_jsonable(spec.rules_catalog)
    core_rules = RuleRegistry.default().rules_for(
        parsed.value,
        SafetyTier.CORE,
        frozenset(spec.systems[parsed].capabilities),
    )
    formal_floor = int(rules_catalog["tiers"]["core"]["minimum_accepted_pairs_per_rule"])
    gate_floor = min(formal_floor, max(1, accepted_pairs // len(core_rules)))
    rules_catalog["tiers"]["core"]["minimum_accepted_pairs_per_rule"] = gate_floor
    for rule in rules_catalog["rules"].values():
        if rule["tier"] == SafetyTier.CORE.value:
            rule["exploration_floor"] = gate_floor
    return replace(
        spec,
        name=f"{spec.name}_gate_{parsed.value}_{accepted_pairs}",
        targets=(parsed,),
        matrix=replace(spec.matrix, shards_per_system=1),
        workload=replace(
            spec.workload,
            accepted_query_pairs_per_shard=accepted_pairs,
            max_generated_seeds_per_shard=min(10_000, accepted_pairs * 10),
        ),
        systems=FrozenMapping({parsed: spec.systems[parsed]}),
        rules_catalog=freeze_value(rules_catalog),
    )


def _previous_gate_complete(
    output_root: str | Path,
    *,
    target: TargetSystem,
    accepted_pairs: int,
    expected_source_revision: Mapping[str, object],
) -> bool:
    root = Path(output_root)
    lifecycle = ExperimentLifecycle(root)
    for manifest_path in sorted(root.glob("*/resolved_manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tasks = manifest["tasks"]
            if (
                len(tasks) != 1
                or tasks[0]["target"] != target.value
                or tasks[0]["accepted_query_pairs"] != accepted_pairs
                or manifest.get("source_revision") != dict(expected_source_revision)
            ):
                continue
            status = lifecycle.status(manifest_path.parent.name)
            if status.completed == 1 and status.incomplete == status.failed == status.active == status.invalid == 0:
                return True
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return False


__all__ = [
    "ExperimentCommands",
    "ExperimentNotReady",
    "GatePrerequisiteError",
]
