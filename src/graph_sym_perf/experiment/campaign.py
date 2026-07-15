"""Fail-closed schema-v3 campaign ancestry and exclusive measurement session."""

from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from graph_sym_perf.core import TargetSystem, to_jsonable
from graph_sym_perf.experiment.evidence_validation import canonical_hash, read_json_object
from graph_sym_perf.experiment.entry_paths import rule_plan_entry_directory_name
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    RulePlan,
    ValidatedYieldExperimentSpec,
)
from graph_sym_perf.experiment.planner import (
    build_rule_plan,
    resolved_manifest_hash,
    write_resolved_manifest,
)
from graph_sym_perf.experiment.selection import (
    AffectedVersionRegressionReport,
    CellSummary,
    PilotSelection,
    SelectionIntegrityError,
    evaluate_p0_gate,
    load_pilot_selection,
    select_p2_cells,
    write_pilot_selection,
)
from graph_sym_perf.experiment.supervisor import (
    RulePlanRunSummary,
    RulePlanSupervisor,
    load_cell_summary,
)
from graph_sym_perf.validation.regression import (
    RegressionManifest,
    load_regression_case,
    load_verified_regression_report,
    probe_current_regression_environment_identity,
    regression_campaign_identity,
    regression_validation_evidence_path,
)


CAMPAIGN_PROTOCOL_VERSION = "validated-yield-campaign-v1"
MEASUREMENT_SESSION_BUSY = "MEASUREMENT_SESSION_BUSY"


class PhaseAncestryError(ValueError):
    """A requested campaign phase lacks immutable successful ancestry."""


class MeasurementSessionBusy(RuntimeError):
    code = MEASUREMENT_SESSION_BUSY


class MeasurementSessionOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class CampaignLifecycleResult:
    campaign_id: str
    phase: CampaignPhase
    run_id: str
    run_dir: Path
    manifest_hash: str
    passed: bool
    summary: RulePlanRunSummary


@dataclass(frozen=True)
class _PhaseEvidence:
    manifest: Mapping[str, object]
    manifest_hash: str
    outcome: Mapping[str, object]
    outcome_hash: str


