"""Formal run lifecycle around an immutable plan and isolated shard workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from graph_sym_perf.core import GdsmithConfig
from graph_sym_perf.experiment.evidence_validation import (
    canonical_hash,
    read_json_object,
    validate_failure_evidence,
    validate_shard_evidence,
)
from graph_sym_perf.experiment.plan_models import ExperimentSpec, RunPlan
from graph_sym_perf.experiment.planner import (
    _git_dirty,
    _git_output,
    build_run_plan,
    resolved_manifest_payload,
    write_resolved_manifest,
)
from graph_sym_perf.experiment.state import ManifestMismatchError
from graph_sym_perf.experiment.supervisor import RunSummary, ShardSupervisor
from graph_sym_perf.gdsmith.runner import check_gdsmith_tooling


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
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.process_isolation = process_isolation
        self.hard_timeout_seconds = hard_timeout_seconds
        self.environment = None if environment is None else dict(environment)

    def run(self, spec: ExperimentSpec, worker: Callable[[object], dict], *, run_id: str | None = None) -> LifecycleResult:
        identifier = run_id or f"{spec.name}-{uuid4().hex[:12]}"
        run_dir = self._run_dir(identifier)
        plan = build_run_plan(spec, environment=self.environment)
        if run_dir.exists():
            raise FileExistsError(f"run id already exists: {identifier}")
        run_dir.mkdir(parents=True)
        write_resolved_manifest(plan, run_dir / "resolved_manifest.json")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec, plan),
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
        run_dir = self._run_dir(run_id)
        if not (run_dir / "resolved_manifest.json").is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        plan = build_run_plan(spec, environment=self.environment)
        stored_manifest = json.loads((run_dir / "resolved_manifest.json").read_text(encoding="utf-8"))
        if stored_manifest != resolved_manifest_payload(plan):
            raise ManifestMismatchError("run resume refused: immutable manifest does not match current plan")
        bind_run_dir = getattr(worker, "bind_run_dir", None)
        if callable(bind_run_dir):
            bind_run_dir(run_dir)
        summary = ShardSupervisor(
            run_dir,
            **self._supervisor_options(spec, plan),
        ).resume(plan, worker)
        self._write_summary(run_id, run_dir, summary)
        return LifecycleResult(run_id, run_dir, summary)

    def status(self, run_id: str) -> RunStatus:
        run_dir = self._run_dir(run_id)
        manifest_path = run_dir / "resolved_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"run has no immutable manifest: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
            raise ValueError("resolved manifest must contain an exact task list")
        tasks = manifest["tasks"]
        total = len(tasks)
        manifest_hash = canonical_hash(manifest)
        counts = {"completed": 0, "incomplete": 0, "failed_final": 0, "active": 0, "invalid": 0}
        expected_dirs: set[Path] = set()
        for task in tasks:
            if not isinstance(task, dict):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            try:
                shard_dir = run_dir / "shards" / _manifest_shard_name(task)
                task_identity = canonical_hash(task)
            except (KeyError, TypeError, ValueError):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            if shard_dir in expected_dirs:
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            expected_dirs.add(shard_dir)
            checkpoint_path = shard_dir / "checkpoint.json"
            try:
                failure = validate_failure_evidence(
                    shard_dir / "failure.json",
                    manifest_hash=manifest_hash,
                    task_identity=task_identity,
                )
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            if not checkpoint_path.is_file():
                if failure is not None:
                    counts["failed_final"] += 1
                    counts["invalid"] += 1
                else:
                    counts["active"] += 1
                continue
            try:
                checkpoint = read_json_object(checkpoint_path)
                state = validate_shard_evidence(
                    shard_dir,
                    checkpoint,
                    manifest_hash=manifest_hash,
                    task_identity=task_identity,
                    accepted_target=_strict_int(task["accepted_query_pairs"]),
                )
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                counts["failed_final"] += 1
                counts["invalid"] += 1
                continue
            if failure is not None:
                counts["failed_final"] += 1
                counts["invalid"] += 1
            elif state.value in {"completed", "incomplete", "failed_final"}:
                counts[state.value] += 1
            elif state.value in {
                "planned", "generating", "loading", "building_accepted_pairs", "screening",
                "validating", "reducing", "failed_retryable",
            }:
                counts["active"] += 1
            else:
                counts["failed_final"] += 1
                counts["invalid"] += 1
        for extra in (run_dir / "shards").glob("*"):
            if extra.is_dir() and extra not in expected_dirs and (extra / "checkpoint.json").exists():
                counts["invalid"] += 1
        return RunStatus(
            run_id=run_id,
            total=total,
            completed=counts["completed"],
            incomplete=counts["incomplete"],
            failed=counts["failed_final"],
            active=counts["active"],
            invalid=counts["invalid"],
        )

    def _supervisor_options(self, spec: ExperimentSpec, plan: RunPlan) -> dict[str, object]:
        return {
            "retry_limit": int(spec.scheduling["shard_retry_limit"]),
            "process_isolation": self.process_isolation,
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "heartbeat_seconds": float(spec.scheduling["heartbeat_seconds"]),
            "stale_after_seconds": float(spec.scheduling["stale_after_seconds"]),
            "max_parallel_generation_systems": int(spec.scheduling["max_parallel_generation_systems"]),
            "max_parallel_measurement_systems": int(spec.scheduling["max_parallel_measurement_systems"]),
            "max_parallel_shards_per_system": int(spec.scheduling["max_parallel_shards_per_system"]),
            "shard_budget_bytes": int(spec.artifacts["shard_budget_mb"]) * 1024 * 1024,
            "attempt_guard": self._source_provenance_guard(spec, plan),
        }

    def _source_provenance_guard(self, spec: ExperimentSpec, plan: RunPlan) -> Callable[[], None]:
        return _SourceProvenanceGuard(spec, plan, environment=self.environment)

    def _run_dir(self, run_id: str) -> Path:
        if (
            not isinstance(run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is None
            or run_id in {".", ".."}
        ):
            raise ValueError("run id must be a safe basename")
        root = self.output_root.resolve()
        candidate = (root / run_id).resolve()
        if candidate.parent != root:
            raise ValueError("run id escapes the output root")
        return self.output_root / run_id

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


class _SourceProvenanceGuard:
    def __init__(
        self,
        spec: ExperimentSpec,
        plan: RunPlan,
        *,
        environment: Mapping[str, str] | None,
    ) -> None:
        self.spec = spec
        self.expected = plan.source_revision
        if plan.schema_version == 3 and not self.expected.provenance_complete:
            raise ManifestMismatchError("source provenance is incomplete")
        self.repository_root = spec.source_path.parents[1]
        self.environment = os.environ if environment is None else environment
        self.artifact_signature = self._artifact_signature()

    def __call__(self) -> None:
        signature = self._artifact_signature()
        if signature != self.artifact_signature:
            tooling = check_gdsmith_tooling(
                GdsmithConfig(expected_source_commit=self.spec.generation.expected_commit),
                self.environment,
            )
            if (
                tooling.jar_sha256 != self.expected.gdsmith_jar_sha256
                or tooling.build_manifest_sha256 != self.expected.gdsmith_build_manifest_sha256
            ):
                raise ManifestMismatchError("source provenance changed during run")
            self.artifact_signature = signature
        try:
            rules_sha256 = hashlib.sha256(self.spec.rules_source_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ManifestMismatchError("source provenance changed during run") from exc
        current = (
            _git_output(self.repository_root, "rev-parse", "HEAD"),
            _git_dirty(self.repository_root),
            _git_output(self.spec.generation.checkout, "rev-parse", "HEAD"),
            _git_dirty(self.spec.generation.checkout),
            rules_sha256,
        )
        expected = (
            self.expected.repository_commit,
            self.expected.repository_dirty,
            self.expected.gdsmith_commit,
            self.expected.gdsmith_dirty,
            self.expected.rules_schema_sha256,
        )
        if current != expected:
            raise ManifestMismatchError("source provenance changed during run")

    def _artifact_signature(self) -> tuple[object, ...]:
        config = GdsmithConfig(expected_source_commit=self.spec.generation.expected_commit)
        jar_value = self.environment.get(config.jar_env)
        jar = Path(jar_value).expanduser() if jar_value else None
        manifest = jar.with_suffix(jar.suffix + ".manifest.json") if jar is not None else None
        return (
            jar_value,
            self.environment.get(config.jar_sha256_env),
            _path_identity(jar),
            _path_identity(manifest),
        )


def _path_identity(path: Path | None) -> tuple[int, int, int, int, int] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


__all__ = ["ExperimentLifecycle", "LifecycleResult", "RunStatus"]


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected an integer")
    return value


def _manifest_shard_name(task: dict[str, object]) -> str:
    target = task["target"]
    shard_index = _strict_int(task["shard_index"])
    if not isinstance(target, str) or not target:
        raise TypeError("manifest task target must be a non-empty string")
    safe_target = re.sub(r"[^a-zA-Z0-9_.-]+", "-", target).strip("-")
    if not safe_target:
        raise ValueError("manifest task target has no safe shard name")
    return f"{safe_target}-{shard_index:03d}"