class CampaignLifecycle:
    """Coordinate phase identity and ancestry without duplicating worker logic."""

    def __init__(
        self,
        campaign_root: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
        regression_verifier: Callable[
            [ValidatedYieldExperimentSpec, Path, Mapping[str, str]],
            Mapping[str, object],
        ] | None = None,
        measurement_lock_root: str | Path | None = None,
        process_isolation: bool = True,
    ) -> None:
        self.campaign_root = Path(campaign_root)
        self.environment = dict(os.environ if environment is None else environment)
        self._regression_verifier = regression_verifier or _verify_regression_artifact
        configured_lock_root = self.environment.get("GRAPHSYMPERF_MEASUREMENT_LOCK_ROOT")
        self.measurement_lock_root = Path(
            measurement_lock_root
            or configured_lock_root
            or (Path(tempfile.gettempdir()) / "graphsymperf-measurement-locks")
        )
        self.process_isolation = process_isolation

    def run(
        self,
        spec: ValidatedYieldExperimentSpec,
        worker: Callable[[object], Mapping[str, object]],
        *,
        phase: CampaignPhase,
        run_id: str,
        parent_run: str | None = None,
        selection: str | Path | None = None,
        retry_limit: int = 0,
    ) -> CampaignLifecycleResult:
        if not isinstance(spec, ValidatedYieldExperimentSpec):
            raise TypeError("CampaignLifecycle requires a schema-v3 validated-yield spec")
        if not isinstance(phase, CampaignPhase):
            raise TypeError("phase must be a CampaignPhase")
        run_dir = self._run_dir(run_id)
        if run_dir.exists():
            raise FileExistsError(f"campaign run id already exists: {run_id}")
        campaign_id = campaign_identity(spec)
        config_hash = campaign_config_hash(spec)
        parent_ref = self._validate_ancestry(
            spec,
            phase,
            parent_run=parent_run,
            campaign_id=campaign_id,
            config_hash=config_hash,
        )
        pilot, selection_ref = self._selection_for_phase(
            spec,
            phase,
            selection,
            campaign_id=campaign_id,
            parent=parent_ref,
        )
        selected_cells = (
            tuple((cell.rule_id, cell.graph_scale) for cell in pilot.cells)
            if pilot is not None
            else ()
        )
        plan = build_rule_plan(
            spec,
            phase,
            selected_cells=selected_cells,
            environment=self.environment,
        )
        self._validate_plan_ancestry(plan, parent_ref)
        if phase is CampaignPhase.P2:
            assert parent_ref is not None
            parent_run_id = parent_ref.get("run_id")
            if not isinstance(parent_run_id, str):
                raise PhaseAncestryError("P2 parent run identity is invalid")
            parent_plan = read_json_object(self._run_dir(parent_run_id) / "rule_plan.json")
            p1_seeds = {
                entry.get("shard_seed")
                for entry in parent_plan.get("entries", [])
                if isinstance(entry, Mapping)
            }
            _validate_p2_plan(plan, pilot, p1_seeds=p1_seeds)

        lease = self.measurement_lock(spec).acquire()
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            write_resolved_manifest(plan, run_dir / "rule_plan.json")
            rule_plan_hash = resolved_manifest_hash(plan)
            manifest_body: dict[str, object] = {
                "campaign_id": campaign_id,
                "campaign_protocol_version": CAMPAIGN_PROTOCOL_VERSION,
                "config_hash": config_hash,
                "parent": parent_ref,
                "phase": phase.value,
                "rule_plan_hash": rule_plan_hash,
                "run_id": run_id,
                "selection": selection_ref,
            }
            manifest_hash = canonical_hash(manifest_body)
            _write_sealed_json(run_dir / "resolved_manifest.json", manifest_body)

            bind_regression_root = getattr(worker, "bind_regression_artifact_root", None)
            if callable(bind_regression_root):
                bind_regression_root(
                    self._regression_root_for_phase(
                        phase,
                        run_dir=run_dir,
                        parent=parent_ref,
                    )
                )
            summary = RulePlanSupervisor(
                run_dir,
                campaign_id=campaign_id,
                input_manifest_hash=manifest_hash,
                retry_limit=retry_limit,
                process_isolation=self.process_isolation,
                attempt_guard=_campaign_source_provenance_guard(
                    spec,
                    plan,
                    environment=self.environment,
                ),
            ).run(plan, worker)
            passed, gate_reasons, regression_artifact = self._phase_passed(
                spec,
                phase,
                plan,
                summary,
                run_id,
            )
            _ordered_summaries, cell_summary_root = self._ordered_cell_summaries(run_id)
        finally:
            lease.release()
        outcome_body = {
            "campaign_id": campaign_id,
            "cell_summary_root": cell_summary_root,
            "counts": {
                "completed": len(summary.completed),
                "infrastructure_failed": len(summary.infrastructure_failed),
                "quarantined": len(summary.quarantined),
                "resource_exhausted": len(summary.resource_exhausted),
                "underfilled": len(summary.underfilled),
            },
            "manifest_hash": manifest_hash,
            "passed": passed,
            "phase": phase.value,
            "reason_codes": list(gate_reasons),
            "rule_plan_hash": rule_plan_hash,
            "run_id": run_id,
        }
        if regression_artifact is not None:
            outcome_body["regression_artifact"] = dict(regression_artifact)
        _write_sealed_json(run_dir / "phase_outcome.json", outcome_body)
        return CampaignLifecycleResult(
            campaign_id=campaign_id,
            phase=phase,
            run_id=run_id,
            run_dir=run_dir,
            manifest_hash=manifest_hash,
            passed=passed,
            summary=summary,
        )

    def measurement_lock(
        self,
        spec: ValidatedYieldExperimentSpec,
    ) -> "MeasurementSessionLock":
        return MeasurementSessionLock(
            self.measurement_lock_root,
            target_identity=measurement_target_identity(spec, self.environment),
        )

    def cell_summaries(self, run_id: str) -> tuple[CellSummary, ...]:
        evidence = self._load_phase(run_id)
        summaries, root = self._ordered_cell_summaries(run_id)
        if root != evidence.outcome.get("cell_summary_root"):
            raise PhaseAncestryError("phase cell summary root drift")
        if any(summary.input_manifest_hash != evidence.manifest_hash for summary in summaries):
            raise PhaseAncestryError("cell summary manifest ancestry drift")
        return summaries

    def report(self) -> dict[str, object]:
        phases: list[dict[str, object]] = []
        if not self.campaign_root.is_dir():
            return {"campaign_protocol_version": CAMPAIGN_PROTOCOL_VERSION, "phases": phases}
        for manifest_path in sorted(self.campaign_root.glob("*/resolved_manifest.json")):
            evidence = self._load_phase(manifest_path.parent.name)
            phases.append(
                {
                    "manifest_hash": evidence.manifest_hash,
                    "outcome": dict(evidence.outcome),
                    "run_id": manifest_path.parent.name,
                }
            )
        return {"campaign_protocol_version": CAMPAIGN_PROTOCOL_VERSION, "phases": phases}

    def create_selection(
        self,
        *,
        p1_run: str,
        output: str | Path,
        max_cells: int = 6,
        expected_campaign_id: str | None = None,
    ) -> PilotSelection:
        evidence = self._load_phase(p1_run)
        if evidence.manifest.get("phase") != CampaignPhase.P1.value:
            raise PhaseAncestryError("pilot selection requires a P1 run")
        if evidence.outcome.get("passed") is not True:
            raise PhaseAncestryError("pilot selection requires a successful P1 run")
        campaign_id = evidence.manifest.get("campaign_id")
        if not isinstance(campaign_id, str):
            raise PhaseAncestryError("P1 campaign identity is invalid")
        if expected_campaign_id is not None and campaign_id != expected_campaign_id:
            raise PhaseAncestryError("P1 campaign identity does not match the requested config")
        cell_summary_root = evidence.outcome.get("cell_summary_root")
        if not _is_sha256(cell_summary_root):
            raise PhaseAncestryError("P1 cell summary root is invalid")
        selection = select_p2_cells(
            self.cell_summaries(p1_run),
            max_cells=max_cells,
            campaign_id=campaign_id,
            input_p1_manifest_hash=evidence.manifest_hash,
            input_p1_cell_summary_root=cell_summary_root,
        )
        write_pilot_selection(selection, output)
        return selection

    def _phase_passed(
        self,
        spec: ValidatedYieldExperimentSpec,
        phase: CampaignPhase,
        plan: RulePlan,
        summary: RulePlanRunSummary,
        run_id: str,
    ) -> tuple[bool, tuple[str, ...], Mapping[str, object] | None]:
        if summary.failed:
            return False, tuple(
                name
                for name, values in (
                    ("underfilled", summary.underfilled),
                    ("quarantined", summary.quarantined),
                    ("resource_exhausted", summary.resource_exhausted),
                    ("infrastructure_failed", summary.infrastructure_failed),
                )
                if values
            ), None
        if len(summary.completed) != len(plan.entries):
            return False, ("entry_count_mismatch",), None
        if phase is not CampaignPhase.P0:
            return True, (), None
        summaries = self.cell_summaries_without_outcome(run_id)
        core = tuple(cell for cell in summaries if cell.stream == "discovery")
        controls = tuple(cell for cell in summaries if cell.stream == "control")
        try:
            regression_artifact = self._verified_regression_reference(
                spec,
                self._run_dir(run_id),
            )
        except (OSError, TypeError, ValueError) as exc:
            return False, ("regression_artifact_invalid",), None
        gate = evaluate_p0_gate(
            core_cells=core,
            control_cells=controls,
            regression=AffectedVersionRegressionReport(
                passed=regression_artifact.get("passed") is True,
                artifact_complete=regression_artifact.get("artifact_complete") is True,
            ),
        )
        return gate.passed, gate.reason_codes, regression_artifact

    def cell_summaries_without_outcome(self, run_id: str) -> tuple[CellSummary, ...]:
        run_dir = self._run_dir(run_id)
        manifest = _load_sealed_json(run_dir / "resolved_manifest.json", "phase manifest")
        manifest_hash = canonical_hash(manifest)
        summaries, _root = self._ordered_cell_summaries(run_id)
        if any(summary.input_manifest_hash != manifest_hash for summary in summaries):
            raise PhaseAncestryError("cell summary manifest ancestry drift")
        return summaries

    def _selection_for_phase(
        self,
        spec: ValidatedYieldExperimentSpec,
        phase: CampaignPhase,
        selection_path: str | Path | None,
        *,
        campaign_id: str,
        parent: Mapping[str, object] | None,
    ) -> tuple[PilotSelection | None, Mapping[str, object] | None]:
        if phase is not CampaignPhase.P2:
            if selection_path is not None:
                raise PhaseAncestryError("selection is only valid for P2")
            return None, None
        if selection_path is None or parent is None:
            raise PhaseAncestryError("P2 requires an immutable pilot selection")
        parent_manifest_hash = parent.get("manifest_hash")
        if not isinstance(parent_manifest_hash, str):
            raise PhaseAncestryError("P2 parent manifest hash is missing")
        parent_cell_summary_root = parent.get("cell_summary_root")
        if not _is_sha256(parent_cell_summary_root):
            raise PhaseAncestryError("P2 parent cell summary root is missing")
        preregistered_max_cells = spec.campaign.p2.max_rule_scale_cells
        if preregistered_max_cells is None:
            raise PhaseAncestryError(
                "P2 preregistered max_rule_scale_cells is missing"
            )
        try:
            pilot = load_pilot_selection(
                selection_path,
                expected_campaign_id=campaign_id,
                expected_p1_manifest_hash=parent_manifest_hash,
                expected_p1_cell_summary_root=parent_cell_summary_root,
                expected_max_cells=preregistered_max_cells,
            )
        except SelectionIntegrityError as exc:
            raise PhaseAncestryError(str(exc)) from exc
        parent_run = parent.get("run_id")
        if not isinstance(parent_run, str):
            raise PhaseAncestryError("P2 parent run identity is missing")
        expected = select_p2_cells(
            self.cell_summaries(parent_run),
            max_cells=preregistered_max_cells,
            campaign_id=campaign_id,
            input_p1_manifest_hash=parent_manifest_hash,
            input_p1_cell_summary_root=parent_cell_summary_root,
        )
        if pilot != expected:
            raise PhaseAncestryError(
                "pilot selection does not match parent P1 RulePlan-ordered cell summaries"
            )
        if pilot.status != "selected" or not pilot.cells:
            raise PhaseAncestryError("P2 requires a successful non-empty pilot selection")
        raw = read_json_object(Path(selection_path))
        content_hash = raw.get("content_hash")
        if not isinstance(content_hash, str):
            raise PhaseAncestryError("pilot selection content hash is missing")
        return pilot, {
            "content_hash": content_hash,
            "input_p1_manifest_hash": pilot.input_p1_manifest_hash,
            "input_p1_cell_summary_root": pilot.input_p1_cell_summary_root,
            "selector_protocol_version": pilot.selector_protocol_version,
        }

    def _validate_ancestry(
        self,
        spec: ValidatedYieldExperimentSpec,
        phase: CampaignPhase,
        *,
        parent_run: str | None,
        campaign_id: str,
        config_hash: str,
    ) -> Mapping[str, object] | None:
        if phase is CampaignPhase.P0:
            if parent_run is not None:
                raise PhaseAncestryError("P0 must not have a parent run")
            return None
        required = CampaignPhase.P0 if phase is CampaignPhase.P1 else CampaignPhase.P1
        if parent_run is None:
            raise PhaseAncestryError(f"{required.value.upper()} parent run is required")
        parent = self._load_phase(parent_run)
        if parent.manifest.get("phase") != required.value:
            raise PhaseAncestryError(f"{phase.value.upper()} requires a {required.value.upper()} parent")
        if parent.manifest.get("campaign_id") != campaign_id:
            raise PhaseAncestryError("parent campaign identity mismatch")
        if parent.manifest.get("config_hash") != config_hash:
            raise PhaseAncestryError("parent campaign configuration drift")
        if parent.outcome.get("passed") is not True:
            raise PhaseAncestryError("parent phase is not successful")
        reference: dict[str, object] = {
            "manifest_hash": parent.manifest_hash,
            "outcome_hash": parent.outcome_hash,
            "phase": required.value,
            "run_id": parent_run,
        }
        if required is CampaignPhase.P1:
            cell_summary_root = parent.outcome.get("cell_summary_root")
            if not _is_sha256(cell_summary_root):
                raise PhaseAncestryError("P1 parent cell summary root is invalid")
            reference["cell_summary_root"] = cell_summary_root
        if required is CampaignPhase.P0:
            regression_artifact = parent.outcome.get("regression_artifact")
            if not isinstance(regression_artifact, Mapping):
                raise PhaseAncestryError("P0 regression artifact reference is missing")
            self._verified_regression_reference(
                spec,
                self._run_dir(parent_run),
                expected=regression_artifact,
            )
            reference["regression_artifact"] = dict(regression_artifact)
        if phase is CampaignPhase.P2:
            grandparent_ref = parent.manifest.get("parent")
            if not isinstance(grandparent_ref, Mapping) or grandparent_ref.get("phase") != "p0":
                raise PhaseAncestryError("P2 parent lacks successful P0 ancestry")
            grandparent_run = grandparent_ref.get("run_id")
            if not isinstance(grandparent_run, str):
                raise PhaseAncestryError("P2 P0 ancestry run id is invalid")
            grandparent = self._load_phase(grandparent_run)
            if (
                grandparent.manifest_hash != grandparent_ref.get("manifest_hash")
                or grandparent.outcome_hash != grandparent_ref.get("outcome_hash")
                or grandparent.outcome.get("passed") is not True
                or grandparent.manifest.get("campaign_id") != campaign_id
                or grandparent.manifest.get("config_hash") != config_hash
            ):
                raise PhaseAncestryError("P2 P0 ancestry drift")
            regression_artifact = grandparent_ref.get("regression_artifact")
            if not isinstance(regression_artifact, Mapping):
                raise PhaseAncestryError("P2 P0 regression artifact reference is missing")
            self._verified_regression_reference(
                spec,
                self._run_dir(grandparent_run),
                expected=regression_artifact,
            )
            reference["p0_ancestor"] = dict(grandparent_ref)
        return reference

    def _verified_regression_reference(
        self,
        spec: ValidatedYieldExperimentSpec,
        run_dir: Path,
        *,
        expected: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        reference = dict(self._regression_verifier(spec, run_dir, self.environment))
        required_hashes = (
            "campaign_identity_hash",
            "environment_identity_hash",
            "report_content_hash",
            "report_sha256",
            "validation_evidence_sha256",
        )
        if (
            reference.get("passed") is not True
            or reference.get("artifact_complete") is not True
            or not isinstance(reference.get("case_id"), str)
            or not isinstance(reference.get("report_path"), str)
            or any(
                not isinstance(reference.get(name), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(reference.get(name))) is None
                for name in required_hashes
            )
        ):
            raise PhaseAncestryError("regression artifact reference is incomplete")
        if expected is not None and reference != dict(expected):
            raise PhaseAncestryError("regression artifact ancestry drift")
        return reference

    def _regression_root_for_phase(
        self,
        phase: CampaignPhase,
        *,
        run_dir: Path,
        parent: Mapping[str, object] | None,
    ) -> Path:
        if phase is CampaignPhase.P0:
            return run_dir
        if parent is None:
            raise PhaseAncestryError("non-P0 phase lacks regression ancestry")
        if phase is CampaignPhase.P1:
            run_id = parent.get("run_id")
        else:
            ancestor = parent.get("p0_ancestor")
            run_id = ancestor.get("run_id") if isinstance(ancestor, Mapping) else None
        if not isinstance(run_id, str):
            raise PhaseAncestryError("P0 regression artifact root identity is invalid")
        return self._run_dir(run_id)

    def _validate_plan_ancestry(
        self,
        plan: RulePlan,
        parent_ref: Mapping[str, object] | None,
    ) -> None:
        if not plan.source_revision.provenance_complete:
            raise PhaseAncestryError("RulePlan source revision is incomplete")
        if (
            plan.source_revision.repository_dirty is not False
            or plan.source_revision.gdsmith_dirty is not False
        ):
            raise PhaseAncestryError("RulePlan source revision must be clean")
        if parent_ref is None:
            return
        parent_run = parent_ref.get("run_id")
        if not isinstance(parent_run, str):
            raise PhaseAncestryError("parent run identity is invalid")
        parent_plan = read_json_object(self._run_dir(parent_run) / "rule_plan.json")
        if parent_plan.get("source_revision") != to_jsonable(plan.source_revision):
            raise PhaseAncestryError("parent RulePlan source revision drift")

    def _load_phase(self, run_id: str) -> _PhaseEvidence:
        run_dir = self._run_dir(run_id)
        try:
            manifest = _load_sealed_json(run_dir / "resolved_manifest.json", "phase manifest")
            outcome = _load_sealed_json(run_dir / "phase_outcome.json", "phase outcome")
            rule_plan = read_json_object(run_dir / "rule_plan.json")
        except (OSError, TypeError, ValueError) as exc:
            raise PhaseAncestryError(str(exc)) from exc
        manifest_hash = canonical_hash(manifest)
        outcome_hash = canonical_hash(outcome)
        if canonical_hash(rule_plan) != manifest.get("rule_plan_hash"):
            raise PhaseAncestryError("phase rule plan content hash mismatch")
        entries = rule_plan.get("entries")
        if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
            raise PhaseAncestryError("phase rule plan entries are invalid")
        source_revision = rule_plan.get("source_revision")
        if not isinstance(source_revision, Mapping) or not _source_revision_payload_complete(
            source_revision
        ):
            raise PhaseAncestryError("phase rule plan source revision is incomplete")
        if rule_plan.get("phase") != manifest.get("phase"):
            raise PhaseAncestryError("phase rule plan identity drift")
        if outcome.get("manifest_hash") != manifest_hash:
            raise PhaseAncestryError("phase outcome manifest ancestry drift")
        if (
            outcome.get("campaign_id") != manifest.get("campaign_id")
            or outcome.get("phase") != manifest.get("phase")
            or outcome.get("run_id") != manifest.get("run_id")
            or outcome.get("rule_plan_hash") != manifest.get("rule_plan_hash")
        ):
            raise PhaseAncestryError("phase outcome identity drift")
        _summaries, cell_summary_root = self._ordered_cell_summaries(run_id)
        if outcome.get("cell_summary_root") != cell_summary_root:
            raise PhaseAncestryError("phase cell summary root drift")
        return _PhaseEvidence(manifest, manifest_hash, outcome, outcome_hash)

    def _ordered_cell_summaries(
        self,
        run_id: str,
    ) -> tuple[tuple[CellSummary, ...], str]:
        run_dir = self._run_dir(run_id)
        rule_plan = read_json_object(run_dir / "rule_plan.json")
        entries = rule_plan.get("entries")
        if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
            raise PhaseAncestryError("phase RulePlan entries are invalid")
        summaries: list[CellSummary] = []
        bindings: list[dict[str, object]] = []
        expected_paths: set[Path] = set()
        for entry in entries:
            entry_id = entry.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                raise PhaseAncestryError("phase RulePlan entry identity is invalid")
            path = (
                run_dir
                / "entries"
                / rule_plan_entry_directory_name(entry_id)
                / "cell_summary.json"
            )
            expected_paths.add(path)
            payload = read_json_object(path)
            content_hash = payload.get("content_hash")
            if not isinstance(content_hash, str):
                raise PhaseAncestryError("cell summary content hash is missing")
            summary = load_cell_summary(path)
            summaries.append(summary)
            bindings.append(
                {
                    "cell_summary_content_hash": content_hash,
                    "entry_id": entry_id,
                    "relative_path": str(path.relative_to(run_dir)),
                }
            )
        actual_paths = set((run_dir / "entries").glob("*/cell_summary.json"))
        if actual_paths != expected_paths:
            raise PhaseAncestryError("phase cell summary set differs from RulePlan")
        return tuple(summaries), canonical_hash({"ordered_cell_summaries": bindings})

    def _run_dir(self, run_id: str) -> Path:
        if (
            not isinstance(run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is None
            or run_id in {".", ".."}
        ):
            raise ValueError("campaign run id must be a safe basename")
        root = self.campaign_root.resolve()
        candidate = (root / run_id).resolve()
        if candidate.parent != root:
            raise ValueError("campaign run id escapes the campaign root")
        return self.campaign_root / run_id


def _verify_regression_artifact(
    spec: ValidatedYieldExperimentSpec,
    run_dir: Path,
    environment: Mapping[str, str],
) -> Mapping[str, object]:
    manifest_path = spec.regression.get("manifest")
    if not isinstance(manifest_path, Path):
        raise ValueError("regression manifest is not configured")
    manifest = RegressionManifest.load(manifest_path)
    case = load_regression_case(manifest.path, environment)
    identity = probe_current_regression_environment_identity(case)
    regression_identity = regression_campaign_identity(
        spec_name=spec.name,
        schema_version=spec.schema_version,
        manifest_sha256=manifest.manifest_sha256,
    )
    report_path = run_dir / "regressions" / manifest.case_id / "regression_report.json"
    report = load_verified_regression_report(
        report_path,
        case=case,
        campaign_identity_hash=regression_identity,
        current_identity=identity,
    )
    raw = read_json_object(report_path)
    report_content_hash = raw.get("content_hash")
    evidence_path = regression_validation_evidence_path(report_path)
    if not isinstance(report_content_hash, str):
        raise ValueError("regression report content hash is missing")
    return {
        "artifact_complete": True,
        "campaign_identity_hash": regression_identity,
        "case_id": manifest.case_id,
        "environment_identity_hash": report.environment_identity_hash,
        "passed": True,
        "report_content_hash": report_content_hash,
        "report_path": str(report_path.relative_to(run_dir)),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "validation_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }


def measurement_target_identity(
    spec: ValidatedYieldExperimentSpec,
    environment: Mapping[str, str],
) -> str:
    if not isinstance(spec, ValidatedYieldExperimentSpec):
        raise TypeError("measurement target identity requires a schema-v3 spec")
    del environment
    definition = spec.systems.get(TargetSystem.NEO4J)
    target_id = definition.measurement_target_id if definition is not None else None
    if (
        not isinstance(target_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", target_id) is None
    ):
        raise PhaseAncestryError(
            "schema-v3 Neo4j measurement_target_id is missing or invalid"
        )
    return canonical_hash(
        {
            "measurement_target_id": target_id,
            "protocol": "neo4j-exclusive-server-identity-v4",
            "target": "neo4j",
        }
    )


def _canonical_measurement_host(host: str | None, port: int) -> Mapping[str, object]:
    if host is None:
        return {"unresolved_hostname": "<missing>"}
    normalized_host = host.strip().lower().rstrip(".")
    if not normalized_host:
        return {"unresolved_hostname": "<missing>"}
    if normalized_host == "localhost":
        return {"addresses": ["loopback"]}
    try:
        return {"addresses": [_canonical_ip_address(normalized_host)]}
    except ValueError:
        pass
    try:
        address_info = socket.getaddrinfo(
            normalized_host,
            port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror):
        return {"unresolved_hostname": normalized_host}
    addresses = {
        _canonical_ip_address(str(sockaddr[0]))
        for _family, _type, _proto, _canonname, sockaddr in address_info
        if isinstance(sockaddr, tuple) and sockaddr
    }
    if not addresses:
        return {"unresolved_hostname": normalized_host}
    if all(address == "loopback" for address in addresses):
        return {"addresses": ["loopback"]}
    return {"addresses": sorted(addresses)}


def _canonical_ip_address(raw: str) -> str:
    address = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "loopback"
    return address.compressed


def _campaign_source_provenance_guard(
    spec: ValidatedYieldExperimentSpec,
    plan: RulePlan,
    *,
    environment: Mapping[str, str],
) -> Callable[[], None]:
    # The schema-v2 and schema-v3 lifecycles deliberately share the exact same
    # before/after-attempt provenance verifier.
    from graph_sym_perf.experiment.lifecycle import _SourceProvenanceGuard

    return _SourceProvenanceGuard(spec, plan, environment=environment)  # type: ignore[arg-type]


def _source_revision_payload_complete(value: Mapping[str, object]) -> bool:
    string_fields = (
        "repository_commit",
        "gdsmith_commit",
        "rules_schema_sha256",
        "gdsmith_jar_sha256",
        "gdsmith_build_manifest_sha256",
    )
    return (
        all(isinstance(value.get(name), str) and bool(str(value.get(name))) for name in string_fields)
        and value.get("repository_dirty") is False
        and value.get("gdsmith_dirty") is False
    )


class MeasurementSessionLock:
    """One conservative, identity-bound Neo4j measurement owner per campaign."""

    def __init__(
        self,
        campaign_root: str | Path,
        *,
        target_identity: str | None = None,
    ) -> None:
        self.campaign_root = Path(campaign_root)
        if target_identity is not None and re.fullmatch(r"[0-9a-f]{64}", target_identity) is None:
            raise ValueError("target_identity must be a lowercase sha256")
        self.target_identity = target_identity or canonical_hash(
            {"legacy_lock_root": str(self.campaign_root.resolve())}
        )
        self.lock_dir = (
            self.campaign_root / f"neo4j-{self.target_identity}"
            if target_identity is not None
            else self.campaign_root / ".neo4j-measurement-session"
        )
        self.owner_path = self.lock_dir / "owner.json"

    def acquire(self) -> "MeasurementSessionLease":
        self.campaign_root.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            staging = self.campaign_root / f".{self.lock_dir.name}.pending-{uuid4().hex}"
            try:
                staging.mkdir(mode=0o700)
                token = uuid4().hex
                body = {
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "process_start_identity": _process_start_identity(os.getpid()),
                    "target_identity": self.target_identity,
                    "token": token,
                }
                sealed = {**body, "content_hash": canonical_hash(body)}
                raw = _create_owner_file(staging / "owner.json", sealed)
                try:
                    staging.rename(self.lock_dir)
                except OSError:
                    if not self.lock_dir.exists():
                        raise
                    if not self._recover_proven_stale_owner():
                        raise MeasurementSessionBusy(
                            f"{MEASUREMENT_SESSION_BUSY}: Neo4j measurement session already owned"
                        )
                    continue
                return MeasurementSessionLease(
                    self.lock_dir,
                    self.owner_path,
                    sealed,
                    raw,
                )
            finally:
                if staging.exists():
                    try:
                        (staging / "owner.json").unlink(missing_ok=True)
                        staging.rmdir()
                    except OSError:
                        pass
        raise MeasurementSessionBusy(f"{MEASUREMENT_SESSION_BUSY}: lock acquisition raced")

    def _recover_proven_stale_owner(self) -> bool:
        if self.lock_dir.is_symlink() or not self.lock_dir.is_dir():
            return False
        try:
            raw = self.owner_path.read_bytes()
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping):
            return False
        body = {key: value for key, value in payload.items() if key != "content_hash"}
        if payload.get("content_hash") != canonical_hash(body):
            return False
        pid = body.get("pid")
        process_start = body.get("process_start_identity")
        token = body.get("token")
        host = body.get("host")
        target_identity = body.get("target_identity")
        if (
            host != socket.gethostname()
            or target_identity != self.target_identity
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(process_start, str)
            or not process_start
            or not isinstance(token, str)
            or not token
        ):
            return False
        # A live PID is never stolen, even when its observable identity differs.
        if _pid_is_live(pid):
            return False
        try:
            if self.owner_path.read_bytes() != raw or tuple(self.lock_dir.iterdir()) != (self.owner_path,):
                return False
            self.owner_path.unlink()
            self.lock_dir.rmdir()
        except OSError:
            return False
        return True


class MeasurementSessionLease:
    def __init__(
        self,
        lock_dir: Path,
        owner_path: Path,
        owner: Mapping[str, object],
        raw: bytes,
    ) -> None:
        self.lock_dir = lock_dir
        self.owner_path = owner_path
        self.owner = dict(owner)
        self.raw = raw
        self._released = False

    def __enter__(self) -> "MeasurementSessionLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            raise MeasurementSessionOwnershipError("measurement session ownership already released")
        try:
            current = self.owner_path.read_bytes()
            payload = json.loads(current)
        except (OSError, json.JSONDecodeError) as exc:
            raise MeasurementSessionOwnershipError("measurement session ownership receipt is missing") from exc
        if current != self.raw or payload != self.owner:
            raise MeasurementSessionOwnershipError("measurement session ownership receipt changed")
        if (
            payload.get("pid") != os.getpid()
            or payload.get("process_start_identity") != _process_start_identity(os.getpid())
        ):
            raise MeasurementSessionOwnershipError("measurement session ownership identity mismatch")
        try:
            if tuple(self.lock_dir.iterdir()) != (self.owner_path,):
                raise MeasurementSessionOwnershipError("measurement session lock contains foreign evidence")
            self.owner_path.unlink()
            self.lock_dir.rmdir()
        except OSError as exc:
            raise MeasurementSessionOwnershipError("measurement session ownership release failed") from exc
        self._released = True


def campaign_config_hash(spec: ValidatedYieldExperimentSpec) -> str:
    return canonical_hash(to_jsonable(spec))


def campaign_identity(spec: ValidatedYieldExperimentSpec) -> str:
    return canonical_hash(
        {
            "config_hash": campaign_config_hash(spec),
            "name": spec.name,
            "schema_version": spec.schema_version,
        }
    )


def _validate_p2_plan(
    plan: RulePlan,
    selection: PilotSelection | None,
    *,
    p1_seeds: set[object],
) -> None:
    if selection is None:
        raise PhaseAncestryError("P2 plan lacks an immutable selection")
    expected = {(cell.rule_id, cell.graph_scale) for cell in selection.cells}
    actual = {(entry.rule_id, entry.graph_scale) for entry in plan.entries}
    if actual != expected:
        raise PhaseAncestryError("P2 RulePlan does not match the immutable selection")
    by_cell: dict[tuple[str, int], list[object]] = {}
    for entry in plan.entries:
        by_cell.setdefault((entry.rule_id, entry.graph_scale), []).append(entry)
        if entry.accepted_pair_quota != 1000:
            raise PhaseAncestryError("P2 RulePlan quota must be exactly 1000")
    if any(len(entries) != 3 for entries in by_cell.values()):
        raise PhaseAncestryError("P2 RulePlan must have exactly three shards per selected cell")
    seeds = [entry.shard_seed for entry in plan.entries]
    if len(seeds) != len(set(seeds)):
        raise PhaseAncestryError("P2 RulePlan seeds must be unique")
    if set(seeds) & p1_seeds:
        raise PhaseAncestryError("P2 RulePlan seeds overlap P1 ancestry")


def _write_sealed_json(path: Path, body: Mapping[str, object]) -> None:
    payload = {**body, "content_hash": canonical_hash(body)}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_sealed_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = read_json_object(path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} cannot be read: {exc}") from exc
    content_hash = payload.pop("content_hash", None)
    if content_hash != canonical_hash(payload):
        raise ValueError(f"{label} content hash mismatch")
    return payload


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _create_owner_file(path: Path, payload: Mapping[str, object]) -> bytes:
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw


def _process_start_identity(pid: int) -> str:
    try:
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return f"pid:{pid}:identity-unavailable"
    value = completed.stdout.strip()
    return value or f"pid:{pid}:identity-unavailable"


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = [
    "CAMPAIGN_PROTOCOL_VERSION",
    "CampaignLifecycle",
    "CampaignLifecycleResult",
    "MEASUREMENT_SESSION_BUSY",
    "MeasurementSessionBusy",
    "MeasurementSessionLease",
    "MeasurementSessionLock",
    "MeasurementSessionOwnershipError",
    "PhaseAncestryError",
    "campaign_config_hash",
    "campaign_identity",
]
