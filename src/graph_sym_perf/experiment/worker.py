"""One shard's runtime-first generation, acceptance, and screening lifecycle.

The worker deliberately owns only a single `(DBMS, shard)` execution context.
It does not use plan or PROFILE APIs, and it stores only minimal accepted-pair
records until a pair earns candidate retention through ordinary runtime screen.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.adapters.lifecycle import reset_graph_state
from graph_sym_perf.adapters.neo4j import read_neo4j_graph_manifest
from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import (
    ContentAddressedStore,
    enforce_shard_budget,
    load_artifact_record,
    verify_candidate_record_reference,
    verify_candidate_outcome_validation_chain,
)
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import (
    AdapterError,
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    QueryGenerationConfig,
    QueryExecutionError,
    QueryTimeoutError,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
    to_jsonable,
)
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.base import DecisionKind
from graph_sym_perf.dialects.native import NativeCypherDialect
from graph_sym_perf.equivalence.checker import compare_typed_results
from graph_sym_perf.experiment.plan_models import (
    CampaignPhase,
    ExperimentSpec,
    RulePlanEntry,
    RuleStream,
    ShardTask,
    ValidatedYieldExperimentSpec,
)
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.experiment.entry_paths import rule_plan_entry_directory_name
from graph_sym_perf.experiment.process_monitor import sanitize_terminal_error
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import (
    EntryState,
    ErrorKind,
    ShardState,
    _append_jsonl_fsync,
    _atomic_write_json,
    _fsync_directory,
)
from graph_sym_perf.generation.corpus import GraphCorpus, GraphManifest, PortableQuery, portable_ast_hash
from graph_sym_perf.generation.gdsmith import GdsmithGenerator, GenerationRequest
from graph_sym_perf.gdsmith.replay import gdsmith_read_query_is_replayable
from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port, run_gdsmith
from graph_sym_perf.runtime.measurement import RuntimeHarness, paired_schedule
from graph_sym_perf.runtime.models import (
    EquivalentQueryPair,
    NoiseCalibration,
    RuntimeEvidence,
    RuntimePolicy,
    RuntimeVerdict,
)
from graph_sym_perf.runtime.noise import (
    collect_aa_noise_samples,
    summarize_aa_noise_samples,
)
from graph_sym_perf.runtime.oracle import validate_stage2_evidence
from graph_sym_perf.runtime.records import (
    ArtifactIntegrityError,
    ArtifactWriteError,
    CompactRuntimeStore,
    PairRuntimeRecord,
    canonical_record_digest,
    summarize_integer_samples,
)
from graph_sym_perf.runtime.screening import (
    DEFAULT_SCREENING_POLICY,
    DurableTimeoutQueue,
    ScreeningPolicy,
    Stage2Candidate,
    Stage2Decision,
    TimeoutQueueRecord,
    evaluate_stage2,
    select_control_validation_pairs,
    stage1_top_k,
)
from graph_sym_perf.sanitization import sanitize_structure
from graph_sym_perf.reliability.findings import ReliabilityFindingStore
from graph_sym_perf.transforms.provider import CoreRulePairProvider
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import (
    SafetyCertificate as TransformSafetyCertificate,
    canonical_semantic_proof_hash,
)
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    IssueValidationReport,
    PipelineStage,
    ValidationPolicy,
    canonical_validation_report_hash,
)
from graph_sym_perf.validation.evidence import issue_validation_report_is_complete
from graph_sym_perf.validation.regression import (
    RegressionArtifactError,
    RegressionConfigurationError,
    RegressionManifest,
    build_production_regression_runner,
    load_regression_case,
    load_verified_regression_report,
    probe_current_regression_environment_identity,
    regression_campaign_identity,
    write_regression_configuration_failure,
    write_unresolved_regression_configuration_failure,
)
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner
from graph_sym_perf.validation.sealed_replay import (
    replay_issue_validation_report,
    runtime_evidence_from_payload,
)
from graph_sym_perf.validation.validator import IssueValidator
from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPairCorpus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
)
from graph_sym_perf.workload.rule_pairs import (
    RulePairFactory,
    certified_pair_hash,
    validate_certified_pair,
)
from graph_sym_perf.workload.registry import AcceptedCorpusRegistry


_MAX_QUERY_CANCELLATION_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class GeneratedShard:
    """Neutral graph and read-query corpus emitted by one GDsmith invocation."""

    graph_statements: tuple[str, ...]
    query_texts: tuple[str, ...]
    generator_metadata: Mapping[str, object]


class _AdapterFactory(Protocol):
    def __call__(self, task: ShardTask | RulePlanEntry) -> Any: ...


class _GenerationRunner(Protocol):
    def __call__(self, task: ShardTask | RulePlanEntry, raw_dir: Path) -> GeneratedShard: ...


class _CandidatePipeline(Protocol):
    def process(self, candidate: CandidateBundle, **context: object) -> Mapping[str, object]: ...

    def process_validated(
        self,
        candidate: CandidateBundle,
        validation: object,
        **context: object,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class _EntryValidationAdapterFactory:
    spec: ValidatedYieldExperimentSpec
    environment: Mapping[str, str]
    entry: RulePlanEntry
    adapter_creator: Callable[..., Any] = create_adapter

    def __call__(self, _candidate: CandidateBundle) -> Any:
        run_config = _entry_run_config(self.spec, self.entry)
        return self.adapter_creator(
            run_config.systems[0],
            run_config,
            self.environment,
            allow_create=True,
        )


class WorkerFailure(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class AdapterQueryProxy:
    """Expose an adapter while publishing each execute call's hard deadline."""

    def __init__(
        self,
        adapter: Any,
        *,
        attempt_dir: str | Path,
        graph_hash: str,
        dbms_fingerprint: str,
        default_timeout_seconds: float,
        on_error: Callable[[ErrorKind, Mapping[str, object]], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self.attempt_dir = Path(attempt_dir)
        self.graph_hash = graph_hash
        self._dbms_fingerprint = dbms_fingerprint
        self.default_timeout_seconds = default_timeout_seconds
        self.on_error = on_error

    def update_dbms_fingerprint(self, value: str) -> None:
        self._dbms_fingerprint = value

    def execute(
        self,
        query: str,
        parameters: Mapping[str, Any],
        query_kind: str,
        **kwargs: Any,
    ) -> Any:
        timeout = kwargs.get("timeout_seconds")
        if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
            timeout = self.default_timeout_seconds
            kwargs["timeout_seconds"] = timeout
        return self._call_with_inflight(
            query_identity=query,
            query_kind=query_kind,
            timeout_seconds=float(timeout),
            operation=lambda: self._adapter.execute(query, parameters, query_kind, **kwargs),
        )

    def ensure_schema(self) -> Any:
        operation = getattr(self._adapter, "ensure_schema", None)
        if not callable(operation):
            return None
        return self._call_with_inflight(
            query_identity="adapter.ensure_schema",
            query_kind="schema_setup",
            timeout_seconds=self.default_timeout_seconds,
            operation=operation,
        )

    def reset_schema(self) -> Any:
        operation = getattr(self._adapter, "reset_schema", None)
        if not callable(operation):
            return None
        return self._call_with_inflight(
            query_identity="adapter.reset_schema",
            query_kind="schema_reset",
            timeout_seconds=self.default_timeout_seconds,
            operation=operation,
        )

    def reset_graph(self, *, timeout_seconds: float | None = None) -> bool:
        operation = getattr(self._adapter, "reset_graph", None)
        if not callable(operation):
            return False
        effective_timeout = timeout_seconds or self.default_timeout_seconds
        self._call_with_inflight(
            query_identity="adapter.reset_graph",
            query_kind="graph_reset",
            timeout_seconds=effective_timeout,
            operation=lambda: operation(timeout_seconds=effective_timeout),
        )
        return True

    def connectivity_check(self) -> Any:
        operation = getattr(self._adapter, "connectivity_check", None)
        if not callable(operation):
            return False, "adapter connectivity contract missing"
        return self._call_with_inflight(
            query_identity="adapter.connectivity_check",
            query_kind="connectivity",
            timeout_seconds=self.default_timeout_seconds,
            operation=operation,
        )

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> Any:
        operation = getattr(self._adapter, "load_gdsmith_graph")
        identity = "\n".join(statement.strip() for statement in statements)
        return self._call_with_inflight(
            query_identity=identity,
            query_kind="graph_load",
            timeout_seconds=self.default_timeout_seconds,
            operation=lambda: operation(statements),
        )

    def load_graph(self, corpus: GraphCorpus) -> Any:
        operation = getattr(self._adapter, "load_graph", None)
        if callable(operation):
            loader = lambda: operation(corpus)
        else:
            legacy = getattr(self._adapter, "load_gdsmith_graph")
            loader = lambda: legacy(corpus.statements)
        return self._call_with_inflight(
            query_identity="\n".join(corpus.statements),
            query_kind="graph_load",
            timeout_seconds=self.default_timeout_seconds,
            operation=loader,
        )

    def dbms_health_epoch(self) -> DBMSHealthEpoch:
        operation = getattr(self._adapter, "dbms_health_epoch", None)
        if not callable(operation):
            return DBMSHealthEpoch(None, reason="adapter_health_epoch_contract_missing")
        return self._call_with_inflight(
            query_identity="adapter.dbms_health_epoch",
            query_kind="health_epoch",
            timeout_seconds=self.default_timeout_seconds,
            operation=operation,
        )

    def _call_with_inflight(
        self,
        *,
        query_identity: str,
        query_kind: str,
        timeout_seconds: float,
        operation: Callable[[], Any],
    ) -> Any:
        started_at = time.time()
        cancellation_grace = min(
            _MAX_QUERY_CANCELLATION_GRACE_SECONDS,
            max(0.1, timeout_seconds * 0.1),
        )
        inflight = {
            "cancellation_grace_seconds": cancellation_grace,
            "dbms_fingerprint": self._dbms_fingerprint,
            "deadline": started_at + timeout_seconds + cancellation_grace,
            "graph_hash": self.graph_hash,
            "operation_timeout_seconds": timeout_seconds,
            "query_hash": _query_hash(query_identity),
            "query_kind": query_kind,
            "started_at": started_at,
        }
        _atomic_write_json(self.attempt_dir / "inflight.json", inflight)
        try:
            return operation()
        except QueryTimeoutError:
            self._notify(ErrorKind.QUERY_TIMEOUT, inflight)
            raise
        except TimeoutError:
            self._notify(ErrorKind.DRIVER_TIMEOUT, inflight)
            raise
        finally:
            inflight_path = self.attempt_dir / "inflight.json"
            inflight_path.unlink(missing_ok=True)
            _fsync_directory(self.attempt_dir)

    def _notify(self, kind: ErrorKind, inflight: Mapping[str, object]) -> None:
        if self.on_error is not None:
            self.on_error(kind, inflight)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


class RuntimeFirstShardWorker:
    """Build accepted pairs and runtime-screen them with a fresh adapter.

    `GeneratedShard` is deliberately neutral: AGE and LadybugDB use an explicit
    compatible GDsmith proxy for generation, then replay the parsed graph into
    their own target adapter.  A query is counted only after both ordinary
    executions return complete, typed-equivalent results.
    """

    def __init__(
        self,
        spec: ExperimentSpec | ValidatedYieldExperimentSpec,
        *,
        environment: Mapping[str, str] | None = None,
        generation_runner: _GenerationRunner | None = None,
        adapter_factory: _AdapterFactory | None = None,
        candidate_pipeline: _CandidatePipeline | None = None,
        validation_runner_factory: Callable[[RulePlanEntry], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.environment = dict(os.environ if environment is None else environment)
        self._generation_runner = generation_runner or self._generate_with_gdsmith
        self._adapter_factory = adapter_factory or self._create_adapter
        self._candidate_pipeline = candidate_pipeline
        self._validation_runner_factory = validation_runner_factory
        self._clock = clock
        self._run_dir: Path | None = None
        self._regression_artifact_root: Path | None = None
        self._attempt_dir: Path | None = None
        self._attempt_context: AttemptContext | None = None

    def bind_run_dir(self, run_dir: str | Path) -> None:
        self._run_dir = Path(run_dir)

    def bind_regression_artifact_root(self, artifact_root: str | Path) -> None:
        """Bind the immutable cross-phase root that owns the P0 gate artifact.

        Task-11 ancestry resolution can call this seam for independent phase
        run directories; direct Task-9 use defaults to the bound run directory.
        """

        self._regression_artifact_root = Path(artifact_root)

    def bind_attempt_dir(self, attempt_dir: str | Path) -> None:
        self._attempt_dir = Path(attempt_dir)

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self._attempt_context = context
        attempt_dir = getattr(context, "attempt_dir", None)
        if attempt_dir is not None:
            self._attempt_dir = Path(attempt_dir)

    def __call__(self, task: ShardTask | RulePlanEntry) -> dict[str, object]:
        if isinstance(task, RulePlanEntry):
            return self._run_rule_plan_entry(task)
        if not isinstance(task, ShardTask):
            raise TypeError("worker task must be a RulePlanEntry or schema-v2 ShardTask")
        return self._run_schema_v2_compat(task)

    def _run_rule_plan_entry(self, entry: RulePlanEntry) -> dict[str, object]:
        """Execute the isolated schema-v3 acceptance lifecycle for one rule."""

        if not isinstance(self.spec, ValidatedYieldExperimentSpec):
            raise TypeError("RulePlanEntry requires a schema-v3 ValidatedYieldExperimentSpec")
        if self._run_dir is None:
            raise RuntimeError("entry worker was not bound to an immutable run directory")
        entry_dir = self._run_dir / "entries" / _entry_directory_name(entry.entry_id)
        entry_dir.mkdir(parents=True, exist_ok=True)
        _validate_existing_stage2_resume_policy(
            entry_dir / "stage2_progress.json",
            spec=self.spec,
            entry=entry,
        )
        attempt_dir = self._attempt_dir
        if attempt_dir is None:
            attempt_dir = _next_entry_attempt_dir(entry_dir)
            attempt_dir.mkdir(parents=True, exist_ok=False)
            self._attempt_dir = attempt_dir
        if entry.stream is RuleStream.REGRESSION:
            return self._run_regression_rule_plan_entry(entry, attempt_dir)
        if entry.phase in {CampaignPhase.P1, CampaignPhase.P2}:
            try:
                manifest = self._regression_manifest()
                campaign_identity_hash = self._regression_campaign_identity(manifest)
                case = load_regression_case(manifest.path, self.environment)
                current_identity = probe_current_regression_environment_identity(case)
                load_verified_regression_report(
                    self._regression_gate_path(manifest),
                    case=case,
                    campaign_identity_hash=campaign_identity_hash,
                    current_identity=current_identity,
                )
            except (RegressionConfigurationError, RegressionArtifactError) as exc:
                return self._finish_entry(
                    entry,
                    attempt_dir,
                    terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                    generated=None,
                    infrastructure_error_kind=ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    infrastructure_reason=str(exc),
                )
        progress_path = entry_dir / "acceptance_progress.json"
        progress = _load_entry_progress(progress_path, entry=entry)
        prior_elapsed = float(progress["elapsed_seconds"])
        progress_finish_fields: dict[str, object] = {
            "accepted_hashes": tuple(progress["accepted_pair_hashes"]),
            "applicability_count": len(progress["applicable_query_ordinals"]),
            "generated_seeds": int(progress["attempted_query_records"]),
            "rejects": dict(progress["reject_counts"]),
            "rule_stats": _rule_stats_from_entry_progress(entry, progress),
        }
        started = self._clock()
        invalidation = _load_entry_runtime_invalidation(entry_dir, entry=entry)
        if invalidation is not None:
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=None,
                infrastructure_error_kind=ErrorKind.INFRASTRUCTURE_ERROR,
                infrastructure_reason=str(invalidation["reason"]),
                **progress_finish_fields,
            )

        deadline = started + max(0.0, entry.max_entry_runtime_seconds - prior_elapsed)
        if prior_elapsed >= entry.max_entry_runtime_seconds or self._clock() >= deadline:
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.RESOURCE_EXHAUSTED,
                generated=None,
                **progress_finish_fields,
            )

        generation_lease = self._attempt_context.generation_lease() if self._attempt_context else nullcontext()
        try:
            with generation_lease:
                task_identity = canonical_hash(to_jsonable(entry))
                generation_path = entry_dir / "generation_corpus.json"
                generated = _load_generation_corpus(
                    generation_path,
                    task_identity=task_identity,
                    require_unique_query_asts=True,
                )
                if generated is None:
                    generated = self._generation_runner(entry, attempt_dir / "gdsmith_raw")
                    _seal_generation_corpus(
                        generation_path,
                        task_identity=task_identity,
                        generated=generated,
                        require_unique_query_asts=True,
                    )
        except WorkerFailure as exc:
            if _entry_infrastructure_error_kind(exc) is None:
                raise
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=started,
                now=self._clock(),
                last_query_ordinal=int(progress["last_query_ordinal"]),
                applicable_query_ordinals=tuple(progress["applicable_query_ordinals"]),
                rejects=dict(progress["reject_counts"]),
                accepted_hashes=tuple(progress["accepted_pair_hashes"]),
                pending_acceptances=tuple(progress["pending_acceptances"]),
                legacy_migration_sha256=progress["legacy_migration_sha256"],
                evidence_identity=progress["evidence_identity"],
                stage1_intent=progress["stage1_intent"],
                accepted_record_ledger=tuple(progress["accepted_record_ledger"]),
                noise_floor_id=progress["noise_floor_id"],
                noise_intent=progress["noise_intent"],
                noise_failure_id=progress["noise_failure_id"],
                noise_failure_intent=progress["noise_failure_intent"],
            )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=None,
                infrastructure_error_kind=exc.kind,
                infrastructure_reason=str(exc),
                **progress_finish_fields,
            )
        except (MemoryError, OSError, AdapterError) as exc:
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=started,
                now=self._clock(),
                last_query_ordinal=int(progress["last_query_ordinal"]),
                applicable_query_ordinals=tuple(progress["applicable_query_ordinals"]),
                rejects=dict(progress["reject_counts"]),
                accepted_hashes=tuple(progress["accepted_pair_hashes"]),
                pending_acceptances=tuple(progress["pending_acceptances"]),
                legacy_migration_sha256=progress["legacy_migration_sha256"],
                evidence_identity=progress["evidence_identity"],
                stage1_intent=progress["stage1_intent"],
                accepted_record_ledger=tuple(progress["accepted_record_ledger"]),
                noise_floor_id=progress["noise_floor_id"],
                noise_intent=progress["noise_intent"],
                noise_failure_id=progress["noise_failure_id"],
                noise_failure_intent=progress["noise_failure_intent"],
            )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=None,
                infrastructure_error_kind=(
                    _entry_infrastructure_error_kind(exc) or ErrorKind.INFRASTRUCTURE_ERROR
                ),
                infrastructure_reason=str(exc),
                **progress_finish_fields,
            )
        if self._clock() >= deadline:
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=started,
                now=self._clock(),
                last_query_ordinal=int(progress["last_query_ordinal"]),
                applicable_query_ordinals=tuple(progress["applicable_query_ordinals"]),
                rejects=dict(progress["reject_counts"]),
                accepted_hashes=tuple(progress["accepted_pair_hashes"]),
                pending_acceptances=tuple(progress["pending_acceptances"]),
                legacy_migration_sha256=progress["legacy_migration_sha256"],
                evidence_identity=progress["evidence_identity"],
                stage1_intent=progress["stage1_intent"],
                accepted_record_ledger=tuple(progress["accepted_record_ledger"]),
                noise_floor_id=progress["noise_floor_id"],
                noise_intent=progress["noise_intent"],
                noise_failure_id=progress["noise_failure_id"],
                noise_failure_intent=progress["noise_failure_intent"],
            )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.RESOURCE_EXHAUSTED,
                generated=generated,
                **progress_finish_fields,
            )

        graph_hash = _graph_snapshot_hash(generated.graph_statements)
        try:
            adapter = self._adapter_factory(entry)
        except WorkerFailure as exc:
            if _entry_infrastructure_error_kind(exc) is None:
                raise
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=started,
                now=self._clock(),
                last_query_ordinal=int(progress["last_query_ordinal"]),
                applicable_query_ordinals=tuple(progress["applicable_query_ordinals"]),
                rejects=dict(progress["reject_counts"]),
                accepted_hashes=tuple(progress["accepted_pair_hashes"]),
                pending_acceptances=tuple(progress["pending_acceptances"]),
                legacy_migration_sha256=progress["legacy_migration_sha256"],
                evidence_identity=progress["evidence_identity"],
                stage1_intent=progress["stage1_intent"],
                accepted_record_ledger=tuple(progress["accepted_record_ledger"]),
                noise_floor_id=progress["noise_floor_id"],
                noise_intent=progress["noise_intent"],
                noise_failure_id=progress["noise_failure_id"],
                noise_failure_intent=progress["noise_failure_intent"],
            )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=generated,
                infrastructure_error_kind=exc.kind,
                infrastructure_reason=str(exc),
                **progress_finish_fields,
            )
        except (MemoryError, OSError, AdapterError) as exc:
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=started,
                now=self._clock(),
                last_query_ordinal=int(progress["last_query_ordinal"]),
                applicable_query_ordinals=tuple(progress["applicable_query_ordinals"]),
                rejects=dict(progress["reject_counts"]),
                accepted_hashes=tuple(progress["accepted_pair_hashes"]),
                pending_acceptances=tuple(progress["pending_acceptances"]),
                legacy_migration_sha256=progress["legacy_migration_sha256"],
                evidence_identity=progress["evidence_identity"],
                stage1_intent=progress["stage1_intent"],
                accepted_record_ledger=tuple(progress["accepted_record_ledger"]),
                noise_floor_id=progress["noise_floor_id"],
                noise_intent=progress["noise_intent"],
                noise_failure_id=progress["noise_failure_id"],
                noise_failure_intent=progress["noise_failure_intent"],
            )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=generated,
                infrastructure_error_kind=(
                    _entry_infrastructure_error_kind(exc) or ErrorKind.INFRASTRUCTURE_ERROR
                ),
                infrastructure_reason=str(exc),
                **progress_finish_fields,
            )
        proxy = AdapterQueryProxy(
            adapter,
            attempt_dir=attempt_dir,
            graph_hash=graph_hash,
            dbms_fingerprint="unverified",
            default_timeout_seconds=float(entry.query_timeout_seconds),
            on_error=lambda kind, inflight: self._record_query_failure(kind, inflight, attempt_dir),
        )
        graph_manifest: GraphManifest | None = None
        fingerprint = DBMSFingerprint(None, "not_loaded")
        health_epoch = DBMSHealthEpoch(None, reason="not_loaded")
        outcome: dict[str, object] = {
            "accepted": (),
            **_entry_outcome_from_progress(entry, progress),
            "terminal_state": EntryState.INFRASTRUCTURE_FAILED,
        }
        measurement_lease = self._attempt_context.measurement_lease() if self._attempt_context else nullcontext()
        try:
            with measurement_lease:
                self._reset_and_load(proxy, generated.graph_statements)
                try:
                    requested_max_nodes = _requested_max_nodes(entry, generated)
                except (TypeError, ValueError) as exc:
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        f"requested graph scale is invalid: {exc}",
                    ) from exc
                if entry.target is TargetSystem.NEO4J:
                    try:
                        graph_manifest = read_neo4j_graph_manifest(
                            proxy,
                            requested_max_nodes=requested_max_nodes,
                            statements=generated.graph_statements,
                        )
                    except (TypeError, ValueError) as exc:
                        raise WorkerFailure(
                            ErrorKind.INFRASTRUCTURE_ERROR,
                            f"graph manifest read failed: {exc}",
                        ) from exc
                    if graph_manifest.graph_fingerprint != graph_hash:
                        raise WorkerFailure(
                            ErrorKind.INFRASTRUCTURE_ERROR,
                            "graph manifest fingerprint does not match sealed graph",
                        )
                    _atomic_write_json(attempt_dir / "graph_manifest.json", asdict(graph_manifest))
                fingerprint = _adapter_dbms_fingerprint(proxy)
                health_epoch = _adapter_dbms_health_epoch(proxy)
                if not fingerprint.available or not health_epoch.available:
                    raise WorkerFailure(
                        ErrorKind.DBMS_UNAVAILABLE,
                        fingerprint.reason or health_epoch.reason or "DBMS identity unavailable",
                    )
                assert fingerprint.value is not None
                assert health_epoch.value is not None
                proxy.update_dbms_fingerprint(fingerprint.value)
                outcome = self._build_rule_entry_pairs(
                    proxy,
                    entry,
                    generated.query_texts,
                    entry_dir,
                    graph_manifest=graph_manifest,
                    graph_statements=generated.graph_statements,
                    graph_snapshot_hash=(
                        graph_manifest.graph_fingerprint if graph_manifest is not None else graph_hash
                    ),
                    dbms_fingerprint=fingerprint.value,
                    dbms_version=fingerprint.server_version or "version-unavailable",
                    driver_version=fingerprint.driver_version or "driver-version-unavailable",
                    dbms_health_epoch=health_epoch.value,
                    deadline=deadline,
                    progress_path=progress_path,
                    progress=progress,
                    attempt_started=started,
                    prior_elapsed=prior_elapsed,
                )
                if outcome["terminal_state"] is not EntryState.INFRASTRUCTURE_FAILED:
                    final_identity_stable = False
                    final_fingerprint = _adapter_dbms_fingerprint(proxy)
                    final_epoch = _adapter_dbms_health_epoch(proxy)
                    if (
                        not final_fingerprint.available
                        or not final_epoch.available
                        or final_fingerprint.value != fingerprint.value
                        or final_epoch.value != health_epoch.value
                    ):
                        outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                        outcome["infrastructure_error_kind"] = ErrorKind.DBMS_CRASHED
                        outcome["infrastructure_reason"] = "DBMS fingerprint or health epoch changed"
                        baseline_graph_hash = (
                            graph_manifest.graph_fingerprint
                            if graph_manifest is not None
                            else graph_hash
                        )
                        ReliabilityFindingStore(self._run_dir).record_entry(
                            kind=ErrorKind.DBMS_CRASHED,
                            entry_id=entry.entry_id,
                            graph_hash=baseline_graph_hash,
                            dbms_fingerprint=fingerprint.value,
                            dbms_version=fingerprint.server_version,
                            observed_identity={
                                "reason": "DBMS fingerprint or health epoch changed",
                                "baseline": {
                                    "graph_snapshot_hash": baseline_graph_hash,
                                    "dbms_fingerprint": fingerprint.value,
                                    "dbms_health_epoch": health_epoch.value,
                                },
                                "observed": {
                                    "dbms_fingerprint": final_fingerprint.value,
                                    "dbms_health_epoch": final_epoch.value,
                                    "fingerprint_available": final_fingerprint.available,
                                    "health_epoch_available": final_epoch.available,
                                },
                            },
                            attempt_evidence=str(
                                attempt_dir.relative_to(self._run_dir)
                            ),
                        )
                        _write_entry_runtime_invalidation(
                            entry_dir,
                            entry=entry,
                            evidence_identity=_runtime_evidence_identity(
                                graph_snapshot_hash=baseline_graph_hash,
                                dbms_fingerprint=fingerprint.value,
                                dbms_health_epoch=health_epoch.value,
                            ),
                            reason="DBMS fingerprint or health epoch changed",
                            observed={
                                "dbms_fingerprint": final_fingerprint.value,
                                "dbms_health_epoch": final_epoch.value,
                            },
                        )
                    elif graph_manifest is not None:
                        try:
                            final_graph = read_neo4j_graph_manifest(
                                proxy,
                                requested_max_nodes=entry.graph_scale,
                                statements=generated.graph_statements,
                            )
                        except (TypeError, ValueError) as exc:
                            raise WorkerFailure(
                                ErrorKind.INFRASTRUCTURE_ERROR,
                                f"final graph manifest read failed: {exc}",
                            ) from exc
                        if final_graph != graph_manifest:
                            outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                            outcome["infrastructure_error_kind"] = ErrorKind.INFRASTRUCTURE_ERROR
                            outcome["infrastructure_reason"] = "observed graph manifest drifted during acceptance"
                            ReliabilityFindingStore(self._run_dir).record_entry(
                                kind=ErrorKind.INFRASTRUCTURE_ERROR,
                                entry_id=entry.entry_id,
                                graph_hash=graph_manifest.graph_fingerprint,
                                dbms_fingerprint=fingerprint.value,
                                dbms_version=fingerprint.server_version,
                                observed_identity={
                                    "reason": (
                                        "observed graph manifest drifted during acceptance"
                                    ),
                                    "baseline": {
                                        "graph_manifest": to_jsonable(
                                            asdict(graph_manifest)
                                        ),
                                        "dbms_fingerprint": fingerprint.value,
                                        "dbms_health_epoch": health_epoch.value,
                                    },
                                    "observed": {
                                        "graph_manifest": to_jsonable(
                                            asdict(final_graph)
                                        ),
                                        "dbms_fingerprint": final_fingerprint.value,
                                        "dbms_health_epoch": final_epoch.value,
                                    },
                                },
                                attempt_evidence=str(
                                    attempt_dir.relative_to(self._run_dir)
                                ),
                            )
                            _write_entry_runtime_invalidation(
                                entry_dir,
                                entry=entry,
                                evidence_identity=_runtime_evidence_identity(
                                    graph_snapshot_hash=(
                                        graph_manifest.graph_fingerprint
                                    ),
                                    dbms_fingerprint=fingerprint.value,
                                    dbms_health_epoch=health_epoch.value,
                                ),
                                reason=(
                                    "observed graph manifest drifted during acceptance"
                                ),
                                observed=to_jsonable(asdict(final_graph)),
                            )
                        else:
                            final_identity_stable = True
                    else:
                        final_identity_stable = True
                    if final_identity_stable:
                        try:
                            timeout_queue = DurableTimeoutQueue(
                                entry_dir / "timeout_queue.jsonl"
                            )
                            expected_timeout_records = tuple(
                                outcome.get("timeout_records", ())
                            )
                            for timeout_record in expected_timeout_records:
                                if not isinstance(timeout_record, TimeoutQueueRecord):
                                    raise ArtifactIntegrityError(
                                        "Stage-2 timeout event is not typed"
                                    )
                            timeout_queue.reconcile(expected_timeout_records)
                        except ArtifactIntegrityError as exc:
                            outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                            outcome["infrastructure_error_kind"] = (
                                ErrorKind.EVIDENCE_INTEGRITY_ERROR
                            )
                            outcome["infrastructure_reason"] = str(exc)
                        except ArtifactWriteError as exc:
                            outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                            outcome["infrastructure_error_kind"] = (
                                ErrorKind.INFRASTRUCTURE_ERROR
                            )
                            outcome["infrastructure_reason"] = str(exc)
        except WorkerFailure as exc:
            if _entry_infrastructure_error_kind(exc) is not None:
                latest_progress = _load_entry_progress(progress_path, entry=entry)
                outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                outcome["infrastructure_error_kind"] = exc.kind
                outcome["infrastructure_reason"] = str(exc)
                outcome.update(_entry_outcome_from_progress(entry, latest_progress))
            else:
                raise
        except (MemoryError, OSError, AdapterError) as exc:
            latest_progress = _load_entry_progress(progress_path, entry=entry)
            outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
            outcome["infrastructure_error_kind"] = (
                _entry_infrastructure_error_kind(exc) or ErrorKind.INFRASTRUCTURE_ERROR
            )
            outcome["infrastructure_reason"] = str(exc)
            outcome.update(_entry_outcome_from_progress(entry, latest_progress))
        finally:
            try:
                adapter.close()
            except (MemoryError, OSError, AdapterError) as exc:
                outcome["terminal_state"] = EntryState.INFRASTRUCTURE_FAILED
                outcome["infrastructure_error_kind"] = (
                    _entry_infrastructure_error_kind(exc) or ErrorKind.INFRASTRUCTURE_ERROR
                )
                outcome["infrastructure_reason"] = str(exc)

        outcome_generated_seeds = int(outcome["generated_seeds"])
        outcome_rejects = dict(outcome["rejects"])
        outcome_accepted_hashes = tuple(outcome["accepted_hashes"])
        _persist_entry_progress(
            progress_path,
            entry=entry,
            prior_elapsed=prior_elapsed,
            attempt_started=started,
            now=self._clock(),
            last_query_ordinal=outcome_generated_seeds,
            applicable_query_ordinals=tuple(outcome["applicable_query_ordinals"]),
            rejects=outcome_rejects,
            accepted_hashes=outcome_accepted_hashes,
            pending_acceptances=tuple(outcome.get("pending_acceptances", ())),
            legacy_migration_sha256=outcome.get("legacy_migration_sha256"),
            evidence_identity=outcome.get("evidence_identity", progress["evidence_identity"]),
            stage1_intent=outcome.get("stage1_intent", progress["stage1_intent"]),
            accepted_record_ledger=tuple(
                outcome.get("accepted_record_ledger", progress["accepted_record_ledger"])
            ),
            noise_floor_id=outcome.get("noise_floor_id", progress["noise_floor_id"]),
            noise_intent=outcome.get("noise_intent", progress["noise_intent"]),
            noise_failure_id=outcome.get(
                "noise_failure_id", progress["noise_failure_id"]
            ),
            noise_failure_intent=outcome.get(
                "noise_failure_intent", progress["noise_failure_intent"]
            ),
        )

        return self._finish_entry(
            entry,
            attempt_dir,
            terminal_state=outcome["terminal_state"],
            generated=generated,
            accepted_hashes=tuple(outcome["accepted_hashes"]),
            applicability_count=len(outcome["applicable_query_ordinals"]),
            generated_seeds=int(outcome["generated_seeds"]),
            rejects=dict(outcome["rejects"]),
            rule_stats=dict(outcome["rule_stats"]),
            graph_manifest=graph_manifest,
            dbms_fingerprint=fingerprint,
            health_epoch=health_epoch,
            stage1_selected_pairs=int(outcome.get("stage1_selected_pairs", 0)),
            stage2_measured_pairs=int(outcome.get("stage2_measured_pairs", 0)),
            stage2_promoted_pairs=int(outcome.get("stage2_promoted_pairs", 0)),
            validation_selected_pairs=int(
                outcome.get("validation_selected_pairs", 0)
            ),
            validation_passed_pairs=int(
                outcome.get("validation_passed_pairs", 0)
            ),
            pipeline_terminal_candidates=int(
                outcome.get("pipeline_terminal_candidates", 0)
            ),
            submit_ready_issues=int(outcome.get("submit_ready_issues", 0)),
            cell_metrics=dict(outcome.get("cell_metrics", {})),
            control_gate_hit=outcome.get("control_gate_hit", False) is True,
            control_validation_pass=(
                outcome.get("control_validation_pass", False) is True
            ),
            infrastructure_error_kind=outcome.get("infrastructure_error_kind"),
            infrastructure_reason=outcome.get("infrastructure_reason"),
        )

    def _regression_manifest(self) -> RegressionManifest:
        manifest_path = self.spec.regression.get("manifest")
        if not isinstance(manifest_path, Path):
            raise RegressionConfigurationError("regression manifest is not configured")
        return RegressionManifest.load(manifest_path)

    def _regression_campaign_identity(self, manifest: RegressionManifest) -> str:
        return regression_campaign_identity(
            spec_name=self.spec.name,
            schema_version=self.spec.schema_version,
            manifest_sha256=manifest.manifest_sha256,
        )

    def _regression_gate_path(self, manifest: RegressionManifest) -> Path:
        return self._regression_gate_path_for_case(manifest.case_id)

    def _regression_gate_path_for_case(self, case_id: str) -> Path:
        if self._run_dir is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("entry worker was not bound to an immutable run directory")
        artifact_root = self._regression_artifact_root or self._run_dir
        return artifact_root / "regressions" / case_id / "regression_report.json"

    def _run_regression_rule_plan_entry(
        self,
        entry: RulePlanEntry,
        attempt_dir: Path,
    ) -> dict[str, object]:
        try:
            manifest = self._regression_manifest()
            report_path = self._regression_gate_path(manifest)
            campaign_identity_hash = self._regression_campaign_identity(manifest)
            if entry.regression_case_id != manifest.case_id:
                raise RegressionConfigurationError("regression entry case identity mismatch")
            case = load_regression_case(manifest.path, self.environment)
        except RegressionConfigurationError as exc:
            if "manifest" in locals() and "campaign_identity_hash" in locals():
                write_regression_configuration_failure(
                    report_path=self._regression_gate_path(manifest),
                    manifest=manifest,
                    campaign_identity_hash=campaign_identity_hash,
                    exception_type=type(exc).__name__,
                )
            else:
                manifest_path = self.spec.regression.get("manifest")
                manifest_sha256 = "0" * 64
                if (
                    isinstance(manifest_path, Path)
                    and not manifest_path.is_symlink()
                    and manifest_path.is_file()
                ):
                    try:
                        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                    except OSError:
                        manifest_sha256 = "0" * 64
                unresolved_campaign_identity = regression_campaign_identity(
                    spec_name=self.spec.name,
                    schema_version=self.spec.schema_version,
                    manifest_sha256=manifest_sha256,
                )
                write_unresolved_regression_configuration_failure(
                    report_path=self._regression_gate_path_for_case(
                        str(entry.regression_case_id)
                    ),
                    case_id=str(entry.regression_case_id),
                    manifest_sha256=manifest_sha256,
                    campaign_identity_hash=unresolved_campaign_identity,
                    exception_type=type(exc).__name__,
                )
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=None,
                infrastructure_error_kind=ErrorKind.INFRASTRUCTURE_ERROR,
                infrastructure_reason="regression_configuration_error",
            )
        runner = build_production_regression_runner(report_path)
        runner.run(case, campaign_identity_hash=campaign_identity_hash)
        try:
            current_identity = probe_current_regression_environment_identity(case)
            load_verified_regression_report(
                report_path,
                case=case,
                campaign_identity_hash=campaign_identity_hash,
                current_identity=current_identity,
            )
        except RegressionArtifactError as exc:
            return self._finish_entry(
                entry,
                attempt_dir,
                terminal_state=EntryState.INFRASTRUCTURE_FAILED,
                generated=None,
                infrastructure_error_kind=ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                infrastructure_reason=str(exc),
            )
        return self._finish_entry(
            entry,
            attempt_dir,
            terminal_state=EntryState.COMPLETED,
            generated=None,
        )

    def _build_rule_entry_pairs(
        self,
        adapter: Any,
        entry: RulePlanEntry,
        queries: tuple[str, ...],
        entry_dir: Path,
        *,
        graph_snapshot_hash: str,
        graph_manifest: GraphManifest | None,
        graph_statements: tuple[str, ...],
        dbms_fingerprint: str,
        dbms_version: str,
        driver_version: str,
        dbms_health_epoch: str,
        deadline: float,
        progress_path: Path,
        progress: Mapping[str, object],
        attempt_started: float,
        prior_elapsed: float,
    ) -> dict[str, object]:
        """Build, calibrate, and durably promote one entry's Stage-1 pairs."""

        screening_policy = DEFAULT_SCREENING_POLICY
        _require_screening_policy_config(self.spec, policy=screening_policy)
        _require_unique_entry_query_asts(queries)
        factory = RulePairFactory(RuleRegistry.default())
        dialect = _dialect_for(entry.target)
        replayable = getattr(adapter, "gdsmith_query_is_replayable", gdsmith_read_query_is_replayable)
        max_rows = int(self.spec.validation["max_result_rows"])
        rejects: Counter[str] = Counter(dict(progress["reject_counts"]))
        not_applicable = rejects["RULE_NOT_APPLICABLE"]
        last_query_ordinal = int(progress["last_query_ordinal"])
        generated_seeds = int(progress["attempted_query_records"])
        applicable_query_ordinals = set(progress["applicable_query_ordinals"])
        verified_applicability_limit = max(
            last_query_ordinal,
            max(applicable_query_ordinals, default=0),
        )
        if verified_applicability_limit > len(queries):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "applicable query ledger exceeds the sealed query corpus",
            )
        for verified_ordinal in range(1, verified_applicability_limit + 1):
            verified_build = factory.build(
                queries[verified_ordinal - 1],
                entry,
                entry.target,
            )
            replay_is_applicable = (
                not verified_build.quarantine
                and verified_build.pair is not None
            )
            if replay_is_applicable != (
                verified_ordinal in applicable_query_ordinals
            ):
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "applicable query ledger disagrees with deterministic rule replay",
                )
        legacy_migration_sha256 = progress["legacy_migration_sha256"]
        loaded_schema = int(progress["_loaded_schema_version"])
        current_identity = _runtime_evidence_identity(
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        )
        persisted_identity = progress["evidence_identity"]
        if persisted_identity is not None and dict(persisted_identity) != current_identity:
            _write_entry_runtime_invalidation(
                entry_dir,
                entry=entry,
                evidence_identity=dict(persisted_identity),
                reason="runtime evidence identity changed before resume",
                observed=current_identity,
            )
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "runtime evidence identity changed before resume",
            )
        evidence_identity: dict[str, object] = current_identity
        stage1_intent = (
            dict(progress["stage1_intent"])
            if progress["stage1_intent"] is not None
            else None
        )
        accepted_record_ledger = [
            dict(value) for value in progress["accepted_record_ledger"]
        ]
        noise_floor_id = progress["noise_floor_id"]
        noise_intent = (
            dict(progress["noise_intent"])
            if progress["noise_intent"] is not None
            else None
        )
        noise_failure_id = progress["noise_failure_id"]
        noise_failure_intent = (
            dict(progress["noise_failure_intent"])
            if progress["noise_failure_intent"] is not None
            else None
        )
        pending = [dict(value) for value in progress["pending_acceptances"]]
        pending_by_hash = {str(value["pair_hash"]): value for value in pending}
        records_path = entry_dir / "pair_runtime.jsonl"
        try:
            store = CompactRuntimeStore(records_path)
        except ArtifactIntegrityError as exc:
            raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, str(exc)) from exc
        except ArtifactWriteError as exc:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, str(exc)) from exc
        if loaded_schema == 2 and (
            progress["accepted_pair_hashes"]
            or pending
            or store.count
            or (entry_dir / "noise_calibration.json").exists()
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "schema-v2 runtime evidence cannot be bound to a guessed DBMS health epoch",
            )
        if loaded_schema == 3 and (
            progress["accepted_pair_hashes"]
            or pending
            or stage1_intent is not None
            or store.count
            or (entry_dir / "noise_calibration.json").exists()
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "schema-v3 accepted evidence has no ordered digest ledger or raw noise anchor",
            )
        if (
            loaded_schema == 4
            and (entry_dir / "noise_calibration_failure.json").exists()
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "schema-v4 progress cannot bind an existing noise failure artifact",
            )
        durable_noise_failure = _load_entry_noise_failure(
            entry_dir / "noise_calibration_failure.json",
            entry=entry,
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        )
        if durable_noise_failure is None and (
            noise_failure_id is not None or noise_failure_intent is not None
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "noise failure progress exists without its durable artifact",
            )
        if durable_noise_failure is not None:
            durable_failure_id = str(durable_noise_failure["content_hash"])
            if noise_failure_id is not None:
                failure_binding_matches = (
                    noise_failure_id == durable_failure_id
                    and noise_failure_intent is None
                )
            else:
                failure_binding_matches = (
                    noise_failure_intent is not None
                    and noise_failure_intent.get("noise_failure_id")
                    == durable_failure_id
                )
            if not failure_binding_matches:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "noise failure artifact has no exact progress binding",
                )
        acceptance_records = accepted_record_ledger + pending
        expected_probe_hashes = tuple(
            str(value["pair_hash"])
            for value in _expected_noise_probe_records(acceptance_records)
        )
        existing_noise: tuple[NoiseCalibration, str] | None = None
        if store.count:
            existing_noise = _load_entry_noise(
                entry_dir / "noise_calibration.json",
                entry=entry,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
                expected_probe_hashes=expected_probe_hashes,
            )
            if existing_noise is None or existing_noise[0].p99_delta_ms is None:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "pair_runtime records exist without valid bound noise evidence",
                )
            if noise_floor_id != existing_noise[1] or noise_intent is not None:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "pair_runtime records are not bound to the anchored noise artifact",
                )
        runtime_ordinals = _validate_runtime_records(
            store,
            entry=entry,
            graph_snapshot_hash=graph_snapshot_hash,
            source_queries=queries,
            factory=factory,
            noise_floor_id=existing_noise[1] if existing_noise is not None else None,
            noise_p99_ms=(
                existing_noise[0].p99_delta_ms if existing_noise is not None else None
            ),
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        )
        accepted_hashes = (
            set(progress["accepted_pair_hashes"])
            if loaded_schema in {4, 5, 6}
            else set()
        )
        (
            accepted_record_ledger,
            accepted_hashes,
            pending_by_hash,
            stage1_intent,
            recovery_needs_persist,
        ) = _reconcile_runtime_ledger(
            records=store.records,
            ledger=accepted_record_ledger,
            accepted_hashes=accepted_hashes,
            pending_by_hash=pending_by_hash,
            stage1_intent=stage1_intent,
        )
        pending = sorted(pending_by_hash.values(), key=lambda value: int(value["source_query_ordinal"]))

        def persist_progress() -> None:
            _persist_entry_progress(
                progress_path,
                entry=entry,
                prior_elapsed=prior_elapsed,
                attempt_started=attempt_started,
                now=self._clock(),
                last_query_ordinal=last_query_ordinal,
                applicable_query_ordinals=tuple(
                    sorted(applicable_query_ordinals)
                ),
                rejects=rejects,
                accepted_hashes=tuple(sorted(accepted_hashes)),
                pending_acceptances=tuple(pending),
                legacy_migration_sha256=legacy_migration_sha256,
                evidence_identity=evidence_identity,
                stage1_intent=stage1_intent,
                accepted_record_ledger=tuple(accepted_record_ledger),
                noise_floor_id=noise_floor_id,
                noise_intent=noise_intent,
                noise_failure_id=noise_failure_id,
                noise_failure_intent=noise_failure_intent,
            )

        def complete_query_record(ordinal: int) -> None:
            nonlocal generated_seeds, last_query_ordinal
            last_query_ordinal = ordinal
            generated_seeds = ordinal
            persist_progress()

        legacy_path = entry_dir / "accepted_pairs.jsonl"
        if legacy_path.exists():
            legacy_sha256 = _path_sha256(legacy_path)
            if legacy_migration_sha256 not in {None, legacy_sha256}:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "legacy accepted-pair migration identity changed",
                )
            legacy_ordinals = _validated_entry_accepted_hashes(
                legacy_path,
                entry=entry,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                source_queries=queries,
            )
            old_progress_hashes = (
                set(progress["accepted_pair_hashes"])
                if loaded_schema == 1
                else set(pending_by_hash).union(accepted_hashes)
            )
            legacy_hashes = set(legacy_ordinals)
            appended = legacy_hashes - old_progress_hashes
            if (
                old_progress_hashes - legacy_hashes
                or len(appended) > 1
                or (
                    appended
                    and legacy_ordinals[next(iter(appended))] != last_query_ordinal + 1
                )
            ):
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "legacy accepted pairs do not match their Task-5 progress",
                )
            if appended:
                last_query_ordinal = legacy_ordinals[next(iter(appended))]
                generated_seeds = last_query_ordinal
            for pair_hash, ordinal in sorted(legacy_ordinals.items(), key=lambda item: item[1]):
                if pair_hash in accepted_hashes or pair_hash in pending_by_hash:
                    continue
                pair = _rebuild_pending_pair(factory, entry, queries, ordinal, pair_hash)
                try:
                    left = adapter.execute(
                        pair.query_a,
                        pair.parameters,
                        "acceptance_migration_base",
                        max_rows=max_rows,
                        timeout_seconds=entry.query_timeout_seconds,
                    )
                    right = adapter.execute(
                        pair.query_b,
                        pair.parameters,
                        "acceptance_migration_transformed",
                        max_rows=max_rows,
                        timeout_seconds=entry.query_timeout_seconds,
                    )
                except Exception as exc:
                    raise WorkerFailure(
                        _entry_infrastructure_error_kind(exc)
                        or ErrorKind.INFRASTRUCTURE_ERROR,
                        f"legacy accepted-pair runtime migration failed: {exc}",
                    ) from exc
                if _is_truncated(left, max_rows) or _is_truncated(right, max_rows):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "legacy accepted-pair migration produced a truncated result",
                    )
                equivalence = compare_typed_results(
                    left.rows,
                    right.rows,
                    ordered=_has_order_by(pair.query_a),
                )
                if not equivalence.equivalent or not equivalence.complete_result:
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "legacy accepted-pair migration failed typed equivalence revalidation",
                    )
                applicable_query_ordinals.add(ordinal)
                migrated = _pending_acceptance(
                    pair_hash,
                    ordinal,
                    left,
                    right,
                    max_sample_us=_entry_sample_upper_bound_us(entry),
                )
                pending.append(migrated)
                pending_by_hash[pair_hash] = migrated
            legacy_migration_sha256 = legacy_sha256
            # This checkpoint is the commit point. Before it, Task-5 JSONL is
            # authoritative; after it, the checkpoint is sufficient to rebuild
            # every query/certificate from the sealed corpus.
            persist_progress()
            _unlink_fsync(legacy_path)
            loaded_schema = 3
        elif loaded_schema == 1 and progress["accepted_pair_hashes"]:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "Task-5 accepted progress exists without its durable evidence",
            )

        if set(pending_by_hash).intersection(accepted_hashes):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "a pair cannot be both pending and durably accepted",
            )
        if len(accepted_hashes) + len(pending) > entry.accepted_pair_quota:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "runtime and pending pairs exceed the immutable entry quota",
            )
        if recovery_needs_persist or loaded_schema in {1, 2, 3, 4, 5}:
            persist_progress()

        infrastructure_error_kind: ErrorKind | None = None
        infrastructure_reason: str | None = None
        state = EntryState.BUILDING_ACCEPTED_PAIRS
        final_ordinal = min(len(queries), entry.max_generated_seed_budget)
        total_pairs = lambda: len(accepted_hashes) + len(pending)
        for ordinal in range(last_query_ordinal + 1, final_ordinal + 1):
            if total_pairs() >= entry.accepted_pair_quota:
                break
            query = queries[ordinal - 1]
            if self._clock() >= deadline:
                state = EntryState.RESOURCE_EXHAUSTED
                break
            build = factory.build(query, entry, entry.target)
            if build.quarantine:
                if ordinal in applicable_query_ordinals:
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "applicable query ledger disagrees with deterministic rule replay",
                    )
                _write_entry_quarantine(
                    entry_dir,
                    entry,
                    query_a=query,
                    query_b=None,
                    reason=build.reject_code or "RULE_INVARIANT_MISMATCH",
                    transform_metadata={},
                )
                complete_query_record(ordinal)
                state = EntryState.QUARANTINED
                break
            if build.pair is None:
                if ordinal in applicable_query_ordinals:
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "applicable query ledger disagrees with deterministic rule replay",
                    )
                reason = build.reject_code or "RULE_NOT_APPLICABLE"
                rejects[reason] += 1
                if reason == "RULE_NOT_APPLICABLE":
                    not_applicable += 1
                complete_query_record(ordinal)
                continue
            pair = build.pair
            if ordinal not in applicable_query_ordinals:
                applicable_query_ordinals.add(ordinal)
                # Seal applicability before downstream dialect, execution, or
                # equivalence rejection so retries cannot lose the denominator.
                persist_progress()
            left_decision = dialect.check(PortableQuery(pair.query_a))
            right_decision = dialect.check(PortableQuery(pair.query_b))
            if left_decision.kind is DecisionKind.REJECT:
                rejects[f"DIALECT_{left_decision.reason_code.value}"] += 1
                complete_query_record(ordinal)
                continue
            if right_decision.kind is DecisionKind.REJECT:
                rejects[f"DIALECT_{right_decision.reason_code.value}"] += 1
                complete_query_record(ordinal)
                continue
            if not replayable(pair.query_a) or not replayable(pair.query_b):
                rejects["DIALECT_REPLAY_REJECT"] += 1
                complete_query_record(ordinal)
                continue
            if pair.pair_hash in accepted_hashes or pair.pair_hash in pending_by_hash:
                complete_query_record(ordinal)
                continue
            try:
                left = adapter.execute(
                    pair.query_a,
                    pair.parameters,
                    "acceptance_base",
                    max_rows=max_rows,
                    timeout_seconds=entry.query_timeout_seconds,
                )
                right = adapter.execute(
                    pair.query_b,
                    pair.parameters,
                    "acceptance_transformed",
                    max_rows=max_rows,
                    timeout_seconds=entry.query_timeout_seconds,
                )
            except QueryTimeoutError:
                rejects["QUERY_TIMEOUT"] += 1
                complete_query_record(ordinal)
                continue
            except TimeoutError as exc:
                state = EntryState.INFRASTRUCTURE_FAILED
                infrastructure_error_kind = ErrorKind.DRIVER_TIMEOUT
                infrastructure_reason = str(exc)
                persist_progress()
                break
            except Exception as exc:
                if _is_entry_infrastructure_exception(exc):
                    state = EntryState.INFRASTRUCTURE_FAILED
                    infrastructure_error_kind = _entry_infrastructure_error_kind(exc)
                    infrastructure_reason = str(exc)
                    persist_progress()
                    break
                rejects["QUERY_EXECUTION_ERROR"] += 1
                complete_query_record(ordinal)
                continue
            if _is_truncated(left, max_rows) or _is_truncated(right, max_rows):
                rejects["RESULT_TRUNCATED"] += 1
                complete_query_record(ordinal)
                continue
            equivalence = compare_typed_results(
                left.rows,
                right.rows,
                ordered=_has_order_by(pair.query_a),
            )
            if not equivalence.equivalent:
                if not equivalence.complete_result:
                    reason = equivalence.reason_codes[0] if equivalence.reason_codes else "typed_equivalence_incomplete"
                    rejects[reason.upper()] += 1
                    complete_query_record(ordinal)
                    continue
                if entry.tier == "core":
                    _write_entry_quarantine(
                        entry_dir,
                        entry,
                        query_a=pair.query_a,
                        query_b=pair.query_b,
                        reason="TYPED_EQUIVALENCE_FAILED",
                        transform_metadata=pair.transform_metadata,
                    )
                    complete_query_record(ordinal)
                    state = EntryState.QUARANTINED
                    break
                rejects["TYPED_EQUIVALENCE_FAILED"] += 1
                complete_query_record(ordinal)
                continue
            try:
                pending_record = _pending_acceptance(
                    pair.pair_hash,
                    ordinal,
                    left,
                    right,
                    max_sample_us=_entry_sample_upper_bound_us(entry),
                )
            except WorkerFailure as exc:
                state = EntryState.INFRASTRUCTURE_FAILED
                infrastructure_error_kind = exc.kind
                infrastructure_reason = str(exc)
                persist_progress()
                break
            pending.append(pending_record)
            pending_by_hash[pair.pair_hash] = pending_record
            complete_query_record(ordinal)
        else:
            if total_pairs() < entry.accepted_pair_quota:
                state = EntryState.UNDERFILLED

        if state is EntryState.BUILDING_ACCEPTED_PAIRS and total_pairs() < entry.accepted_pair_quota:
            state = EntryState.UNDERFILLED

        if state is EntryState.BUILDING_ACCEPTED_PAIRS and total_pairs() == entry.accepted_pair_quota:
            def persist_noise_intent(
                artifact_id: str,
                probe_hashes: tuple[str, ...],
            ) -> None:
                nonlocal noise_intent
                noise_intent = {
                    "noise_floor_id": artifact_id,
                    "probe_pair_hashes": list(probe_hashes),
                }
                persist_progress()

            def persist_noise_failure_intent(artifact_id: str) -> None:
                nonlocal noise_failure_intent
                noise_failure_intent = {"noise_failure_id": artifact_id}
                persist_progress()

            noise, calibrated_noise_floor_id = _load_or_calibrate_entry_noise(
                entry_dir,
                entry=entry,
                adapter=adapter,
                factory=factory,
                queries=queries,
                pending=accepted_record_ledger + pending,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
                anchored_noise_floor_id=noise_floor_id,
                pending_noise_intent=noise_intent,
                persist_intent=persist_noise_intent,
                anchored_noise_failure_id=noise_failure_id,
                pending_noise_failure_intent=noise_failure_intent,
                persist_failure_intent=persist_noise_failure_intent,
            )
            failure = _load_entry_noise_failure(
                entry_dir / "noise_calibration_failure.json",
                entry=entry,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
            )
            if failure is not None:
                failure_id = str(failure["content_hash"])
                if noise_failure_id is not None and noise_failure_id != failure_id:
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "noise failure identity differs from progress",
                    )
                if (
                    noise_failure_id is None
                    and (
                        noise_failure_intent is None
                        or noise_failure_intent.get("noise_failure_id") != failure_id
                    )
                ):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "noise failure artifact was not preceded by its durable intent",
                    )
                noise_failure_id = failure_id
                noise_failure_intent = None
                persist_progress()
                state = EntryState.INFRASTRUCTURE_FAILED
                infrastructure_error_kind = ErrorKind(str(failure["error_kind"]))
                infrastructure_reason = str(failure["error_reason"])

            noise_is_complete = bool(calibrated_noise_floor_id)
            if failure is None and noise_is_complete:
                if (
                    noise_floor_id is not None
                    and noise_floor_id != calibrated_noise_floor_id
                ):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "noise calibration identity differs from progress",
                    )
                if (
                    noise_floor_id is None
                    and (
                        noise_intent is None
                        or noise_intent.get("noise_floor_id")
                        != calibrated_noise_floor_id
                    )
                ):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "noise artifact was not preceded by its durable intent",
                    )
                noise_floor_id = calibrated_noise_floor_id
                noise_intent = None
                persist_progress()

            if failure is not None:
                pass
            elif not noise_is_complete or not noise.valid or noise.p99_delta_ms is None:
                state = EntryState.INFRASTRUCTURE_FAILED
                infrastructure_error_kind = ErrorKind.INFRASTRUCTURE_ERROR
                infrastructure_reason = "invalid fixed-protocol A/A noise calibration"
            else:
                for pending_record in tuple(pending):
                    if self._clock() >= deadline:
                        state = EntryState.RESOURCE_EXHAUSTED
                        break
                    ordinal = int(pending_record["source_query_ordinal"])
                    pair_hash = str(pending_record["pair_hash"])
                    pair = _rebuild_pending_pair(factory, entry, queries, ordinal, pair_hash)
                    evidence = RuntimeHarness().measure_pair(
                        adapter,
                        pair.query_a,
                        pair.query_b,
                        parameters=pair.parameters,
                        policy=RuntimePolicy(
                            warmups=screening_policy.stage1_warmups,
                            pairs=screening_policy.stage1_pairs,
                            seed=_stage1_seed(
                                entry,
                                pair_hash,
                                policy=screening_policy,
                            ),
                            require_exact_balance=(
                                screening_policy.stage1_require_exact_balance
                            ),
                            timeout_ms=float(entry.query_timeout_seconds * 1000),
                            bootstrap_resamples=(
                                screening_policy.stage1_bootstrap_resamples
                            ),
                        ),
                    )
                    if evidence.verdict is RuntimeVerdict.INVALID:
                        state = EntryState.INFRASTRUCTURE_FAILED
                        infrastructure_error_kind = ErrorKind.INFRASTRUCTURE_ERROR
                        infrastructure_reason = (
                            "Stage-1 measurement is invalid: "
                            + ",".join(evidence.reason_codes)
                        )
                        break
                    try:
                        record = PairRuntimeRecord.from_evidence(
                            pair=pair,
                            entry=entry,
                            evidence=evidence,
                            noise_floor_id=noise_floor_id,
                            noise_p99_ms=noise.p99_delta_ms,
                            dbms_fingerprint=dbms_fingerprint,
                            dbms_health_epoch=dbms_health_epoch,
                            source_query_ordinal=ordinal,
                            graph_fingerprint=graph_snapshot_hash,
                        )
                    except (ArithmeticError, TypeError, ValueError) as exc:
                        state = EntryState.INFRASTRUCTURE_FAILED
                        infrastructure_error_kind = ErrorKind.INFRASTRUCTURE_ERROR
                        infrastructure_reason = (
                            "Stage-1 runtime evidence conversion failed: "
                            f"{type(exc).__name__}"
                        )
                        break
                    stage1_intent = {
                        "pair_hash": pair_hash,
                        "record_digest": canonical_record_digest(record),
                        "source_query_ordinal": ordinal,
                    }
                    # The digest intent is committed before the append. Recovery
                    # accepts a durable-ahead record only when it matches this
                    # exact pending transaction and canonical record digest.
                    persist_progress()
                    try:
                        store.append(record)
                    except ArtifactWriteError as exc:
                        state = EntryState.INFRASTRUCTURE_FAILED
                        infrastructure_error_kind = ErrorKind.INFRASTRUCTURE_ERROR
                        infrastructure_reason = f"pair_runtime durable append failed: {exc}"
                        break
                    accepted_hashes.add(pair_hash)
                    accepted_record_ledger.append(
                        {
                            **pending_record,
                            "record_digest": stage1_intent["record_digest"],
                        }
                    )
                    pending.remove(pending_record)
                    pending_by_hash.pop(pair_hash, None)
                    stage1_intent = None
                    persist_progress()
                else:
                    state = EntryState.STAGE1_COMPLETE

        stage1_selected_pairs = 0
        stage2_measured_pairs = 0
        stage2_promoted_pairs = 0
        validation_selected_pairs = 0
        validation_passed_pairs = 0
        pipeline_terminal_candidates = 0
        submit_ready_issues = 0
        control_gate_hit = False
        control_validation_pass = False
        timeout_records: tuple[TimeoutQueueRecord, ...] = ()
        if state is EntryState.STAGE1_COMPLETE:
            if noise.p99_delta_ms is None:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "Stage-2 screening requires a bound p99 noise floor",
                )
            stage2_outcome = self._screen_rule_entry_stage2(
                adapter,
                entry,
                entry_dir,
                accepted_pair_count=len(accepted_hashes),
                queries=queries,
                factory=factory,
                records=store.records,
                graph_manifest=graph_manifest,
                graph_statements=graph_statements,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_version=dbms_version,
                driver_version=driver_version,
                dbms_health_epoch=dbms_health_epoch,
                noise_floor_id=str(noise_floor_id),
                noise_p99_ms=float(noise.p99_delta_ms),
                deadline=deadline,
            )
            state = stage2_outcome["terminal_state"]
            stage1_selected_pairs = int(stage2_outcome["stage1_selected_pairs"])
            stage2_measured_pairs = int(stage2_outcome["stage2_measured_pairs"])
            stage2_promoted_pairs = int(stage2_outcome["stage2_promoted_pairs"])
            validation_selected_pairs = int(
                stage2_outcome["validation_selected_pairs"]
            )
            validation_passed_pairs = int(
                stage2_outcome.get("validation_passed_pairs", 0)
            )
            pipeline_terminal_candidates = int(
                stage2_outcome.get("pipeline_terminal_candidates", 0)
            )
            submit_ready_issues = int(stage2_outcome.get("submit_ready_issues", 0))
            control_gate_hit = stage2_outcome.get("control_gate_hit", False) is True
            control_validation_pass = (
                stage2_outcome.get("control_validation_pass", False) is True
            )
            timeout_records = tuple(stage2_outcome["timeout_records"])
            if stage2_outcome.get("infrastructure_reason") is not None:
                infrastructure_error_kind = stage2_outcome[
                    "infrastructure_error_kind"
                ]
                infrastructure_reason = str(stage2_outcome["infrastructure_reason"])

        persist_progress()
        result: dict[str, object] = {
            "accepted_hashes": tuple(sorted(accepted_hashes)),
            "applicable_query_ordinals": tuple(
                sorted(applicable_query_ordinals)
            ),
            "pending_acceptances": tuple(pending),
            "legacy_migration_sha256": legacy_migration_sha256,
            "evidence_identity": evidence_identity,
            "stage1_intent": stage1_intent,
            "accepted_record_ledger": tuple(accepted_record_ledger),
            "noise_floor_id": noise_floor_id,
            "noise_intent": noise_intent,
            "noise_failure_id": noise_failure_id,
            "noise_failure_intent": noise_failure_intent,
            "generated_seeds": generated_seeds,
            "rejects": dict(sorted(rejects.items())),
            "rule_stats": {
                "accepted": ({entry.rule_id: len(accepted_hashes)} if accepted_hashes else {}),
                "not_applicable": ({entry.rule_id: not_applicable} if not_applicable else {}),
            },
            "stage1_selected_pairs": stage1_selected_pairs,
            "stage2_measured_pairs": stage2_measured_pairs,
            "stage2_promoted_pairs": stage2_promoted_pairs,
            "validation_selected_pairs": validation_selected_pairs,
            "validation_passed_pairs": validation_passed_pairs,
            "pipeline_terminal_candidates": pipeline_terminal_candidates,
            "submit_ready_issues": submit_ready_issues,
            "cell_metrics": dict(stage2_outcome.get("cell_metrics", {}))
            if state in {EntryState.COMPLETED, EntryState.STAGE2_COMPLETE}
            else {},
            "control_gate_hit": control_gate_hit,
            "control_validation_pass": control_validation_pass,
            "timeout_records": timeout_records,
            "terminal_state": state,
        }
        if infrastructure_reason is not None:
            result["infrastructure_error_kind"] = infrastructure_error_kind
            result["infrastructure_reason"] = infrastructure_reason
        return result

    def _screen_rule_entry_stage2(
        self,
        adapter: Any,
        entry: RulePlanEntry,
        entry_dir: Path,
        *,
        accepted_pair_count: int,
        queries: tuple[str, ...],
        factory: RulePairFactory,
        records: tuple[dict[str, Any], ...],
        graph_manifest: GraphManifest | None,
        graph_statements: tuple[str, ...],
        graph_snapshot_hash: str,
        dbms_fingerprint: str,
        dbms_version: str,
        driver_version: str,
        dbms_health_epoch: str,
        noise_floor_id: str,
        noise_p99_ms: float,
        deadline: float,
    ) -> dict[str, object]:
        """Upgrade only deterministic Stage-1 Top-K pairs to Stage-2 evidence."""

        if graph_manifest is None:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "Neo4j Stage-2 candidate requires a complete graph manifest",
            )
        if not math.isfinite(noise_p99_ms) or noise_p99_ms < 0:
            return {
                "terminal_state": EntryState.INFRASTRUCTURE_FAILED,
                "stage1_selected_pairs": 0,
                "stage2_measured_pairs": 0,
                "stage2_promoted_pairs": 0,
                "validation_selected_pairs": 0,
                "timeout_records": (),
                "infrastructure_error_kind": ErrorKind.INFRASTRUCTURE_ERROR,
                "infrastructure_reason": (
                    "Stage-2 screening requires a finite non-negative p99 noise floor"
                ),
            }
        policy = DEFAULT_SCREENING_POLICY
        _require_screening_policy_config(self.spec, policy=policy)
        typed_records: tuple[PairRuntimeRecord, ...] = tuple(
            PairRuntimeRecord(
                **{
                    **record,
                    "samples_us": tuple(
                        tuple(sample) for sample in record["samples_us"]
                    ),
                }
            )
            for record in records
        )
        try:
            selected_hashes = stage1_top_k(
                typed_records,
                noise_p99_ms=noise_p99_ms,
                policy=policy,
            )
        except (TypeError, ValueError) as exc:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                f"Stage-1 ranking evidence is invalid: {exc}",
            ) from exc
        records_by_hash = {
            str(record["pair_hash"]): dict(record) for record in records
        }
        candidate_root = entry_dir / "candidates"
        entry_payload = _stage2_entry_payload(entry)
        evidence_identity = _runtime_evidence_identity(
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        )
        screening_policy = _stage2_screening_policy(
            self.spec,
            entry,
            noise_floor_id=noise_floor_id,
        )
        progress_path = entry_dir / "stage2_progress.json"
        selection_path = entry_dir / "stage2_selection.json"
        stage2_progress = _load_or_create_stage2_progress(
            progress_path,
            entry_payload=entry_payload,
            evidence_identity=evidence_identity,
            noise_floor_id=noise_floor_id,
            screening_policy=screening_policy,
            selected_pair_hashes=selected_hashes,
            candidate_root=candidate_root,
            selection_path=selection_path,
        )
        candidate_ledger = [
            dict(value) for value in stage2_progress["candidate_ledger"]
        ]
        candidate_intent = (
            dict(stage2_progress["candidate_intent"])
            if stage2_progress["candidate_intent"] is not None
            else None
        )

        def persist_stage2_progress() -> None:
            _persist_stage2_progress(
                progress_path,
                entry_payload=entry_payload,
                evidence_identity=evidence_identity,
                noise_floor_id=noise_floor_id,
                screening_policy=screening_policy,
                selected_pair_hashes=selected_hashes,
                candidate_ledger=candidate_ledger,
                candidate_intent=candidate_intent,
            )

        timeout_records: list[TimeoutQueueRecord] = []
        for record in typed_records:
            timeout_side = _compact_timeout_side(record)
            if timeout_side is not None:
                timeout_records.append(
                    TimeoutQueueRecord(
                        entry_id=entry.entry_id,
                        pair_hash=record.pair_hash,
                        timeout_side=timeout_side,
                        stage="stage1",
                        evidence_digest=canonical_record_digest(
                            records_by_hash[record.pair_hash]
                        ),
                        graph_snapshot_hash=graph_snapshot_hash,
                        dbms_fingerprint=dbms_fingerprint,
                        dbms_health_epoch=dbms_health_epoch,
                        noise_floor_id=noise_floor_id,
                    )
                )

        measured: list[tuple[Stage2Candidate, str]] = []
        pairs_by_hash: dict[str, object] = {}
        for selected_index, pair_hash in enumerate(selected_hashes):
            stage1_record = records_by_hash[pair_hash]
            ordinal = int(stage1_record["source_query_ordinal"])
            pair = _rebuild_pending_pair(factory, entry, queries, ordinal, pair_hash)
            pairs_by_hash[pair_hash] = pair
            expected_direction = stage1_record.get("runtime_direction")
            if expected_direction not in {"a", "b"}:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "ordinary Stage-1 Top-K record has no frozen runtime direction",
                )
            seed = _stage2_seed(entry, pair_hash, policy=policy)
            candidate_path = candidate_root / pair_hash / "candidate.json"
            existing = _load_stage2_candidate(
                candidate_path,
                entry=entry,
                pair=pair,
                stage1_record=stage1_record,
                graph_manifest=graph_manifest,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
                noise_floor_id=noise_floor_id,
                noise_p99_ms=noise_p99_ms,
                seed=seed,
                expected_direction=str(expected_direction),
                entry_payload=entry_payload,
                screening_policy=screening_policy,
                policy=policy,
            )
            if selected_index < len(candidate_ledger):
                ledger_entry = candidate_ledger[selected_index]
                if existing is None or (
                    ledger_entry["pair_hash"] != pair_hash
                    or ledger_entry["candidate_digest"] != existing[1]
                ):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "Stage-2 candidate differs from its ordered progress anchor",
                    )
            elif candidate_intent is not None:
                if existing is None:
                    _remove_empty_stage2_candidate_dir(
                        candidate_path.parent,
                        candidate_root=candidate_root,
                    )
                    candidate_intent = None
                    persist_stage2_progress()
                elif (
                    candidate_intent["selected_index"] != selected_index
                    or candidate_intent["pair_hash"] != pair_hash
                    or candidate_intent["candidate_digest"] != existing[1]
                ):
                    raise WorkerFailure(
                        ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                        "durable Stage-2 candidate differs from its exact intent",
                    )
                else:
                    candidate_ledger.append(
                        {
                            "pair_hash": pair_hash,
                            "candidate_digest": existing[1],
                        }
                    )
                    candidate_intent = None
                    persist_stage2_progress()
            elif existing is not None:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "Stage-2 candidate exists without a progress anchor or intent",
                )
            if existing is None:
                if self._clock() >= deadline:
                    return {
                        "terminal_state": EntryState.RESOURCE_EXHAUSTED,
                        "stage1_selected_pairs": len(selected_hashes),
                        "stage2_measured_pairs": len(measured),
                        "stage2_promoted_pairs": sum(
                            candidate.decision.promote
                            for candidate, _digest in measured
                        ),
                        "validation_selected_pairs": 0,
                        "timeout_records": tuple(timeout_records),
                    }
                evidence = RuntimeHarness().measure_pair(
                    adapter,
                    pair.query_a,
                    pair.query_b,
                    parameters=pair.parameters,
                    policy=RuntimePolicy(
                        warmups=policy.stage2_warmups,
                        pairs=policy.stage2_pairs,
                        seed=seed,
                        require_exact_balance=policy.stage2_require_exact_balance,
                        timeout_ms=float(entry.query_timeout_seconds * 1000),
                        bootstrap_resamples=policy.stage2_bootstrap_resamples,
                    ),
                    expected_slow_query=str(expected_direction),
                )
                if evidence.verdict is RuntimeVerdict.INVALID:
                    kind = (
                        ErrorKind.DRIVER_TIMEOUT
                        if "execution_error:TimeoutError" in evidence.reason_codes
                        else ErrorKind.INFRASTRUCTURE_ERROR
                    )
                    return {
                        "terminal_state": EntryState.INFRASTRUCTURE_FAILED,
                        "stage1_selected_pairs": len(selected_hashes),
                        "stage2_measured_pairs": len(measured),
                        "stage2_promoted_pairs": sum(
                            candidate.decision.promote
                            for candidate, _digest in measured
                        ),
                        "validation_selected_pairs": 0,
                        "timeout_records": tuple(timeout_records),
                        "infrastructure_error_kind": kind,
                        "infrastructure_reason": (
                            "Stage-2 measurement is invalid: "
                            + ",".join(evidence.reason_codes)
                        ),
                    }
                decision = evaluate_stage2(
                    evidence,
                    noise_p99_ms=noise_p99_ms,
                    policy=policy,
                )
                score = (
                    float(evidence.median_delta_ms)
                    / max(noise_p99_ms, policy.normalization_epsilon_ms)
                    if evidence.median_delta_ms is not None
                    else float(stage1_record["delta_over_noise"])
                )
                stage2_candidate = Stage2Candidate(
                    pair_hash=pair_hash,
                    decision=decision,
                    noise_normalized_score=score,
                    evidence=evidence,
                )
                candidate_payload = _stage2_candidate_payload(
                    entry=entry,
                    pair=pair,
                    stage1_record=stage1_record,
                    graph_manifest=graph_manifest,
                    graph_snapshot_hash=graph_snapshot_hash,
                    dbms_fingerprint=dbms_fingerprint,
                    dbms_health_epoch=dbms_health_epoch,
                    noise_floor_id=noise_floor_id,
                    seed=seed,
                    expected_direction=str(expected_direction),
                    candidate=stage2_candidate,
                    entry_payload=entry_payload,
                    screening_policy=screening_policy,
                    policy=policy,
                )
                candidate_digest = str(candidate_payload["content_hash"])
                candidate_intent = {
                    "selected_index": selected_index,
                    "pair_hash": pair_hash,
                    "candidate_digest": candidate_digest,
                }
                persist_stage2_progress()
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                _fsync_directory(candidate_root)
                _atomic_write_json(candidate_path, candidate_payload)
                candidate_ledger.append(
                    {
                        "pair_hash": pair_hash,
                        "candidate_digest": candidate_digest,
                    }
                )
                candidate_intent = None
                persist_stage2_progress()
            else:
                stage2_candidate, candidate_digest = existing
            measured.append((stage2_candidate, candidate_digest))
            timeout_side = _runtime_timeout_side(stage2_candidate.evidence)
            if timeout_side is not None:
                timeout_records.append(
                    TimeoutQueueRecord(
                        entry_id=entry.entry_id,
                        pair_hash=pair_hash,
                        timeout_side=timeout_side,
                        stage="stage2",
                        evidence_digest=canonical_hash(
                            to_jsonable(asdict(stage2_candidate.evidence))
                        ),
                        graph_snapshot_hash=graph_snapshot_hash,
                        dbms_fingerprint=dbms_fingerprint,
                        dbms_health_epoch=dbms_health_epoch,
                        noise_floor_id=noise_floor_id,
                    )
                )

        promoted = [candidate for candidate, _digest in measured if candidate.decision.promote]
        if entry.stream is RuleStream.CONTROL:
            selected_candidates = select_control_validation_pairs(
                tuple(candidate for candidate, _digest in measured)
            )
            validation_limit = 2 if promoted else 1
        else:
            validation_limit = 5 if entry.phase is CampaignPhase.P2 else 2
            selected_candidates = tuple(
                sorted(promoted, key=_stage2_candidate_rank_key)[:validation_limit]
            )
        selected_for_validation = tuple(
            candidate.pair_hash for candidate in selected_candidates
        )
        measured_by_pair_hash = {
            candidate.pair_hash: (candidate, candidate_digest)
            for candidate, candidate_digest in measured
        }
        validation_outcomes = self._validate_stage2_pairs(
            entry,
            entry_dir,
            selected_candidates=tuple(
                measured_by_pair_hash[pair_hash]
                for pair_hash in selected_for_validation
            ),
            pairs_by_hash=pairs_by_hash,
            graph_statements=graph_statements,
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_version=dbms_version,
            driver_version=driver_version,
            noise_p99_ms=noise_p99_ms,
        )
        _persist_stage2_selection(
            selection_path,
            entry=entry,
            measured=tuple(measured),
            selected_pair_hashes=selected_for_validation,
            validation_limit=validation_limit,
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
            noise_floor_id=noise_floor_id,
            entry_payload=entry_payload,
            screening_policy=screening_policy,
        )
        pipeline_outcomes = tuple(
            outcome["pipeline_outcome"]
            for outcome in validation_outcomes
            if isinstance(outcome.get("pipeline_outcome"), Mapping)
        )
        passed_outcomes = tuple(
            outcome
            for outcome in validation_outcomes
            if outcome.get("stage") == PipelineStage.VALIDATION_PASSED.value
        )
        health_events = sum(
            session.get("health_event") is not None
            for outcome in validation_outcomes
            for session in outcome.get("sessions", ())
            if isinstance(session, Mapping)
        )
        best_delta = max(
            (float(outcome.get("pooled_delta_ms") or 0.0) for outcome in passed_outcomes),
            default=0.0,
        )
        best_ratio = max(
            (float(outcome.get("validated_ratio") or 1.0) for outcome in passed_outcomes),
            default=1.0,
        )
        best_ratio_ci_low = max(
            (float(outcome.get("pooled_ratio_ci_low") or 1.0) for outcome in passed_outcomes),
            default=1.0,
        )
        yield_successes = (
            len(passed_outcomes)
            if entry.stream is RuleStream.DISCOVERY
            else 0
        )
        validation_yield, validation_yield_ci_low = _validated_pair_yield(
            successes=yield_successes,
            accepted_pairs=accepted_pair_count,
        )
        return {
            "terminal_state": (
                EntryState.COMPLETED
                if self._candidate_pipeline is not None
                else EntryState.STAGE2_COMPLETE
            ),
            "stage1_selected_pairs": len(selected_hashes),
            "stage2_measured_pairs": len(measured),
            "stage2_promoted_pairs": (
                0 if entry.stream is RuleStream.CONTROL else len(promoted)
            ),
            "validation_selected_pairs": len(selected_for_validation),
            "validation_passed_pairs": (
                0
                if entry.stream is RuleStream.CONTROL
                else sum(
                    outcome["stage"] == PipelineStage.VALIDATION_PASSED.value
                    for outcome in validation_outcomes
                )
            ),
            "control_gate_hit": entry.stream is RuleStream.CONTROL
            and any(
                outcome.get("control_gate_hit") is True
                for outcome in validation_outcomes
            ),
            "control_validation_pass": entry.stream is RuleStream.CONTROL
            and any(
                outcome.get("control_validation_pass") is True
                for outcome in validation_outcomes
            ),
            "pipeline_terminal_candidates": len(pipeline_outcomes),
            "submit_ready_issues": sum(
                dict(outcome.get("reduction", {})).get("promotion") == "submit_ready"
                for outcome in pipeline_outcomes
            ),
            "cell_metrics": {
                "best_delta_over_noise": best_delta
                / max(noise_p99_ms, policy.normalization_epsilon_ms),
                "best_ratio_ci_low": best_ratio_ci_low,
                "best_validated_delta_ms": best_delta,
                "best_validated_ratio": best_ratio,
                "health_events": health_events,
                "timeout_rate": min(1.0, len(timeout_records) / max(1, len(records))),
                "validation_pair_yield": validation_yield,
                "validation_yield_ci_low": validation_yield_ci_low,
            },
            "timeout_records": tuple(timeout_records),
        }

    def _validate_stage2_pairs(
        self,
        entry: RulePlanEntry,
        entry_dir: Path,
        *,
        selected_candidates: tuple[tuple[Stage2Candidate, str], ...],
        pairs_by_hash: Mapping[str, object],
        graph_statements: tuple[str, ...],
        graph_snapshot_hash: str,
        dbms_fingerprint: str,
        dbms_version: str,
        driver_version: str,
        noise_p99_ms: float,
    ) -> tuple[dict[str, object], ...]:
        if not selected_candidates:
            return ()
        runner = None
        snapshot = GraphSnapshot.from_statements(graph_statements)
        if snapshot.content_hash != graph_snapshot_hash:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "validation graph statements do not match the Stage-2 fingerprint",
            )
        outcomes: list[dict[str, object]] = []
        for stage2_candidate, sealed_candidate_digest in selected_candidates:
            if not _is_sha256(sealed_candidate_digest):
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "validation requires a sealed Stage-2 candidate digest",
                )
            certified_pair = pairs_by_hash[stage2_candidate.pair_hash]
            pair = EquivalentQueryPair(
                certified_pair.query_a,
                certified_pair.query_b,
                certified_pair.parameters,
                graph_snapshot_hash,
                dbms_fingerprint,
                entry.rule_id,
                certified_pair.certificate,
            )
            candidate = CandidateBundle.create(
                pair,
                graph_snapshot=snapshot,
                dbms_version=dbms_version,
                resolved_config={
                    "entry_id": entry.entry_id,
                    "stream": entry.stream.value,
                    "target": entry.target.value,
                    "validation": dict(self.spec.validation),
                },
                discovery_slow_query=str(stage2_candidate.evidence.slow_query),
                discovery_ratio=stage2_candidate.evidence.median_ratio,
                certificate=_validation_certificate(pair),
                validation_policy=ValidationPolicy(
                    noise_floor_ms=noise_p99_ms,
                    timer_resolution_ms=0.001,
                    max_result_rows=int(self.spec.validation["max_result_rows"]),
                    query_timeout_seconds=float(entry.query_timeout_seconds),
                    session_timeout_seconds=max(
                        300.0,
                        float(entry.query_timeout_seconds) * 10.0,
                    ),
                ),
            )
            validation_path = (
                entry_dir / "validation" / f"{stage2_candidate.pair_hash}.json"
            )
            expected_identity = {
                "candidate_hash": candidate.candidate_hash,
                "dbms_fingerprint": dbms_fingerprint,
                "entry_identity": _stage2_entry_identity(entry),
                "graph_snapshot_hash": graph_snapshot_hash,
                "pair_hash": stage2_candidate.pair_hash,
                "stage2_evidence_digest": canonical_hash(
                    to_jsonable(asdict(stage2_candidate.evidence))
                ),
                "stage2_candidate_digest": sealed_candidate_digest,
                "stream": entry.stream.value,
            }
            existing_outcome = _load_stage2_validation_outcome(
                validation_path,
                expected_identity=expected_identity,
                candidate=candidate,
                expected_control_gate_hit=(
                    stage2_candidate.decision.promote
                    if entry.stream is RuleStream.CONTROL
                    else None
                ),
            )
            if existing_outcome is not None:
                report = replay_issue_validation_report(
                    candidate,
                    existing_outcome["report"],
                )
                pipeline_outcome = self._continue_validated_rule_candidate(
                    entry,
                    entry_dir,
                    candidate=candidate,
                    validation=report,
                    validation_path=validation_path,
                    stage2_candidate=stage2_candidate,
                    graph_statements=graph_statements,
                    driver_version=driver_version,
                )
                outcomes.append(
                    {
                        **existing_outcome,
                        **(
                            {"pipeline_outcome": pipeline_outcome}
                            if pipeline_outcome is not None
                            else {}
                        ),
                    }
                )
                continue
            if runner is None:
                if self._validation_runner_factory is not None:
                    runner = self._validation_runner_factory(entry)
                else:
                    assert isinstance(self.spec, ValidatedYieldExperimentSpec)
                    runner = SpawnedValidationSessionRunner(
                        _EntryValidationAdapterFactory(
                            self.spec,
                            self.environment,
                            entry,
                        )
                    )
            try:
                report = IssueValidator().validate(candidate, runner)
            except RuntimeError as exc:
                raise WorkerFailure(
                    ErrorKind.INFRASTRUCTURE_ERROR,
                    f"independent validation process failed: {exc}",
                ) from exc
            outcome: dict[str, object] = {
                "schema_version": 2,
                **expected_identity,
                "stage": report.stage.value,
                "reason_codes": list(report.reason_codes),
                "validated_ratio": report.validated_ratio,
                "pooled_delta_ms": report.pooled_delta_ms,
                "pooled_ratio_ci_low": report.pooled_ratio_ci_low,
                "pooled_delta_ci_low_ms": report.pooled_delta_ci_low_ms,
                "submission_path": (
                    report.submission_path.value
                    if report.submission_path is not None
                    else None
                ),
                "sessions": to_jsonable(asdict(report)["sessions"]),
                "report": to_jsonable(asdict(report)),
            }
            if entry.stream is RuleStream.CONTROL:
                outcome.update(
                    {
                        "control_gate_hit": stage2_candidate.decision.promote,
                        "control_validation_pass": (
                            report.stage is PipelineStage.VALIDATION_PASSED
                        ),
                    }
                )
            _persist_stage2_validation_outcome(
                validation_path,
                outcome,
            )
            pipeline_outcome = self._continue_validated_rule_candidate(
                entry,
                entry_dir,
                candidate=candidate,
                validation=report,
                validation_path=validation_path,
                stage2_candidate=stage2_candidate,
                graph_statements=graph_statements,
                driver_version=driver_version,
            )
            if pipeline_outcome is not None:
                outcome["pipeline_outcome"] = pipeline_outcome
            outcomes.append(outcome)
        return tuple(outcomes)

    def _continue_validated_rule_candidate(
        self,
        entry: RulePlanEntry,
        entry_dir: Path,
        *,
        candidate: CandidateBundle,
        validation: object,
        validation_path: Path,
        stage2_candidate: Stage2Candidate,
        graph_statements: tuple[str, ...],
        driver_version: str,
    ) -> dict[str, object] | None:
        if (
            self._candidate_pipeline is None
            or entry.stream is RuleStream.CONTROL
            or getattr(validation, "stage", None) is not PipelineStage.VALIDATION_PASSED
        ):
            return None
        store = ContentAddressedStore(
            entry_dir / "artifacts",
            budget_bytes=_task_artifact_budget_mb(self.spec, entry) * 1024 * 1024,
        )
        graph_ref = store.put(
            "graph_snapshot.cypher",
            _canonical_graph_payload(graph_statements),
            media_type="text/plain; charset=utf-8",
        )
        if graph_ref.content_hash != candidate.graph_snapshot.content_hash:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "validated candidate graph snapshot differs from its CAS artifact",
            )
        candidate_record = ArtifactRecord(
            level=ArtifactLevel.CANDIDATE,
            payload={
                "candidate_hash": candidate.candidate_hash,
                "content_hashes": dict(candidate.content_hashes),
                "pair_hash": stage2_candidate.pair_hash,
                "query_a": candidate.pair.query_a,
                "query_b": candidate.pair.query_b,
                "parameters": dict(candidate.pair.parameters),
                "rule_id": candidate.certificate.rule_id,
                "safety_certificate": asdict(candidate.certificate),
                "graph_snapshot_hash": candidate.graph_snapshot.content_hash,
                "graph_snapshot_ref": asdict(graph_ref),
                "dbms_fingerprint": candidate.pair.dbms_fingerprint,
                "discovery_median_ratio": candidate.discovery_ratio,
                "discovery_median_delta_ms": stage2_candidate.evidence.median_delta_ms,
                "discovery_slow_query": candidate.discovery_slow_query,
                "discovery_paired_timing": tuple(
                    asdict(sample) for sample in stage2_candidate.evidence.paired_samples
                ),
            },
        ).to_dict()
        candidate_ref = store.put_json(
            f"candidate-{candidate.candidate_hash}.json",
            candidate_record,
        )
        outcome_path = entry_dir / "candidate-outcomes" / f"{candidate.candidate_hash}.json"
        if outcome_path.is_file():
            return _load_terminal_candidate_outcome(
                outcome_path,
                store=store,
                candidate_hash=candidate.candidate_hash,
                candidate_ref=candidate_ref,
            )
        try:
            outcome = dict(
                self._candidate_pipeline.process_validated(
                    candidate,
                    validation,
                    task=entry,
                    artifact_store=store,
                    candidate_ref=candidate_ref,
                    driver_version=driver_version,
                    graph_snapshot_ref=asdict(graph_ref),
                    attempt_dir=self._attempt_dir or entry_dir,
                    validation_adapter_factory=_EntryValidationAdapterFactory(
                        self.spec,
                        self.environment,
                        entry,
                    ),
                    task8_validation_ref=_task8_validation_reference(
                        validation_path,
                        run_dir=self._run_dir,
                        validation=validation,
                    ),
                )
            )
        except WorkerFailure:
            raise
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                f"validated candidate pipeline failed: {type(exc).__name__}: {exc}",
            ) from exc
        validation_outcome = outcome.get("validation")
        reduction_outcome = outcome.get("reduction")
        if (
            outcome.get("candidate_hash") != candidate.candidate_hash
            or not isinstance(validation_outcome, Mapping)
            or validation_outcome.get("stage") != PipelineStage.VALIDATION_PASSED.value
            or not isinstance(reduction_outcome, Mapping)
            or reduction_outcome.get("status")
            not in {
                "not_applicable",
                "reduced_valid",
                "reduction_partial",
                "reduction_invalidated",
            }
        ):
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "validated candidate pipeline did not return terminal bound evidence",
            )
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            outcome_path,
            {
                **outcome,
                "candidate_ref": asdict(candidate_ref),
                "pair_hash": stage2_candidate.pair_hash,
            },
        )
        return outcome

    def _finish_entry(
        self,
        entry: RulePlanEntry,
        attempt_dir: Path,
        *,
        terminal_state: object,
        generated: GeneratedShard | None,
        accepted_hashes: tuple[str, ...] = (),
        applicability_count: int = 0,
        generated_seeds: int = 0,
        rejects: Mapping[str, int] | None = None,
        rule_stats: Mapping[str, object] | None = None,
        graph_manifest: GraphManifest | None = None,
        dbms_fingerprint: DBMSFingerprint | None = None,
        health_epoch: DBMSHealthEpoch | None = None,
        infrastructure_error_kind: object | None = None,
        infrastructure_reason: object | None = None,
        stage1_selected_pairs: int = 0,
        stage2_measured_pairs: int = 0,
        stage2_promoted_pairs: int = 0,
        validation_selected_pairs: int = 0,
        validation_passed_pairs: int = 0,
        pipeline_terminal_candidates: int = 0,
        submit_ready_issues: int = 0,
        cell_metrics: Mapping[str, object] | None = None,
        control_gate_hit: bool = False,
        control_validation_pass: bool = False,
    ) -> dict[str, object]:
        state = terminal_state if isinstance(terminal_state, EntryState) else EntryState(str(terminal_state))
        acceptance_complete = (
            state
            in {
                EntryState.STAGE1_COMPLETE,
                EntryState.STAGE2_COMPLETE,
                EntryState.VALIDATION_COMPLETE,
                EntryState.REDUCTION_COMPLETE,
                EntryState.MATERIALIZED,
                EntryState.COMPLETED,
            }
            and len(accepted_hashes) == entry.accepted_pair_quota
        )
        summary: dict[str, object] = {
            "accepted_pair_hashes": list(accepted_hashes),
            "accepted_pairs": len(accepted_hashes),
            "accepted_pair_quota": entry.accepted_pair_quota,
            "acceptance_generated_seeds": generated_seeds,
            "acceptance_invalidated": bool(
                accepted_hashes and state is EntryState.INFRASTRUCTURE_FAILED
            ),
            "acceptance_status": (
                "complete" if acceptance_complete else "incomplete"
            ),
            "acceptance_stop_reason": (
                "ACCEPTED_TARGET_REACHED"
                if acceptance_complete
                else state.value.upper()
            ),
            "entry_id": entry.entry_id,
            "entry_state": state.value,
            "family": entry.family,
            "generated_query_records": len(generated.query_texts) if generated is not None else 0,
            "generation": dict(generated.generator_metadata) if generated is not None else {},
            "graph_scale": entry.graph_scale,
            "reject_reasons": dict(sorted((rejects or {}).items())),
            "rule_id": entry.rule_id,
            "rule_stats": dict(rule_stats or {"accepted": {}, "not_applicable": {}}),
            "shard_index": entry.shard_index,
            "stream": entry.stream.value,
            "stage1_selected_pairs": stage1_selected_pairs,
            "stage2_measured_pairs": stage2_measured_pairs,
            "stage2_promoted_pairs": stage2_promoted_pairs,
            "validation_selected_pairs": validation_selected_pairs,
            "validation_passed_pairs": validation_passed_pairs,
            "pipeline_terminal_candidates": pipeline_terminal_candidates,
            "control_gate_hit": control_gate_hit,
            "control_validation_pass": control_validation_pass,
            "submit_ready_issues": submit_ready_issues,
            "target": entry.target.value,
            "terminal_state": state.value,
        }
        if (
            isinstance(applicability_count, bool)
            or not isinstance(applicability_count, int)
            or applicability_count < 0
            or applicability_count > generated_seeds + 1
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "applicability count is not bound to attempted query records",
            )
        typed_mismatches = int((rejects or {}).get("TYPED_EQUIVALENCE_FAILED", 0))
        evidence_complete = state is EntryState.COMPLETED and acceptance_complete
        pipeline_complete = (
            entry.stream is RuleStream.CONTROL
            or pipeline_terminal_candidates == validation_passed_pairs
        )
        summary.update(
            {
                "applicability_count": applicability_count,
                "artifact_complete": evidence_complete and pipeline_complete,
                "best_delta_over_noise": 0.0,
                "best_ratio_ci_low": 1.0,
                "best_validated_delta_ms": 0.0,
                "best_validated_ratio": 1.0,
                "certificate_complete": evidence_complete and typed_mismatches == 0,
                "health_events": 0,
                "timeout_rate": 0.0,
                "typed_mismatches": typed_mismatches,
                "validation_pair_yield": 0.0,
                "validation_yield_ci_low": 0.0,
            }
        )
        if cell_metrics:
            summary.update(dict(cell_metrics))
        if graph_manifest is not None:
            summary.update(
                {
                    "actual_nodes": graph_manifest.actual_nodes,
                    "graph_fingerprint": graph_manifest.graph_fingerprint,
                    "requested_max_nodes": graph_manifest.requested_max_nodes,
                }
            )
        if dbms_fingerprint is not None:
            summary["dbms_fingerprint"] = {
                "available": dbms_fingerprint.available,
                "reason": dbms_fingerprint.reason,
                "value": dbms_fingerprint.value,
            }
        if health_epoch is not None:
            summary["dbms_health_epoch"] = {
                "available": health_epoch.available,
                "reason": health_epoch.reason,
                "source": health_epoch.source,
                "value": health_epoch.value,
            }
        if infrastructure_reason:
            kind = (
                infrastructure_error_kind.value
                if isinstance(infrastructure_error_kind, ErrorKind)
                else str(infrastructure_error_kind or ErrorKind.INFRASTRUCTURE_ERROR.value)
            )
            summary["infrastructure_error_kind"] = kind
            summary["infrastructure_reason"] = sanitize_terminal_error(
                infrastructure_reason
            )
        safe_summary = sanitize_structure(summary)
        if not isinstance(safe_summary, dict):  # pragma: no cover - dict contract
            raise TypeError("entry summary sanitizer must return an object")
        _atomic_write_json(attempt_dir / "summary.json", safe_summary)
        return safe_summary

    def _run_schema_v2_compat(self, task: ShardTask) -> dict[str, object]:
        """Explicit historical adapter; schema-v3 never falls back through it."""

        if not isinstance(self.spec, ExperimentSpec):
            raise TypeError("schema-v2 ShardTask requires an ExperimentSpec")
        if self._run_dir is None:
            raise RuntimeError("shard worker was not bound to an immutable run directory")
        shard_dir = self._run_dir / "shards" / f"{task.target.value}-{task.shard_index:03d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = self._attempt_dir
        if attempt_dir is None:
            # Direct unit/integration use still gets an explicit attempt scope;
            # the formal supervisor always binds the numbered attempt itself.
            attempt_dir = shard_dir / "attempt-000"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            self._attempt_dir = attempt_dir
        shard_deadline = time.monotonic() + task.max_runtime_seconds
        generation_lease = self._attempt_context.generation_lease() if self._attempt_context else nullcontext()
        with generation_lease:
            context_task_identity = getattr(self._attempt_context, "task_identity", None)
            task_identity = context_task_identity or canonical_hash(to_jsonable(task))
            generation_path = shard_dir / "generation_corpus.json"
            generated = _load_generation_corpus(generation_path, task_identity=task_identity)
            reused = generated is not None
            if generated is None:
                generated = self._generation_runner(task, attempt_dir / "gdsmith_raw")
                _seal_generation_corpus(generation_path, task_identity=task_identity, generated=generated)
            _atomic_write_json(
                attempt_dir / "generation_reuse.json",
                {
                    "content_hash": canonical_hash(to_jsonable(generated)),
                    "generation_corpus": "../generation_corpus.json",
                    "reused": reused,
                    "task_identity": task_identity,
                },
            )
        self._transition_attempt(ShardState.LOADING)
        graph_hash = _graph_snapshot_hash(generated.graph_statements)
        timeout = float(self.spec.systems[task.target].default_timeout_seconds)
        measurement_lease = self._attempt_context.measurement_lease() if self._attempt_context else nullcontext()
        with measurement_lease:
            return self._measure_generated(task, shard_dir, attempt_dir, generated, graph_hash, timeout, shard_deadline)

    def _measure_generated(
        self,
        task: ShardTask,
        shard_dir: Path,
        attempt_dir: Path,
        generated: GeneratedShard,
        graph_hash: str,
        timeout: float,
        shard_deadline: float,
    ) -> dict[str, object]:
        artifact_store = ContentAddressedStore(
            shard_dir / "artifacts",
            budget_bytes=int(self.spec.artifacts["shard_budget_mb"]) * 1024 * 1024,
        )
        graph_reference = artifact_store.put(
            "graph_snapshot.cypher",
            _canonical_graph_payload(generated.graph_statements),
            media_type="text/plain; charset=utf-8",
        )
        graph_snapshot = GraphSnapshot.from_statements(generated.graph_statements)
        if graph_snapshot.content_hash != graph_hash or graph_reference.content_hash != graph_hash:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "graph snapshot canonical hash mismatch")
        load_adapter = self._adapter_factory(task)
        load_proxy = AdapterQueryProxy(
            load_adapter,
            attempt_dir=attempt_dir,
            graph_hash=graph_hash,
            dbms_fingerprint="unverified",
            default_timeout_seconds=timeout,
            on_error=lambda kind, inflight: self._record_query_failure(kind, inflight, attempt_dir),
        )
        try:
            self._reset_and_load(load_proxy, generated.graph_statements)
            graph_manifest: GraphManifest | None = None
            if task.target is TargetSystem.NEO4J:
                try:
                    requested_max_nodes = _requested_max_nodes(task, generated)
                    if requested_max_nodes is not None:
                        graph_manifest = read_neo4j_graph_manifest(
                            load_proxy,
                            requested_max_nodes=requested_max_nodes,
                            statements=generated.graph_statements,
                        )
                except (TypeError, ValueError) as exc:
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        f"graph manifest read failed: {exc}",
                    ) from exc
                if graph_manifest is not None and graph_manifest.graph_fingerprint != graph_hash:
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        "graph manifest fingerprint does not match the sealed graph snapshot",
                    )
                if graph_manifest is not None:
                    _atomic_write_json(attempt_dir / "graph_manifest.json", asdict(graph_manifest))
            graph_fingerprint = graph_manifest.graph_fingerprint if graph_manifest is not None else graph_hash
            fingerprint = _adapter_dbms_fingerprint(load_proxy)
            health_epoch = _adapter_dbms_health_epoch(load_proxy)
            if fingerprint.available and health_epoch.available:
                assert fingerprint.value is not None
                load_proxy.update_dbms_fingerprint(fingerprint.value)
                self._transition_attempt(ShardState.BUILDING_ACCEPTED_PAIRS)
                accepted, acceptance, accepted_hashes, rule_stats = self._build_accepted_pairs(
                    load_proxy,
                    task,
                    generated.query_texts,
                    shard_dir,
                    graph_snapshot_hash=graph_fingerprint,
                    dbms_fingerprint=fingerprint.value,
                    max_seconds=max(0.0, shard_deadline - time.monotonic()),
                )
                rejects = Counter(acceptance.reject_counts)
            else:
                accepted = []
                accepted_hashes = set()
                reason = "DBMS_FINGERPRINT_UNAVAILABLE" if not fingerprint.available else "DBMS_HEALTH_EPOCH_UNAVAILABLE"
                rejects = Counter({reason: 1})
                acceptance = AcceptedPairCorpus(
                    (), 0, rejects, AcceptanceStatus.INCOMPLETE, reason
                )
                rule_stats = {"accepted": {}, "not_applicable": {}}
                _ensure_jsonl(shard_dir / "accepted_pairs.jsonl")
        finally:
            load_adapter.close()

        candidate_hashes: set[str] = set()
        candidates = 0
        pipeline_outcomes: list[dict[str, object]] = []
        if fingerprint.available and health_epoch.available and acceptance.status is AcceptanceStatus.COMPLETE:
            self._transition_attempt(ShardState.SCREENING)
            measurement_adapter = self._adapter_factory(task)
            measurement_proxy = AdapterQueryProxy(
                measurement_adapter,
                attempt_dir=attempt_dir,
                graph_hash=graph_hash,
                dbms_fingerprint="unverified",
                default_timeout_seconds=timeout,
                on_error=lambda kind, inflight: self._record_query_failure(kind, inflight, attempt_dir),
            )
            try:
                connected = measurement_proxy.connectivity_check()
                if not isinstance(connected, tuple) or not connected or connected[0] is not True:
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        "measurement adapter connectivity check failed",
                    )
                measurement_fingerprint = _adapter_dbms_fingerprint(measurement_proxy)
                measurement_epoch = _adapter_dbms_health_epoch(measurement_proxy)
                self._require_same_health_identity(
                    fingerprint,
                    measurement_fingerprint,
                    health_epoch,
                    measurement_epoch,
                    attempt_dir,
                )
                assert measurement_fingerprint.value is not None
                measurement_proxy.update_dbms_fingerprint(measurement_fingerprint.value)
                candidate_records, pipeline_outcomes = self._screen_pairs(
                    measurement_proxy,
                    task,
                    accepted,
                    artifact_store=artifact_store,
                    graph_snapshot=graph_snapshot,
                    graph_snapshot_ref=asdict(graph_reference),
                    dbms_version=measurement_fingerprint.server_version,
                    attempt_dir=attempt_dir,
                )
                final_fingerprint = _adapter_dbms_fingerprint(measurement_proxy)
                final_epoch = _adapter_dbms_health_epoch(measurement_proxy)
                self._require_same_health_identity(
                    fingerprint,
                    final_fingerprint,
                    health_epoch,
                    final_epoch,
                    attempt_dir,
                )
                candidate_path = shard_dir / "candidates.jsonl"
                existing_candidates = _jsonl_hashes(candidate_path, "candidate_hash")
                for record in candidate_records:
                    candidate_hash = str(record["candidate_hash"])
                    if candidate_hash in existing_candidates:
                        continue
                    _append_jsonl(candidate_path, record)
                    existing_candidates.add(candidate_hash)
                candidate_hashes = existing_candidates
                candidates = len(candidate_hashes)
            finally:
                measurement_adapter.close()
        if self._candidate_pipeline is not None:
            if len(pipeline_outcomes) != candidates:
                raise WorkerFailure(
                    ErrorKind.INFRASTRUCTURE_ERROR,
                    "not every runtime candidate reached a terminal pipeline outcome",
                )
            validation_path = attempt_dir / "validation.json"
            reduction_path = attempt_dir / "reduction.json"
            validation_outcomes = [dict(outcome["validation"]) for outcome in pipeline_outcomes]
            reduction_outcomes = [dict(outcome["reduction"]) for outcome in pipeline_outcomes]
            if any(
                outcome.get("stage") not in {"validation_passed", "rejected", "inconclusive", "invalid"}
                for outcome in validation_outcomes
            ):
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "candidate validation outcome is not terminal")
            if any(
                outcome.get("status")
                not in {"not_applicable", "reduced_valid", "reduction_partial", "reduction_invalidated"}
                for outcome in reduction_outcomes
            ):
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "candidate reduction outcome is not terminal")
            _atomic_write_json(
                validation_path,
                {
                    "candidate_count": candidates,
                    "terminal_count": len(validation_outcomes),
                    "outcomes": validation_outcomes,
                },
            )
            _atomic_write_json(
                reduction_path,
                {
                    "candidate_count": candidates,
                    "terminal_count": len(reduction_outcomes),
                    "outcomes": reduction_outcomes,
                },
            )
        summary = {
            "target": task.target.value,
            "shard_index": task.shard_index,
            "generation": dict(generated.generator_metadata),
            "generated_query_records": len(generated.query_texts),
            "accepted_pairs": len(accepted_hashes),
            "acceptance_status": acceptance.status.value,
            "acceptance_stop_reason": acceptance.stop_reason,
            "acceptance_generated_seeds": acceptance.generated_seeds,
            "accepted_pair_hashes": sorted(accepted_hashes),
            "screened_candidates": candidates,
            "candidate_hashes": sorted(candidate_hashes),
            "reject_reasons": dict(sorted(rejects.items())),
            "rule_stats": rule_stats,
            "graph_snapshot_ref": asdict(graph_reference),
            "artifact_used_bytes": artifact_store.used_bytes,
            "dbms_fingerprint": {
                "available": fingerprint.available,
                "value": fingerprint.value,
                "reason": fingerprint.reason,
            },
            "dbms_health_epoch": {
                "available": health_epoch.available,
                "value": health_epoch.value,
                "source": health_epoch.source,
                "reason": health_epoch.reason,
            },
        }
        if graph_manifest is not None:
            summary.update(
                {
                    "requested_max_nodes": graph_manifest.requested_max_nodes,
                    "actual_nodes": graph_manifest.actual_nodes,
                    "graph_fingerprint": graph_manifest.graph_fingerprint,
                }
            )
        screening_path = attempt_dir / "screening.json"
        _atomic_write_json(
            screening_path,
            {
                "accepted_pair_hashes": sorted(accepted_hashes),
                "candidate_hashes": sorted(candidate_hashes),
                "dbms_fingerprint": fingerprint.value,
                "dbms_health_epoch": health_epoch.value,
            },
        )
        summary["stage_evidence"] = {
            "screening": {
                "path": str(screening_path.relative_to(shard_dir)),
                "sha256": hashlib.sha256(screening_path.read_bytes()).hexdigest(),
            }
        }
        if self._candidate_pipeline is not None:
            summary["stage_evidence"].update(
                {
                    "validation": {
                        "path": str(validation_path.relative_to(shard_dir)),
                        "sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
                    },
                    "reduction": {
                        "path": str(reduction_path.relative_to(shard_dir)),
                        "sha256": hashlib.sha256(reduction_path.read_bytes()).hexdigest(),
                    },
                }
            )
        safe_summary = sanitize_structure(summary)
        if not isinstance(safe_summary, dict):  # pragma: no cover - dict contract
            raise TypeError("shard summary sanitizer must return an object")
        _atomic_write_json(attempt_dir / "summary.json", safe_summary)
        budget_bytes = int(self.spec.artifacts["shard_budget_mb"]) * 1024 * 1024
        budget_report = enforce_shard_budget(
            shard_dir,
            budget_bytes=budget_bytes,
            nonessential_paths=tuple(sorted(shard_dir.glob("attempt-*/gdsmith_raw"))),
            reserve_bytes=min(1024 * 1024, budget_bytes // 100),
        )
        if budget_report.warnings:
            for warning in budget_report.warnings:
                _append_jsonl(shard_dir / "artifact_warnings.jsonl", {"warning": warning})
        summary["shard_artifact_bytes"] = budget_report.used_bytes
        summary["artifact_warnings"] = budget_report.warnings
        safe_summary = sanitize_structure(summary)
        if not isinstance(safe_summary, dict):  # pragma: no cover - dict contract
            raise TypeError("shard summary sanitizer must return an object")
        _atomic_write_json(attempt_dir / "summary.json", safe_summary)
        return safe_summary

    def _transition_attempt(self, state: ShardState) -> None:
        transition_attempt = getattr(self._attempt_context, "transition", None)
        if callable(transition_attempt):
            transition_attempt(state)

    def _record_query_failure(
        self,
        kind: ErrorKind,
        inflight: Mapping[str, object],
        attempt_dir: Path,
    ) -> None:
        if self._run_dir is None:
            return
        ReliabilityFindingStore(self._run_dir).record(
            kind=kind,
            query_hash=str(inflight.get("query_hash", "")),
            graph_hash=str(inflight.get("graph_hash", "")),
            dbms_fingerprint=str(inflight.get("dbms_fingerprint", "")),
            attempt_evidence=str(attempt_dir.relative_to(self._run_dir)),
        )

    @staticmethod
    def _require_same_health_identity(
        expected_fingerprint: DBMSFingerprint,
        actual_fingerprint: DBMSFingerprint,
        expected_epoch: DBMSHealthEpoch,
        actual_epoch: DBMSHealthEpoch,
        attempt_dir: Path,
    ) -> None:
        if (
            expected_fingerprint.available
            and actual_fingerprint.available
            and expected_fingerprint.value == actual_fingerprint.value
            and expected_epoch.available
            and actual_epoch.available
            and expected_epoch.value == actual_epoch.value
        ):
            return
        _atomic_write_json(
            attempt_dir / "runtime_invalidated.json",
            sanitize_structure({
                "after": actual_fingerprint.value,
                "before": expected_fingerprint.value,
                "fingerprint_after": actual_fingerprint.value,
                "fingerprint_before": expected_fingerprint.value,
                "health_epoch_after": actual_epoch.value,
                "health_epoch_before": expected_epoch.value,
                "reason": (
                    actual_fingerprint.reason
                    or actual_epoch.reason
                    or "dbms_fingerprint_or_health_epoch_changed"
                ),
            }),
        )
        raise WorkerFailure(ErrorKind.DBMS_CRASHED, "DBMS fingerprint or boot health epoch changed during shard execution")

    def _reset_and_load(self, adapter: Any, graph_statements: tuple[str, ...]) -> None:
        # A shard can only start from a known empty graph.  Failure to reset is
        # fatal: continuing would combine this shard with stale graph state.
        reset_graph_state(adapter, query_kind="shard_reset")
        reset_schema = getattr(adapter, "reset_schema", None)
        if callable(reset_schema):
            reset_schema()
        ensure_schema = getattr(adapter, "ensure_schema", None)
        if callable(ensure_schema):
            ensure_schema()
        adapter.load_graph(GraphCorpus(tuple(graph_statements)))

    def _build_accepted_pairs(
        self,
        adapter: Any,
        task: ShardTask,
        queries: tuple[str, ...],
        shard_dir: Path,
        *,
        graph_snapshot_hash: str,
        dbms_fingerprint: str,
        max_seconds: float,
    ) -> tuple[list[EquivalentQueryPair], AcceptedPairCorpus, set[str], dict[str, object]]:
        accepted: list[EquivalentQueryPair] = []
        max_rows = int(self.spec.validation["max_result_rows"])
        default_timeout = self.spec.systems[task.target].default_timeout_seconds
        acceptance_deadline = time.monotonic() + max_seconds
        records_path = shard_dir / "accepted_pairs.jsonl"
        _bind_accepted_identity(
            shard_dir / "accepted_identity.json",
            records_path=records_path,
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
        )
        _ensure_jsonl(records_path)
        seen = _accepted_pair_hashes(records_path)
        seen_source_asts = _accepted_source_ast_counts(records_path)
        timeout_query_hashes = _bound_query_timeout_hashes(
            shard_dir / "query_timeout_quarantine.jsonl",
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
        )
        materialized: set[str] = set()

        dialect = _dialect_for(task.target)
        capability_hash = _dialect_capability_hash(dialect.capabilities)

        if self._run_dir is None:
            raise RuntimeError("rule provider requires a bound run directory")
        provider = CoreRulePairProvider(
            queries,
            dbms=task.target.value,
            shard_index=task.shard_index,
            capability_hash=capability_hash,
            exploration_floor=int(
                self.spec.rules_catalog["tiers"]["core"]["minimum_accepted_pairs_per_rule"]
            ),
            epoch_size=int(self.spec.rules["yield_update_epoch_accepted_pairs"]),
            quarantine_path=shard_dir / "rule_quarantine.jsonl",
            capabilities=frozenset(self.spec.systems[task.target].capabilities),
            validated_yield_path=self._run_dir / "validated_yield" / f"{task.target.value}.jsonl",
        )

        def validator(proposal: PairProposal) -> PairValidation:
            base = proposal.query_a.text
            transformed = proposal.query_b.text
            decision = dialect.check(proposal.query_a)
            transformed_decision = dialect.check(proposal.query_b)
            if decision.kind is DecisionKind.REJECT:
                return PairValidation(False, f"DIALECT_{decision.reason_code.value}")
            if transformed_decision.kind is DecisionKind.REJECT:
                return PairValidation(False, f"DIALECT_{transformed_decision.reason_code.value}")
            replayable = getattr(adapter, "gdsmith_query_is_replayable", gdsmith_read_query_is_replayable)
            if not replayable(base) or not replayable(transformed):
                return PairValidation(False, "DIALECT_REPLAY_REJECT")
            pair_hash = proposal.pair_hash
            source_ast_hash = proposal.query_a.ast_hash
            if pair_hash in seen:
                if pair_hash not in materialized:
                    accepted.append(
                        EquivalentQueryPair(
                            base,
                            transformed,
                            {},
                            graph_snapshot_hash,
                            dbms_fingerprint,
                            proposal.rule_id,
                            proposal.safety_certificate,
                        )
                    )
                    materialized.add(pair_hash)
                    provider.record_accepted(proposal.rule_id, pair_hash)
                return PairValidation(True, "RESUME_EXISTING", {"pair_hash": pair_hash})
            if seen_source_asts[source_ast_hash] >= self.spec.workload.max_variants_per_seed:
                return PairValidation(False, "MAX_VARIANTS_PER_SEED_REACHED")
            if len(seen) >= task.accepted_query_pairs:
                return PairValidation(False, "ACCEPTED_TARGET_ALREADY_REACHED")
            if _query_hash(base) in timeout_query_hashes or _query_hash(transformed) in timeout_query_hashes:
                return PairValidation(False, "QUERY_TIMEOUT_QUARANTINED")
            remaining = acceptance_deadline - time.monotonic()
            if remaining <= 1.0:
                return PairValidation(False, "TIME_BUDGET_EXHAUSTED")
            try:
                left = adapter.execute(
                    base,
                    {},
                    "acceptance_base",
                    max_rows=max_rows,
                    timeout_seconds=min(default_timeout, max(0.1, remaining / 2.0)),
                )
                remaining = acceptance_deadline - time.monotonic()
                if remaining <= 0.1:
                    return PairValidation(False, "TIME_BUDGET_EXHAUSTED")
                right = adapter.execute(
                    transformed,
                    {},
                    "acceptance_transformed",
                    max_rows=max_rows,
                    timeout_seconds=min(default_timeout, remaining),
                )
            except Exception:
                return PairValidation(False, "QUERY_EXECUTION_ERROR")
            if _is_truncated(left, max_rows) or _is_truncated(right, max_rows):
                return PairValidation(False, "RESULT_TRUNCATED")
            equivalence = compare_typed_results(left.rows, right.rows, ordered=_has_order_by(base))
            if not equivalence.equivalent:
                if not equivalence.complete_result:
                    reason = equivalence.reason_codes[0] if equivalence.reason_codes else "typed_equivalence_incomplete"
                    return PairValidation(False, reason.upper())
                provider.quarantine_mismatch(proposal, reason="TYPED_EQUIVALENCE_FAILED")
                return PairValidation(False, "TYPED_EQUIVALENCE_FAILED")
            if self._run_dir is None:
                raise RuntimeError("accepted corpus registry requires a bound run directory")
            duplicate_reason = AcceptedCorpusRegistry(self._run_dir / "accepted_corpus_registry.jsonl").reserve(
                source_ast_hash=source_ast_hash,
                pair_hash=pair_hash,
                target=task.target.value,
                shard_index=task.shard_index,
            )
            if duplicate_reason is not None:
                return PairValidation(False, duplicate_reason)
            seen.add(pair_hash)
            seen_source_asts[source_ast_hash] += 1
            accepted_pair = EquivalentQueryPair(
                base,
                transformed,
                {},
                graph_snapshot_hash,
                dbms_fingerprint,
                proposal.rule_id,
                proposal.safety_certificate,
            )
            accepted.append(accepted_pair)
            provider.record_accepted(proposal.rule_id, pair_hash)
            materialized.add(pair_hash)
            _append_jsonl(
                records_path,
                ArtifactRecord(
                    level=ArtifactLevel.MINIMAL,
                    payload={
                        "pair_hash": pair_hash,
                        "query_hash": source_ast_hash,
                        "rule_id": proposal.rule_id,
                        "site": proposal.safety_certificate.ast_path,
                        "equivalence": "complete_typed_equivalent",
                    },
                ).to_dict(),
            )
            return PairValidation(
                True,
                "COMPLETE_TYPED_EQUIVALENT",
                {"pair_hash": pair_hash, "safety_proof_id": proposal.safety_certificate.proof_id},
            )

        if max_seconds <= 0:
            corpus = AcceptedPairCorpus((), 0, {}, AcceptanceStatus.INCOMPLETE, "TIME_BUDGET_EXHAUSTED")
        else:
            corpus = AcceptedPairBuilder(provider, validator).build(
                AcceptedPairRequest(
                    accepted_pairs=task.accepted_query_pairs,
                    max_generated_seeds=task.max_generated_seeds,
                    max_variants_per_seed=self.spec.workload.max_variants_per_seed,
                    max_seconds=min(max_seconds, 6 * 3600),
                    experiment_seed=task.seed,
                    target=task.target.value,
                    shard_index=task.shard_index,
                )
            )
        return accepted, corpus, seen, {
            "accepted": provider.accepted_counts,
            "not_applicable": provider.not_applicable_counts,
            "yield_snapshot_hash": provider.yield_snapshot_hash,
        }

    def _screen_pairs(
        self,
        adapter: Any,
        task: ShardTask,
        pairs: list[EquivalentQueryPair],
        *,
        artifact_store: ContentAddressedStore,
        graph_snapshot: GraphSnapshot,
        graph_snapshot_ref: Mapping[str, object],
        dbms_version: str | None,
        attempt_dir: Path,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        policy_config = self.spec.runtime["screen"]
        policy = RuntimePolicy(
            warmups=int(policy_config["warmups"]),
            pairs=int(policy_config["pairs"]),
            seed=task.seed,
            require_exact_balance=False,
            timeout_ms=float(self.spec.systems[task.target].default_timeout_seconds * 1000),
        )
        threshold = float(policy_config["ratio"])
        harness = RuntimeHarness()
        candidates: list[dict[str, object]] = []
        pipeline_outcomes: list[dict[str, object]] = []
        terminal_by_pair = _terminal_candidate_outcomes_by_pair(
            artifact_store.root.parent / "candidate-outcomes",
            store=artifact_store,
        )
        noncandidate_path = artifact_store.root.parent / "screened_non_candidates.jsonl"
        screened_non_candidates = _jsonl_hashes(noncandidate_path, "pair_hash")
        for index, pair in enumerate(pairs):
            pair_identity = _pair_hash(pair.query_a, pair.query_b)
            resumed = terminal_by_pair.get(pair_identity)
            if resumed is not None:
                resumed_record, resumed_outcome = resumed
                candidates.append(resumed_record)
                pipeline_outcomes.append(resumed_outcome)
                continue
            if pair_identity in screened_non_candidates:
                continue
            evidence = harness.measure_discovery(
                adapter,
                pair,
                policy=RuntimePolicy(**{**policy.__dict__, "seed": task.seed + index}),
            )
            if evidence.verdict is not RuntimeVerdict.PASS or evidence.median_ratio is None or evidence.median_ratio < threshold:
                _append_jsonl(
                    noncandidate_path,
                    {
                        "pair_hash": pair_identity,
                        "reason_codes": list(evidence.reason_codes),
                        "status": "not_candidate",
                    },
                )
                screened_non_candidates.add(pair_identity)
                continue
            if not dbms_version:
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "candidate DBMS version is unavailable")
            certificate = _validation_certificate(pair)
            bundle = CandidateBundle.create(
                pair,
                graph_snapshot=graph_snapshot,
                dbms_version=dbms_version,
                resolved_config=_candidate_resolved_config(self.spec, task),
                discovery_slow_query=str(evidence.slow_query),
                discovery_ratio=evidence.median_ratio,
                certificate=certificate,
                validation_policy=_candidate_validation_policy(self.spec, task),
            )
            if bundle.verify_integrity():
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "candidate content hash binding failed")
            candidate_record = ArtifactRecord(
                    level=ArtifactLevel.CANDIDATE,
                    payload={
                    "candidate_hash": bundle.candidate_hash,
                    "content_hashes": dict(bundle.content_hashes),
                    "pair_hash": pair_identity,
                    "query_a": pair.query_a,
                    "query_b": pair.query_b,
                    "parameters": dict(pair.parameters),
                    "rule_id": pair.rule_id,
                    "safety_certificate": asdict(certificate),
                    "graph_snapshot_hash": pair.graph_snapshot_hash,
                    "graph_snapshot_ref": dict(graph_snapshot_ref),
                    "dbms_fingerprint": pair.dbms_fingerprint,
                    "discovery_median_ratio": evidence.median_ratio,
                    "discovery_median_delta_ms": evidence.median_delta_ms,
                    "discovery_slow_query": evidence.slow_query,
                    "discovery_paired_timing": tuple(asdict(sample) for sample in evidence.paired_samples),
                    },
                ).to_dict()
            record_ref = artifact_store.put_json(
                f"candidate-{bundle.candidate_hash}.json",
                candidate_record,
            )
            candidate_record["record_ref"] = asdict(record_ref)
            candidates.append(candidate_record)
            if self._candidate_pipeline is not None:
                outcome_path = artifact_store.root.parent / "candidate-outcomes" / f"{bundle.candidate_hash}.json"
                if outcome_path.is_file():
                    outcome = _load_terminal_candidate_outcome(
                        outcome_path,
                        store=artifact_store,
                        candidate_hash=bundle.candidate_hash,
                        candidate_ref=record_ref,
                    )
                else:
                    outcome = dict(
                        self._candidate_pipeline.process(
                            bundle,
                            task=task,
                            artifact_store=artifact_store,
                            candidate_ref=record_ref,
                            driver_version=dbms_version and getattr(adapter, "dbms_fingerprint")().driver_version,
                            graph_snapshot_ref=dict(graph_snapshot_ref),
                            attempt_dir=attempt_dir,
                        )
                    )
                if outcome.get("candidate_hash") != bundle.candidate_hash:
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        "candidate pipeline outcome is not bound to its candidate",
                    )
                validation = outcome.get("validation")
                reduction = outcome.get("reduction")
                if not isinstance(validation, Mapping) or not isinstance(reduction, Mapping):
                    raise WorkerFailure(
                        ErrorKind.INFRASTRUCTURE_ERROR,
                        "candidate pipeline did not return terminal validation and reduction outcomes",
                    )
                pipeline_outcomes.append(outcome)
                if not outcome_path.is_file():
                    outcome_path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(
                        outcome_path,
                        {
                            **outcome,
                            "candidate_ref": asdict(record_ref),
                            "pair_hash": pair_identity,
                        },
                    )
        return candidates, pipeline_outcomes

    def _create_adapter(self, task: ShardTask | RulePlanEntry) -> Any:
        if isinstance(task, RulePlanEntry):
            if not isinstance(self.spec, ValidatedYieldExperimentSpec):
                raise TypeError("RulePlanEntry adapter creation requires a schema-v3 spec")
            run_config = _entry_run_config(self.spec, task)
        elif isinstance(task, ShardTask):
            if not isinstance(self.spec, ExperimentSpec):
                raise TypeError("ShardTask adapter creation requires a schema-v2 spec")
            run_config = migrate_experiment_spec_to_run_config(self.spec, task.target)
        else:
            raise TypeError("adapter task must be RulePlanEntry or ShardTask")
        system = run_config.systems[0]
        return create_adapter(system, run_config, self.environment, allow_create=True)

    def _generate_with_gdsmith(self, task: ShardTask | RulePlanEntry, raw_dir: Path) -> GeneratedShard:
        if isinstance(task, RulePlanEntry):
            target = task.target
            seed = task.shard_seed
            accepted_pair_quota = task.accepted_pair_quota
            max_runtime_seconds = task.max_entry_runtime_seconds
            planned_generation_backend = self.spec.systems[target].generation_backend
            max_node_num = task.graph_scale
        elif isinstance(task, ShardTask):
            target = task.target
            seed = task.seed
            accepted_pair_quota = task.accepted_query_pairs
            max_runtime_seconds = task.max_runtime_seconds
            planned_generation_backend = task.generation_backend
            max_node_num = GdsmithConfig().max_node_num
        else:
            raise TypeError("GDsmith generation task must be ShardTask or RulePlanEntry")

        backend, host, port, username, password = _gdsmith_connection(target, self.environment)
        provenance = _generation_backend_provenance(target, backend)
        if planned_generation_backend not in {"unspecified", provenance}:
            raise RuntimeError(
                f"planned generation backend {planned_generation_backend!r} does not match runtime {provenance!r}"
            )
        requires_single_label = "single_node_label" in self.spec.systems[target].capabilities
        generation_query_limit = _generation_query_limit(task, self.spec)
        result_holder: dict[str, object] = {}

        def invoke(request: GenerationRequest) -> tuple[str, ...]:
            # Leave a 30-second supervisor margin so timeout is emitted as an
            # auditable partial corpus instead of a killed worker process.
            timeout_seconds = max(1.0, min(float(max_runtime_seconds) - 300.0, 6 * 3600.0))
            base_config = GdsmithConfig(
                expected_source_commit=self.spec.generation.expected_commit,
                compatibility_profile="neo4j" if self.spec.schema_version == 3 else "all",
                algorithm=self.spec.generation.algorithm.lower(),
                backend_extra_args=(
                    {backend: ("--label-num", "1")}
                    if requires_single_label
                    else {}
                ),
                timeout_seconds=timeout_seconds,
                max_raw_artifact_bytes=min(
                    350 * 1024 * 1024,
                    _task_artifact_budget_mb(self.spec, task) * 1024 * 1024 * 7 // 10,
                ),
            )
            result = run_gdsmith(
                config=replace(base_config, max_node_num=max_node_num),
                env=self.environment,
                raw_dir=raw_dir,
                backend=backend,
                host=host,
                port=port,
                username=username,
                password=password,
                num_queries=request.limit,
                seed=request.seed,
            )
            result_holder["result"] = result
            artifact_budget_reached = bool(getattr(result, "artifact_budget_exceeded", False))
            fatal_markers = set(getattr(result, "fatal_stderr_markers", ()))
            unrecoverable_markers = fatal_markers - {"DatabaseConnectionLost"}
            process_returncode = getattr(result, "process_returncode", None)
            query_record_limit_reached = (
                result.returncode == 1
                and bool(getattr(result, "query_record_limit_reached", False))
                and not unrecoverable_markers
                and (
                    process_returncode == 1
                    or (process_returncode == 0 and fatal_markers == {"DatabaseConnectionLost"})
                )
            )
            result_holder["query_record_limit_reached"] = query_record_limit_reached
            partial_corpus = result.returncode == 124 or (
                result.returncode == 125 and artifact_budget_reached
            )
            replayable_nonzero_exit = partial_corpus or query_record_limit_reached
            if result.returncode != 0 and not replayable_nonzero_exit:
                reason = "raw artifact budget exceeded" if artifact_budget_reached else "process failure"
                raise WorkerFailure(
                    ErrorKind.INFRASTRUCTURE_ERROR,
                    f"GDsmith failed with code {result.returncode}: {reason}",
                )
            if replayable_nonzero_exit and not result.parsed.accepted_queries:
                raise WorkerFailure(
                    ErrorKind.INFRASTRUCTURE_ERROR,
                    "GDsmith stopped without a replayable query",
                )
            return tuple(record.statement for record in result.parsed.accepted_queries)

        corpus = GdsmithGenerator(invoke).generate(
            GenerationRequest(seed=seed, limit=generation_query_limit)
        )
        result = result_holder["result"]
        if bool(getattr(result, "generation_stalled", False)) and len(corpus.queries) < accepted_pair_quota:
            raise WorkerFailure(
                ErrorKind.WORKER_HUNG,
                (
                    f"GDsmith stalled after {len(corpus.queries)} query records; "
                    f"cannot cover {accepted_pair_quota} accepted pairs"
                ),
            )
        return GeneratedShard(
            graph_statements=result.parsed.graph_statements,
            query_texts=tuple(query.text for query in corpus.queries),
            generator_metadata={
                "backend": backend,
                "generation_backend": provenance,
                "generation_source": "gdsmith",
                "generation_seed": seed,
                "generated_query_record_limit": generation_query_limit,
                "requested_max_nodes": max_node_num,
                "gdsmith_returncode": result.returncode,
                "duplicate_ast_records": len(corpus.duplicate_ast_hashes),
                "generation_stop_reason": (
                    "RAW_ARTIFACT_BUDGET_REACHED"
                    if bool(getattr(result, "artifact_budget_exceeded", False))
                    else "NO_PROGRESS_TIMEOUT"
                    if result.returncode == 124 and result.generation_stalled
                    else "TIME_BUDGET_EXHAUSTED"
                    if result.returncode == 124
                    else "QUERY_RECORD_LIMIT_REACHED"
                    if bool(result_holder.get("query_record_limit_reached", False))
                    else "PROCESS_COMPLETED"
                ),
            },
        )


class FormalRuntimeFirstShardWorker(RuntimeFirstShardWorker):
    """The production worker, with validation and reduction always enabled."""

    def bind_run_dir(self, run_dir: str | Path) -> None:
        super().bind_run_dir(run_dir)
        from graph_sym_perf.experiment.candidate_pipeline import ProductionCandidatePipeline

        self._candidate_pipeline = ProductionCandidatePipeline(
            self.spec,
            environment=self.environment,
            run_dir=run_dir,
        )


def _entry_run_config(
    spec: ValidatedYieldExperimentSpec,
    entry: RulePlanEntry,
) -> RunConfig:
    definition = spec.systems.get(entry.target)
    if definition is None or entry.target not in spec.targets:
        raise ValueError("RulePlanEntry target is not part of the schema-v3 spec")
    optional_env = (
        {"database": "GRAPHSYMPERF_NEO4J_DATABASE"}
        if entry.target is TargetSystem.NEO4J
        else {}
    )
    system = SystemConfig(
        target=entry.target,
        required_env=definition.required_env,
        optional_env=optional_env,
        settings={},
    )
    stage1 = spec.runtime["stage1"]
    stage2 = spec.runtime["stage2"]
    family_name = "bool_redundancy" if entry.family == "bool" else "symmetry"
    return RunConfig(
        source_path=str(spec.source_path),
        paths=RunPaths(),
        systems=(system,),
        dataset=DatasetConfig(seed=entry.shard_seed),
        query_generation=QueryGenerationConfig(
            max_cases=entry.max_generated_seed_budget,
            source=spec.generation.backend,
            gdsmith=GdsmithConfig(
                expected_source_commit=spec.generation.expected_commit,
                compatibility_profile="neo4j" if spec.schema_version == 3 else "all",
                algorithm=spec.generation.algorithm.lower(),
                num_queries=entry.max_generated_seed_budget,
                random_seed=entry.shard_seed,
                timeout_seconds=float(entry.max_entry_runtime_seconds),
                max_node_num=entry.graph_scale,
            ),
        ),
        equivalence=EquivalenceConfig(
            max_result_rows=int(spec.validation["max_result_rows"]),
            timeout_seconds=float(entry.query_timeout_seconds),
        ),
        confirmation=ConfirmationConfig(
            warmup=int(stage1["warmups"]),
            repeats=int(stage1["pairs"]),
            timeout_seconds=float(entry.query_timeout_seconds),
            min_latency_ratio=float(stage2["relative_ratio"]),
            min_latency_delta_ms=float(stage2["absolute_delta_ms"]),
        ),
        transforms=TransformConfig(
            enabled_families=(family_name,),
            enabled_rules=(entry.rule_id,),
            max_variants_per_case=entry.max_variants_per_seed,
        ),
    )


def _task_artifact_budget_mb(
    spec: ExperimentSpec | ValidatedYieldExperimentSpec,
    _task: ShardTask | RulePlanEntry,
) -> int:
    key = "entry_budget_mb" if isinstance(spec, ValidatedYieldExperimentSpec) else "shard_budget_mb"
    value = spec.artifacts[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"artifacts.{key} must be a positive integer")
    return value


def _generation_query_limit(
    task: ShardTask | RulePlanEntry,
    spec: ExperimentSpec | ValidatedYieldExperimentSpec,
) -> int:
    if isinstance(task, RulePlanEntry):
        return task.max_generated_seed_budget
    elif isinstance(task, ShardTask):
        max_generated_seeds = task.max_generated_seeds
        accepted_query_pairs = task.accepted_query_pairs
    else:
        raise TypeError("generation task must be ShardTask or RulePlanEntry")
    capabilities = spec.systems[task.target].capabilities
    if "very_low_applicability_generation" in capabilities:
        return min(max_generated_seeds, accepted_query_pairs * 10)
    if "very_low_yield_generation" in capabilities:
        return min(max_generated_seeds, max(100, accepted_query_pairs * 5))
    multiplier = 5 if "low_yield_generation" in capabilities else 2
    return min(max_generated_seeds, accepted_query_pairs * multiplier)


def _requested_max_nodes(task: ShardTask | RulePlanEntry, generated: GeneratedShard) -> int | None:
    value = generated.generator_metadata.get("requested_max_nodes")
    if value is None:
        if isinstance(task, RulePlanEntry):
            value = task.graph_scale
        elif generated.generator_metadata.get("generation_source") == "gdsmith":
            value = GdsmithConfig().max_node_num
        else:
            return None
    if type(value) is not int:
        raise TypeError("requested_max_nodes must be a positive integer")
    if value <= 0:
        raise ValueError("requested_max_nodes must be a positive integer")
    if isinstance(task, RulePlanEntry) and value != task.graph_scale:
        raise ValueError("requested_max_nodes does not match RulePlanEntry.graph_scale")
    return value


def _generation_backend_provenance(target: TargetSystem, backend: str) -> str:
    if target is TargetSystem.APACHE_AGE:
        if backend != "agensgraph":
            raise ValueError("Apache AGE generation must use the isolated AgensGraph proxy")
        return "agensgraph_proxy"
    if target is TargetSystem.ARCADEDB:
        if backend != "neo4j":
            raise ValueError("ArcadeDB generation must use the isolated Neo4j proxy")
        return "neo4j_proxy"
    if target is TargetSystem.LADYBUGDB:
        return "neo4j_proxy"
    return backend


def _entry_directory_name(entry_id: str) -> str:
    return rule_plan_entry_directory_name(entry_id)


def _next_entry_attempt_dir(entry_dir: Path) -> Path:
    indexes = []
    for path in entry_dir.glob("attempt-[0-9][0-9][0-9]"):
        try:
            indexes.append(int(path.name.removeprefix("attempt-")))
        except ValueError:
            continue
    return entry_dir / f"attempt-{(max(indexes, default=-1) + 1):03d}"


def _pending_acceptance(
    pair_hash: str,
    source_query_ordinal: int,
    left: object,
    right: object,
    *,
    max_sample_us: int,
) -> dict[str, object]:
    def elapsed_us(result: object) -> int:
        value = getattr(result, "elapsed_ms", None)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "acceptance runtime is missing or non-numeric",
            )
        if value < 0 or value != value or value in {float("inf"), float("-inf")}:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "acceptance runtime is not a finite non-negative value",
            )
        try:
            quantized = int(round(float(value) * 1000.0))
        except (ArithmeticError, ValueError) as exc:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "acceptance runtime cannot be quantized to integer microseconds",
            ) from exc
        if quantized > max_sample_us:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "acceptance runtime exceeds the entry query timeout bound",
            )
        return quantized

    return {
        "acceptance_a_us": elapsed_us(left),
        "acceptance_b_us": elapsed_us(right),
        "pair_hash": pair_hash,
        "source_query_ordinal": source_query_ordinal,
    }


def _rebuild_pending_pair(
    factory: RulePairFactory,
    entry: RulePlanEntry,
    source_queries: tuple[str, ...],
    ordinal: int,
    pair_hash: str,
):
    if ordinal < 1 or ordinal > min(len(source_queries), entry.max_generated_seed_budget):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "pending pair source ordinal is outside the sealed corpus",
        )
    rebuilt = factory.build(source_queries[ordinal - 1], entry, entry.target)
    if rebuilt.quarantine or rebuilt.pair is None or rebuilt.pair.pair_hash != pair_hash:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "pending pair cannot be rebuilt from the sealed corpus",
        )
    return rebuilt.pair


def _reconcile_runtime_ledger(
    *,
    records: tuple[dict[str, Any], ...],
    ledger: list[dict[str, object]],
    accepted_hashes: set[str],
    pending_by_hash: dict[str, dict[str, object]],
    stage1_intent: dict[str, object] | None,
) -> tuple[
    list[dict[str, object]],
    set[str],
    dict[str, dict[str, object]],
    dict[str, object] | None,
    bool,
]:
    if len(records) not in {len(ledger), len(ledger) + 1}:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "pair_runtime length is not an exact ledger prefix plus one intent tail",
        )
    for record, ledger_item in zip(records, ledger, strict=False):
        if (
            record.get("pair_hash") != ledger_item["pair_hash"]
            or record.get("source_query_ordinal")
            != ledger_item["source_query_ordinal"]
            or canonical_record_digest(record) != ledger_item["record_digest"]
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "pair_runtime append order or canonical digest differs from its ledger",
            )
    if accepted_hashes != {str(value["pair_hash"]) for value in ledger}:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "accepted_pair_hashes is not an exact mirror of the runtime ledger",
        )
    if stage1_intent is None:
        if len(records) != len(ledger):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "pair_runtime tail exists without its exact Stage-1 intent",
            )
        return ledger, accepted_hashes, pending_by_hash, None, False

    intent_hash = str(stage1_intent["pair_hash"])
    intent_ordinal = int(stage1_intent["source_query_ordinal"])
    pending_record = pending_by_hash.get(intent_hash)
    if (
        pending_record is None
        or int(pending_record["source_query_ordinal"]) != intent_ordinal
        or intent_hash in accepted_hashes
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-1 intent is not bound to exactly one pending transaction",
        )
    if len(records) == len(ledger):
        return ledger, accepted_hashes, pending_by_hash, None, True

    tail = records[-1]
    if (
        tail.get("pair_hash") != intent_hash
        or tail.get("source_query_ordinal") != intent_ordinal
        or canonical_record_digest(tail) != stage1_intent["record_digest"]
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-1 intent does not match the durable runtime tail",
        )
    ledger.append({**pending_record, "record_digest": stage1_intent["record_digest"]})
    accepted_hashes.add(intent_hash)
    pending_by_hash.pop(intent_hash)
    return ledger, accepted_hashes, pending_by_hash, None, True


def _validate_runtime_records(
    store: CompactRuntimeStore,
    *,
    entry: RulePlanEntry,
    graph_snapshot_hash: str,
    source_queries: tuple[str, ...],
    factory: RulePairFactory,
    noise_floor_id: str | None,
    noise_p99_ms: float | None,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
) -> dict[str, int]:
    ordinals: set[int] = set()
    by_hash: dict[str, int] = {}
    required_keys = set(PairRuntimeRecord.__dataclass_fields__)
    policy = DEFAULT_SCREENING_POLICY
    stage1_pairs = policy.stage1_pairs
    for record in store.records:
        ordinal = record.get("source_query_ordinal")
        pair_hash = record.get("pair_hash")
        try:
            if (
                set(record) != required_keys
                or type(ordinal) is not int
                or ordinal in ordinals
                or not isinstance(pair_hash, str)
                or record.get("rule_id") != entry.rule_id
                or record.get("family") != entry.family
                or record.get("stream") != entry.stream.value
                or record.get("phase") != entry.phase.value
                or record.get("graph_scale") != entry.graph_scale
                or record.get("shard_seed") != entry.shard_seed
                or record.get("proof_version") != entry.proof_version
                or record.get("protocol_version") != entry.measurement_protocol_id
                or record.get("screen_stage") != "stage1"
                or record.get("lifecycle_status") != "stage1_recorded"
                or record.get("equivalence_summary") != "complete_typed_equivalent"
                or record.get("graph_fingerprint") != graph_snapshot_hash
                or record.get("dbms_fingerprint") != dbms_fingerprint
                or record.get("dbms_health_epoch") != dbms_health_epoch
            ):
                raise ValueError("runtime record identity differs from the entry")
            pair = _rebuild_pending_pair(factory, entry, source_queries, ordinal, pair_hash)
            certificate_hash = canonical_hash(to_jsonable(asdict(pair.certificate)))
            if (
                record.get("query_a_hash") != _query_hash(pair.query_a)
                or record.get("query_b_hash") != _query_hash(pair.query_b)
                or record.get("site_class") != pair.certificate.site_class
                or record.get("safety_certificate_hash") != certificate_hash
            ):
                raise ValueError("runtime record hashes do not bind the rebuilt pair")
            samples = record.get("samples_us")
            expected_orders = paired_schedule(
                stage1_pairs,
                seed=_stage1_seed(entry, pair_hash, policy=policy),
                require_exact_balance=policy.stage1_require_exact_balance,
            )
            if (
                not isinstance(samples, list)
                or len(samples) != stage1_pairs
                or any(
                    not isinstance(sample, list)
                    or len(sample) != 3
                    or sample[0] not in {"AB", "BA"}
                    or any(
                        value is not None and (type(value) is not int or value < 0)
                        for value in sample[1:]
                    )
                    for sample in samples
                )
                or tuple(sample[0] for sample in samples) != expected_orders
                or noise_floor_id is None
                or noise_p99_ms is None
                or record.get("noise_floor_id") != noise_floor_id
            ):
                raise ValueError("runtime record samples violate the fixed Stage-1 protocol")
            summary = summarize_integer_samples(
                [tuple(sample) for sample in samples],
                noise_p99_ms=noise_p99_ms,
                max_sample_us=_entry_sample_upper_bound_us(entry),
            )
            if any(record.get(key) != value for key, value in summary.items()):
                raise ValueError("runtime record summary differs from canonical integer samples")
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "pair_runtime record is not bound to this RulePlanEntry",
            ) from exc
        ordinals.add(ordinal)
        by_hash[pair_hash] = ordinal
    return by_hash


def _load_or_calibrate_entry_noise(
    entry_dir: Path,
    *,
    entry: RulePlanEntry,
    adapter: Any,
    factory: RulePairFactory,
    queries: tuple[str, ...],
    pending: list[dict[str, object]],
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    anchored_noise_floor_id: str | None,
    pending_noise_intent: Mapping[str, object] | None,
    persist_intent: Callable[[str, tuple[str, ...]], None],
    anchored_noise_failure_id: str | None,
    pending_noise_failure_intent: Mapping[str, object] | None,
    persist_failure_intent: Callable[[str], None],
) -> tuple[NoiseCalibration, str]:
    path = entry_dir / "noise_calibration.json"
    failure_path = entry_dir / "noise_calibration_failure.json"
    selected = _expected_noise_probe_records(pending)
    expected_probe_hashes = tuple(str(value["pair_hash"]) for value in selected)
    existing_failure = _load_entry_noise_failure(
        failure_path,
        entry=entry,
        graph_snapshot_hash=graph_snapshot_hash,
        dbms_fingerprint=dbms_fingerprint,
        dbms_health_epoch=dbms_health_epoch,
    )
    if existing_failure is not None:
        failure_id = str(existing_failure["content_hash"])
        if path.exists():
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "complete and failed noise artifacts cannot coexist",
            )
        if anchored_noise_failure_id is not None:
            if (
                anchored_noise_failure_id != failure_id
                or pending_noise_failure_intent is not None
            ):
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "anchored noise failure differs from its artifact",
                )
        elif pending_noise_failure_intent is not None:
            if pending_noise_failure_intent.get("noise_failure_id") != failure_id:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "durable noise failure differs from its exact intent",
                )
        else:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "noise failure artifact exists without an anchor or durable intent",
            )
        return NoiseCalibration(
            valid=False,
            sample_count=int(existing_failure["sample_count"]),
            p95_delta_ms=None,
            p99_delta_ms=None,
            median_order_bias_ms=None,
            long_tail_ratio=None,
            reason_codes=tuple(existing_failure["reason_codes"]),
        ), ""
    if anchored_noise_failure_id is not None:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise failure progress is anchored without its durable artifact",
        )
    if pending_noise_failure_intent is not None:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise failure intent exists without its exact durable artifact",
        )
    existing = _load_entry_noise(
        path,
        entry=entry,
        graph_snapshot_hash=graph_snapshot_hash,
        dbms_fingerprint=dbms_fingerprint,
        dbms_health_epoch=dbms_health_epoch,
        expected_probe_hashes=expected_probe_hashes,
    )
    if existing is not None:
        existing_id = existing[1]
        if anchored_noise_floor_id is not None:
            if anchored_noise_floor_id != existing_id or pending_noise_intent is not None:
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "anchored noise calibration differs from its artifact",
                )
        elif pending_noise_intent is not None:
            if (
                pending_noise_intent.get("noise_floor_id") != existing_id
                or tuple(pending_noise_intent.get("probe_pair_hashes", ()))
                != expected_probe_hashes
            ):
                raise WorkerFailure(
                    ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                    "durable noise artifact differs from its exact intent",
                )
        else:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "noise artifact exists without an anchor or durable intent",
            )
        return existing

    if anchored_noise_floor_id is not None:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise progress is anchored without its durable artifact",
        )
    if pending_noise_intent is not None:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise intent exists without its exact durable artifact",
        )

    if not pending:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "durable runtime records exist without their noise calibration",
        )
    if len(expected_probe_hashes) < 20:
        noise = NoiseCalibration(
            valid=False,
            sample_count=0,
            p95_delta_ms=None,
            p99_delta_ms=None,
            median_order_bias_ms=None,
            long_tail_ratio=None,
            reason_codes=("insufficient_distinct_aa_probes",),
        )
        return noise, ""
    else:
        probes = []
        probe_hashes = list(expected_probe_hashes)
        for selected_record in selected:
            pair = _rebuild_pending_pair(
                factory,
                entry,
                queries,
                int(selected_record["source_query_ordinal"]),
                str(selected_record["pair_hash"]),
            )
            probes.append(pair.query_a)
        try:
            noise, raw_probe_samples = collect_aa_noise_samples(
                RuntimeHarness(),
                adapter,
                tuple(probes),
                pairs_per_query=10,
                timeout_ms=float(entry.query_timeout_seconds * 1000),
                max_sample_us=_entry_sample_upper_bound_us(entry),
            )
        except (AdapterError, ArithmeticError, MemoryError, OSError, ValueError) as exc:
            failure_kind = (
                _entry_infrastructure_error_kind(exc)
                or ErrorKind.INFRASTRUCTURE_ERROR
            )
            failure_reason = f"noise calibration measurement failed: {exc}"
            _write_entry_noise_failure(
                failure_path,
                entry=entry,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
                expected_probe_hashes=expected_probe_hashes,
                raw_probe_samples=(),
                reason_codes=(f"measurement_error:{type(exc).__name__}",),
                error_kind=failure_kind,
                error_reason=failure_reason,
                persist_intent=persist_failure_intent,
            )
            return NoiseCalibration(
                valid=False,
                sample_count=0,
                p95_delta_ms=None,
                p99_delta_ms=None,
                median_order_bias_ms=None,
                long_tail_ratio=None,
                reason_codes=(f"measurement_error:{type(exc).__name__}",),
            ), ""
        complete = (
            len(raw_probe_samples) == 20
            and all(len(samples) == 10 for samples in raw_probe_samples)
        )
        if not complete:
            _write_entry_noise_failure(
                failure_path,
                entry=entry,
                graph_snapshot_hash=graph_snapshot_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_health_epoch=dbms_health_epoch,
                expected_probe_hashes=expected_probe_hashes,
                raw_probe_samples=raw_probe_samples,
                reason_codes=noise.reason_codes or ("incomplete_aa_samples",),
                error_kind=ErrorKind.INFRASTRUCTURE_ERROR,
                error_reason="invalid fixed-protocol A/A noise calibration",
                persist_intent=persist_failure_intent,
            )
            return noise, ""
        probe_samples_us = [
            {
                "pair_hash": pair_hash,
                "samples_us": [list(sample) for sample in samples],
            }
            for pair_hash, samples in zip(
                probe_hashes,
                raw_probe_samples,
                strict=True,
            )
        ]
    body = {
        "calibration": to_jsonable(asdict(noise)),
        "dbms_fingerprint": dbms_fingerprint,
        "dbms_health_epoch": dbms_health_epoch,
        "distinct_probe_count": len(set(probe_hashes)),
        "entry_id": entry.entry_id,
        "graph_snapshot_hash": graph_snapshot_hash,
        "long_tail_ratio_max": 5.0,
        "minimum_complete_samples": 200,
        "order_bias_rule": "median_order_bias_ms<=p95_delta_ms",
        "pairs_per_query": 10,
        "probe_pair_hashes": probe_hashes,
        "probe_samples_us": probe_samples_us,
        "probe_queries": 20,
        "protocol_version": "aa-noise-20x10-v1",
    }
    content_hash = canonical_hash(body)
    persist_intent(content_hash, tuple(probe_hashes))
    _atomic_write_json(path, {**body, "content_hash": content_hash})
    return noise, content_hash


def _load_entry_noise(
    path: Path,
    *,
    entry: RulePlanEntry,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    expected_probe_hashes: tuple[str, ...],
) -> tuple[NoiseCalibration, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise calibration artifact is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "noise calibration is not an object")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    probe_hashes = body.get("probe_pair_hashes")
    expected_body_keys = {
        "calibration",
        "dbms_fingerprint",
        "dbms_health_epoch",
        "distinct_probe_count",
        "entry_id",
        "graph_snapshot_hash",
        "long_tail_ratio_max",
        "minimum_complete_samples",
        "order_bias_rule",
        "pairs_per_query",
        "probe_pair_hashes",
        "probe_samples_us",
        "probe_queries",
        "protocol_version",
    }
    valid_probe_hashes = (
        isinstance(probe_hashes, list)
        and all(isinstance(value, str) and len(value) == 64 for value in probe_hashes)
        and len(probe_hashes) == len(set(probe_hashes))
    )
    if (
        set(body) != expected_body_keys
        or payload.get("content_hash") != canonical_hash(body)
        or body.get("entry_id") != entry.entry_id
        or body.get("graph_snapshot_hash") != graph_snapshot_hash
        or body.get("dbms_fingerprint") != dbms_fingerprint
        or body.get("dbms_health_epoch") != dbms_health_epoch
        or type(body.get("distinct_probe_count")) is not int
        or not valid_probe_hashes
        or tuple(probe_hashes) != expected_probe_hashes
        or body.get("distinct_probe_count") != len(probe_hashes)
        or body.get("probe_queries") != 20
        or body.get("pairs_per_query") != 10
        or body.get("minimum_complete_samples") != 200
        or body.get("order_bias_rule") != "median_order_bias_ms<=p95_delta_ms"
        or body.get("long_tail_ratio_max") != 5.0
        or body.get("protocol_version") != "aa-noise-20x10-v1"
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise calibration identity or immutable protocol changed",
        )
    calibration = body.get("calibration")
    probe_samples = body.get("probe_samples_us")
    if not isinstance(calibration, dict) or not isinstance(probe_samples, list):
        raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "noise calibration payload is missing")
    try:
        expected_calibration_keys = {
            "long_tail_ratio",
            "median_order_bias_ms",
            "p95_delta_ms",
            "p99_delta_ms",
            "reason_codes",
            "sample_count",
            "valid",
        }
        if (
            set(calibration) != expected_calibration_keys
            or len(probe_samples) != 20
            or any(
                not isinstance(value, dict)
                or set(value) != {"pair_hash", "samples_us"}
                or value["pair_hash"] != probe_hashes[index]
                or not isinstance(value["samples_us"], list)
                or len(value["samples_us"]) != 10
                for index, value in enumerate(probe_samples)
            )
        ):
            raise ValueError("invalid noise calibration field")
        noise = summarize_aa_noise_samples(
            [value["samples_us"] for value in probe_samples],
            max_sample_us=_entry_sample_upper_bound_us(entry),
        )
        if calibration != to_jsonable(asdict(noise)):
            raise ValueError("noise summary differs from canonical integer samples")
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "noise calibration shape is invalid") from exc
    return noise, str(payload["content_hash"])


def _write_entry_noise_failure(
    path: Path,
    *,
    entry: RulePlanEntry,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    expected_probe_hashes: tuple[str, ...],
    raw_probe_samples: tuple[tuple[tuple[str, int, int], ...], ...],
    reason_codes: tuple[str, ...],
    error_kind: ErrorKind,
    error_reason: str,
    persist_intent: Callable[[str], None],
) -> None:
    completed = [
        {
            "pair_hash": expected_probe_hashes[index],
            "samples_us": [list(sample) for sample in samples],
        }
        for index, samples in enumerate(raw_probe_samples)
    ]
    body = sanitize_structure({
        "schema_version": 1,
        "status": "incomplete",
        "entry_id": entry.entry_id,
        "graph_snapshot_hash": graph_snapshot_hash,
        "dbms_fingerprint": dbms_fingerprint,
        "dbms_health_epoch": dbms_health_epoch,
        "protocol_version": "aa-noise-20x10-v1",
        "probe_queries": 20,
        "pairs_per_query": 10,
        "expected_probe_pair_hashes": list(expected_probe_hashes),
        "completed_probe_samples_us": completed,
        "sample_count": sum(len(samples) for samples in raw_probe_samples),
        "reason_codes": list(reason_codes),
        "error_kind": error_kind.value,
        "error_reason": error_reason,
    })
    if not isinstance(body, dict):  # pragma: no cover - dict contract
        raise TypeError("noise failure sanitizer must return an object")
    content_hash = canonical_hash(body)
    persist_intent(content_hash)
    _atomic_write_json(path, {**body, "content_hash": content_hash})


def _load_entry_noise_failure(
    path: Path,
    *,
    entry: RulePlanEntry,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise calibration failure artifact is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise calibration failure artifact is not an object",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    expected_keys = {
        "completed_probe_samples_us",
        "dbms_fingerprint",
        "dbms_health_epoch",
        "entry_id",
        "error_kind",
        "error_reason",
        "expected_probe_pair_hashes",
        "graph_snapshot_hash",
        "pairs_per_query",
        "probe_queries",
        "protocol_version",
        "reason_codes",
        "sample_count",
        "schema_version",
        "status",
    }
    if (
        set(body) != expected_keys
        or payload.get("content_hash") != canonical_hash(body)
        or body.get("schema_version") != 1
        or body.get("status") != "incomplete"
        or body.get("entry_id") != entry.entry_id
        or body.get("graph_snapshot_hash") != graph_snapshot_hash
        or body.get("dbms_fingerprint") != dbms_fingerprint
        or body.get("dbms_health_epoch") != dbms_health_epoch
        or body.get("protocol_version") != "aa-noise-20x10-v1"
        or body.get("probe_queries") != 20
        or body.get("pairs_per_query") != 10
        or not isinstance(body.get("reason_codes"), list)
        or not body["reason_codes"]
        or any(not isinstance(value, str) or not value for value in body["reason_codes"])
        or body.get("error_kind") not in {
            ErrorKind.DRIVER_TIMEOUT.value,
            ErrorKind.DBMS_UNAVAILABLE.value,
            ErrorKind.DBMS_CRASHED.value,
            ErrorKind.INFRASTRUCTURE_ERROR.value,
        }
        or not isinstance(body.get("error_reason"), str)
        or not body["error_reason"]
        or type(body.get("sample_count")) is not int
        or body["sample_count"] < 0
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "noise calibration failure artifact identity or hash is invalid",
        )
    return {**body, "content_hash": payload["content_hash"]}


def _expected_noise_probe_records(
    acceptance_records: list[dict[str, object]],
) -> list[dict[str, object]]:
    ordered = sorted(
        acceptance_records,
        key=lambda value: (int(value["acceptance_a_us"]), str(value["pair_hash"])),
    )
    if len({str(value["pair_hash"]) for value in ordered}) < 20:
        return ordered
    return [
        ordered[round(index * (len(ordered) - 1) / 19)]
        for index in range(20)
    ]


def _stage2_entry_payload(entry: RulePlanEntry) -> dict[str, object]:
    payload = to_jsonable(asdict(entry))
    if not isinstance(payload, dict):  # pragma: no cover - dataclass contract
        raise TypeError("RulePlanEntry payload must be an object")
    return payload


def _remove_empty_stage2_candidate_dir(
    pair_dir: Path,
    *,
    candidate_root: Path,
) -> None:
    """Remove only a provably empty unanchored candidate directory durably."""

    try:
        pair_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "missing Stage-2 candidate directory is not provably empty",
        ) from exc
    _fsync_directory(candidate_root)


def _require_screening_policy_config(
    spec: ValidatedYieldExperimentSpec,
    *,
    policy: ScreeningPolicy,
) -> None:
    if dict(spec.runtime["stage1"]) != policy.stage1_config():
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-1 configuration differs from the immutable ranking protocol",
        )
    if dict(spec.runtime["stage2"]) != policy.stage2_config():
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 configuration differs from the immutable wide-recall protocol",
        )


def _stage2_screening_policy(
    spec: ValidatedYieldExperimentSpec,
    entry: RulePlanEntry,
    *,
    noise_floor_id: str,
) -> dict[str, object]:
    policy = DEFAULT_SCREENING_POLICY
    _require_screening_policy_config(spec, policy=policy)
    payload = policy.to_payload()
    stage2 = dict(payload["stage2"])
    stage2.update(
        {
            "timeout_ms": float(entry.query_timeout_seconds * 1000),
            "seed_derivation": (
                "sha256(shard_seed:entry_id:pair_hash:"
                f"{policy.stage2_seed_version})[:8]&2^63-1"
            ),
        }
    )
    return {
        **payload,
        "experiment_seed": spec.seed,
        "noise_floor_id": noise_floor_id,
        "stage2": stage2,
    }


def _stage2_progress_body(
    *,
    entry_payload: Mapping[str, object],
    evidence_identity: Mapping[str, object],
    noise_floor_id: str,
    screening_policy: Mapping[str, object],
    selected_pair_hashes: tuple[str, ...],
    candidate_ledger: list[dict[str, object]],
    candidate_intent: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        **_stage2_policy_binding(entry_payload, screening_policy),
        "evidence_identity": dict(evidence_identity),
        "noise_floor_id": noise_floor_id,
        "selected_pair_hashes": list(selected_pair_hashes),
        "candidate_ledger": [dict(value) for value in candidate_ledger],
        "candidate_intent": (
            dict(candidate_intent) if candidate_intent is not None else None
        ),
    }


def _stage2_policy_binding(
    entry_payload: Mapping[str, object],
    screening_policy: Mapping[str, object],
) -> dict[str, object]:
    frozen_entry = dict(entry_payload)
    frozen_policy = dict(screening_policy)
    return {
        "entry_payload": frozen_entry,
        "entry_payload_hash": canonical_hash(frozen_entry),
        "screening_policy": frozen_policy,
        "screening_policy_hash": canonical_hash(frozen_policy),
    }


def _persist_stage2_progress(
    path: Path,
    *,
    entry_payload: Mapping[str, object],
    evidence_identity: Mapping[str, object],
    noise_floor_id: str,
    screening_policy: Mapping[str, object],
    selected_pair_hashes: tuple[str, ...],
    candidate_ledger: list[dict[str, object]],
    candidate_intent: dict[str, object] | None,
) -> dict[str, object]:
    body = _stage2_progress_body(
        entry_payload=entry_payload,
        evidence_identity=evidence_identity,
        noise_floor_id=noise_floor_id,
        screening_policy=screening_policy,
        selected_pair_hashes=selected_pair_hashes,
        candidate_ledger=candidate_ledger,
        candidate_intent=candidate_intent,
    )
    payload = {**body, "content_hash": canonical_hash(body)}
    _atomic_write_json(path, payload)
    return payload


def _load_or_create_stage2_progress(
    path: Path,
    *,
    entry_payload: Mapping[str, object],
    evidence_identity: Mapping[str, object],
    noise_floor_id: str,
    screening_policy: Mapping[str, object],
    selected_pair_hashes: tuple[str, ...],
    candidate_root: Path,
    selection_path: Path,
) -> dict[str, object]:
    candidate_dirs = (
        {candidate.name for candidate in candidate_root.iterdir() if candidate.is_dir()}
        if candidate_root.exists()
        else set()
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if candidate_dirs or selection_path.exists():
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "Stage-2 candidate evidence predates the required progress ledger",
            )
        return _persist_stage2_progress(
            path,
            entry_payload=entry_payload,
            evidence_identity=evidence_identity,
            noise_floor_id=noise_floor_id,
            screening_policy=screening_policy,
            selected_pair_hashes=selected_pair_hashes,
            candidate_ledger=[],
            candidate_intent=None,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 progress is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 progress is not an object",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    required = {
        "candidate_intent",
        "candidate_ledger",
        "entry_payload",
        "entry_payload_hash",
        "evidence_identity",
        "noise_floor_id",
        "schema_version",
        "screening_policy",
        "screening_policy_hash",
        "selected_pair_hashes",
    }
    ledger = body.get("candidate_ledger")
    intent = body.get("candidate_intent")
    selected = body.get("selected_pair_hashes")
    valid_ledger = isinstance(ledger, list) and all(
        isinstance(value, dict)
        and set(value) == {"candidate_digest", "pair_hash"}
        and _is_sha256(value.get("pair_hash"))
        and _is_sha256(value.get("candidate_digest"))
        for value in ledger
    )
    valid_intent = intent is None or (
        isinstance(intent, dict)
        and set(intent) == {"candidate_digest", "pair_hash", "selected_index"}
        and type(intent.get("selected_index")) is int
        and _is_sha256(intent.get("pair_hash"))
        and _is_sha256(intent.get("candidate_digest"))
    )
    expected_policy_binding = _stage2_policy_binding(
        entry_payload,
        screening_policy,
    )
    if (
        set(body) != required
        or body.get("schema_version") != 1
        or payload.get("content_hash") != canonical_hash(body)
        or any(body.get(key) != value for key, value in expected_policy_binding.items())
        or body.get("evidence_identity") != dict(evidence_identity)
        or body.get("noise_floor_id") != noise_floor_id
        or selected != list(selected_pair_hashes)
        or not valid_ledger
        or not valid_intent
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 progress identity, policy, shape, or hash differs",
        )
    assert isinstance(ledger, list)
    ledger_hashes = [str(value["pair_hash"]) for value in ledger]
    if (
        len(ledger_hashes) != len(set(ledger_hashes))
        or ledger_hashes != list(selected_pair_hashes[: len(ledger_hashes)])
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate ledger is not the deterministic selected prefix",
        )
    expected_dirs = set(ledger_hashes)
    if intent is not None:
        assert isinstance(intent, dict)
        next_index = len(ledger_hashes)
        if (
            intent["selected_index"] != next_index
            or next_index >= len(selected_pair_hashes)
            or intent["pair_hash"] != selected_pair_hashes[next_index]
        ):
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "Stage-2 candidate intent is not the exact next selected pair",
            )
        expected_dirs.add(str(intent["pair_hash"]))
    if candidate_dirs - expected_dirs:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate directory is orphaned from the progress ledger",
        )
    if selection_path.exists() and len(ledger_hashes) != len(selected_pair_hashes):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 selection exists before the candidate ledger is complete",
        )
    return payload


def _validate_existing_stage2_resume_policy(
    path: Path,
    *,
    spec: ValidatedYieldExperimentSpec,
    entry: RulePlanEntry,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 progress is unreadable before resume",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 progress is not an object before resume",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    noise_floor_id = body.get("noise_floor_id")
    entry_payload = _stage2_entry_payload(entry)
    expected_policy = _stage2_screening_policy(
        spec,
        entry,
        noise_floor_id=str(noise_floor_id),
    )
    expected_binding = _stage2_policy_binding(entry_payload, expected_policy)
    if (
        payload.get("content_hash") != canonical_hash(body)
        or not _is_sha256(noise_floor_id)
        or any(body.get(key) != value for key, value in expected_binding.items())
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 resume entry or screening policy drifted",
        )


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _stage2_candidate_payload(
    *,
    entry: RulePlanEntry,
    pair: object,
    stage1_record: Mapping[str, object],
    graph_manifest: GraphManifest,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    noise_floor_id: str,
    seed: int,
    expected_direction: str,
    candidate: Stage2Candidate,
    entry_payload: Mapping[str, object],
    screening_policy: Mapping[str, object],
    policy: ScreeningPolicy,
) -> dict[str, object]:
    body = {
        "schema_version": 2,
        "entry_identity": _stage2_entry_identity(entry),
        **_stage2_policy_binding(entry_payload, screening_policy),
        "evidence_identity": _runtime_evidence_identity(
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        ),
        "pair_hash": candidate.pair_hash,
        "source_query_ordinal": stage1_record["source_query_ordinal"],
        "noise_floor_id": noise_floor_id,
        "stage1_record_digest": canonical_record_digest(dict(stage1_record)),
        "stage1_evidence": dict(stage1_record),
        "stage2_protocol": _stage2_protocol(
            entry,
            candidate.pair_hash,
            seed=seed,
            expected_direction=expected_direction,
            policy=policy,
        ),
        "query_a": pair.query_a,
        "query_b": pair.query_b,
        "parameters": to_jsonable(dict(pair.parameters)),
        "safety_certificate": to_jsonable(asdict(pair.certificate)),
        "transform_metadata": to_jsonable(dict(pair.transform_metadata)),
        "graph_manifest": to_jsonable(asdict(graph_manifest)),
        "stage2_evidence": to_jsonable(asdict(candidate.evidence)),
        "stage2_decision": _stage2_decision_payload(candidate.decision),
        "noise_normalized_score": candidate.noise_normalized_score,
    }
    return {**body, "content_hash": canonical_hash(body)}


def _load_stage2_candidate(
    path: Path,
    *,
    entry: RulePlanEntry,
    pair: object,
    stage1_record: Mapping[str, object],
    graph_manifest: GraphManifest,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    noise_floor_id: str,
    noise_p99_ms: float,
    seed: int,
    expected_direction: str,
    entry_payload: Mapping[str, object],
    screening_policy: Mapping[str, object],
    policy: ScreeningPolicy,
) -> tuple[Stage2Candidate, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate evidence is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate evidence is not an object",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    required = {
        "entry_identity",
        "entry_payload",
        "entry_payload_hash",
        "evidence_identity",
        "graph_manifest",
        "noise_floor_id",
        "noise_normalized_score",
        "pair_hash",
        "parameters",
        "query_a",
        "query_b",
        "safety_certificate",
        "schema_version",
        "screening_policy",
        "screening_policy_hash",
        "source_query_ordinal",
        "stage1_evidence",
        "stage1_record_digest",
        "stage2_decision",
        "stage2_evidence",
        "stage2_protocol",
        "transform_metadata",
    }
    expected_identity = _runtime_evidence_identity(
        graph_snapshot_hash=graph_snapshot_hash,
        dbms_fingerprint=dbms_fingerprint,
        dbms_health_epoch=dbms_health_epoch,
    )
    expected_stage1 = to_jsonable(dict(stage1_record))
    expected_policy_binding = _stage2_policy_binding(
        entry_payload,
        screening_policy,
    )
    if (
        set(body) != required
        or body.get("schema_version") != 2
        or payload.get("content_hash") != canonical_hash(body)
        or body.get("entry_identity") != _stage2_entry_identity(entry)
        or any(body.get(key) != value for key, value in expected_policy_binding.items())
        or body.get("evidence_identity") != expected_identity
        or body.get("pair_hash") != pair.pair_hash
        or body.get("source_query_ordinal") != stage1_record["source_query_ordinal"]
        or body.get("noise_floor_id") != noise_floor_id
        or body.get("stage1_record_digest")
        != canonical_record_digest(dict(stage1_record))
        or body.get("stage1_evidence") != expected_stage1
        or body.get("stage2_protocol")
        != _stage2_protocol(
            entry,
            pair.pair_hash,
            seed=seed,
            expected_direction=expected_direction,
            policy=policy,
        )
        or body.get("query_a") != pair.query_a
        or body.get("query_b") != pair.query_b
        or body.get("parameters") != to_jsonable(dict(pair.parameters))
        or body.get("safety_certificate") != to_jsonable(asdict(pair.certificate))
        or body.get("transform_metadata")
        != to_jsonable(dict(pair.transform_metadata))
        or body.get("graph_manifest") != to_jsonable(asdict(graph_manifest))
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate identity, hash, or source binding differs",
        )
    try:
        evidence = runtime_evidence_from_payload(body["stage2_evidence"])
        if (
            evidence.query_a_hash != _query_hash(pair.query_a)
            or evidence.query_b_hash != _query_hash(pair.query_b)
            or evidence.slow_query != expected_direction
            or len(evidence.paired_samples) != policy.stage2_pairs
            or tuple(sample.order for sample in evidence.paired_samples)
            != paired_schedule(
                policy.stage2_pairs,
                seed=seed,
                require_exact_balance=policy.stage2_require_exact_balance,
            )
        ):
            raise ValueError("Stage-2 runtime evidence binding differs")
        validate_stage2_evidence(
            evidence,
            query_a_hash=_query_hash(pair.query_a),
            query_b_hash=_query_hash(pair.query_b),
            expected_orders=paired_schedule(
                policy.stage2_pairs,
                seed=seed,
                require_exact_balance=policy.stage2_require_exact_balance,
            ),
            expected_slow_query=expected_direction,
            seed=seed,
            timeout_ms=float(entry.query_timeout_seconds * 1000),
            policy=policy,
        )
        decision = evaluate_stage2(
            evidence,
            noise_p99_ms=noise_p99_ms,
            policy=policy,
        )
        if body.get("stage2_decision") != _stage2_decision_payload(decision):
            raise ValueError("Stage-2 decision differs from runtime evidence")
        score = float(body["noise_normalized_score"])
        expected_score = (
            float(evidence.median_delta_ms)
            / max(noise_p99_ms, policy.normalization_epsilon_ms)
            if evidence.median_delta_ms is not None
            else float(stage1_record["delta_over_noise"])
        )
        if not math.isfinite(score) or score != expected_score:
            raise ValueError("Stage-2 score differs from runtime evidence")
        candidate = Stage2Candidate(pair.pair_hash, decision, score, evidence)
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 candidate runtime evidence is malformed",
        ) from exc
    return candidate, str(payload["content_hash"])


def _persist_stage2_selection(
    path: Path,
    *,
    entry: RulePlanEntry,
    measured: tuple[tuple[Stage2Candidate, str], ...],
    selected_pair_hashes: tuple[str, ...],
    validation_limit: int,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
    noise_floor_id: str,
    entry_payload: Mapping[str, object],
    screening_policy: Mapping[str, object],
) -> None:
    body = {
        "schema_version": 2,
        "entry_identity": _stage2_entry_identity(entry),
        **_stage2_policy_binding(entry_payload, screening_policy),
        "evidence_identity": _runtime_evidence_identity(
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_health_epoch=dbms_health_epoch,
        ),
        "noise_floor_id": noise_floor_id,
        "candidate_digests": [
            {"pair_hash": candidate.pair_hash, "content_hash": digest}
            for candidate, digest in measured
        ],
        "selected_pair_hashes": list(selected_pair_hashes),
        "validation_limit": validation_limit,
    }
    payload = {**body, "content_hash": canonical_hash(body)}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _atomic_write_json(path, payload)
        return
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 selection artifact is unreadable",
        ) from exc
    if existing != payload:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Stage-2 selection artifact differs from deterministic candidates",
        )


def _persist_stage2_validation_outcome(
    path: Path,
    outcome: Mapping[str, object],
) -> None:
    body = to_jsonable(dict(outcome))
    payload = {**body, "content_hash": canonical_hash(body)}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        _fsync_directory(path.parent.parent)
        _atomic_write_json(path, payload)
        return
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome artifact is unreadable",
        ) from exc
    if existing != payload:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome differs from the sealed pooled evidence",
        )


def _task8_validation_reference(
    path: Path,
    *,
    run_dir: Path,
    validation: object,
) -> dict[str, object]:
    if not isinstance(validation, IssueValidationReport):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Task-8 validation reference requires a typed report",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        relative_path = path.relative_to(run_dir).as_posix()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Task-8 validation reference cannot be constructed",
        ) from exc
    content_hash = payload.get("content_hash") if isinstance(payload, Mapping) else None
    if not isinstance(content_hash, str):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "Task-8 validation reference lacks a content hash",
        )
    return {
        "content_hash": content_hash,
        "relative_path": relative_path,
        "report_hash": canonical_validation_report_hash(validation),
        "schema_version": 2,
    }


def _load_stage2_validation_outcome(
    path: Path,
    *,
    expected_identity: Mapping[str, object],
    candidate: CandidateBundle,
    expected_control_gate_hit: bool | None,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome artifact is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome artifact is not an object",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    terminal_stages = {
        PipelineStage.VALIDATION_PASSED.value,
        PipelineStage.REJECTED.value,
        PipelineStage.INCONCLUSIVE.value,
        PipelineStage.INVALID.value,
    }
    report_payload = body.get("report")
    required = {
        *expected_identity,
        "pooled_delta_ci_low_ms",
        "pooled_delta_ms",
        "pooled_ratio_ci_low",
        "reason_codes",
        "report",
        "schema_version",
        "sessions",
        "stage",
        "submission_path",
        "validated_ratio",
    }
    control_fields = {"control_gate_hit", "control_validation_pass"}
    control_stream = expected_identity.get("stream") == RuleStream.CONTROL.value
    expected_fields = required | control_fields if control_stream else required
    try:
        content_hash_matches = payload.get("content_hash") == canonical_hash(body)
    except (TypeError, ValueError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome contains non-canonical evidence",
        ) from exc
    if (
        not content_hash_matches
        or body.get("schema_version") != 2
        or body.get("stage") not in terminal_stages
        or not isinstance(report_payload, dict)
        or not isinstance(body.get("sessions"), list)
        or set(body) != expected_fields
        or report_payload.get("sessions") != body["sessions"]
        or any(body.get(key) != to_jsonable(value) for key, value in expected_identity.items())
        or (
            control_stream
            and (
                type(expected_control_gate_hit) is not bool
                or type(body.get("control_gate_hit")) is not bool
                or type(body.get("control_validation_pass")) is not bool
                or body.get("control_gate_hit") is not expected_control_gate_hit
            )
        )
        or (not control_stream and expected_control_gate_hit is not None)
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome identity or pooled evidence hash differs",
        )
    duplicate_fields = {
        "stage": "stage",
        "reason_codes": "reason_codes",
        "validated_ratio": "validated_ratio",
        "pooled_delta_ms": "pooled_delta_ms",
        "pooled_ratio_ci_low": "pooled_ratio_ci_low",
        "pooled_delta_ci_low_ms": "pooled_delta_ci_low_ms",
        "submission_path": "submission_path",
    }
    if any(
        body.get(outcome_key) != report_payload.get(report_key)
        for outcome_key, report_key in duplicate_fields.items()
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation outcome summary differs from its sealed typed report",
        )
    try:
        recomputed = replay_issue_validation_report(candidate, report_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation typed report cannot be reconstructed",
        ) from exc
    if to_jsonable(asdict(recomputed)) != report_payload or (
        recomputed.stage is PipelineStage.VALIDATION_PASSED
        and not issue_validation_report_is_complete(recomputed)
    ) or (
        control_stream
        and body.get("control_validation_pass")
        is not (recomputed.stage is PipelineStage.VALIDATION_PASSED)
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "validation typed report differs from canonical raw-session replay",
        )
    return body


def _stage2_entry_identity(entry: RulePlanEntry) -> dict[str, object]:
    return {
        "entry_id": entry.entry_id,
        "family": entry.family,
        "graph_scale": entry.graph_scale,
        "measurement_protocol_id": entry.measurement_protocol_id,
        "phase": entry.phase.value,
        "rule_id": entry.rule_id,
        "shard_index": entry.shard_index,
        "shard_seed": entry.shard_seed,
        "stream": entry.stream.value,
        "target": entry.target.value,
        "tier": entry.tier,
    }


def _stage2_protocol(
    entry: RulePlanEntry,
    pair_hash: str,
    *,
    seed: int,
    expected_direction: str,
    policy: ScreeningPolicy,
) -> dict[str, object]:
    return {
        "bootstrap_resamples": policy.stage2_bootstrap_resamples,
        "expected_slow_query": expected_direction,
        "pairs": policy.stage2_pairs,
        "protocol_version": f"stage2-balanced-{policy.stage2_pairs}-v2",
        "require_exact_balance": policy.stage2_require_exact_balance,
        "seed": seed,
        "seed_sha256": hashlib.sha256(
            (
                f"{entry.shard_seed}:{entry.entry_id}:{pair_hash}:"
                f"{policy.stage2_seed_version}"
            ).encode("utf-8")
        ).hexdigest(),
        "warmups": policy.stage2_warmups,
    }


def _stage2_decision_payload(decision: Stage2Decision) -> dict[str, object]:
    return {
        "absolute_pass": decision.absolute_pass,
        "promote": decision.promote,
        "reason_codes": list(decision.reason_codes),
        "relative_pass": decision.relative_pass,
    }


def _stage2_candidate_rank_key(
    candidate: Stage2Candidate,
) -> tuple[float, float, float, str]:
    return (
        -candidate.noise_normalized_score,
        -float(candidate.evidence.median_delta_ms or 0.0),
        -float(candidate.evidence.median_ratio or 0.0),
        candidate.pair_hash,
    )


def _wilson_lower_bound(successes: int, total: int, *, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1.0 + (z * z / total)
    centre = proportion + (z * z / (2.0 * total))
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) / total)
        + (z * z / (4.0 * total * total))
    )
    return max(0.0, (centre - margin) / denominator)


def _validated_pair_yield(
    *,
    successes: int,
    accepted_pairs: int,
) -> tuple[float, float]:
    """Return validated CORE issues per accepted pair and its Wilson lower bound."""

    if (
        isinstance(successes, bool)
        or isinstance(accepted_pairs, bool)
        or not isinstance(successes, int)
        or not isinstance(accepted_pairs, int)
        or accepted_pairs < 0
        or successes < 0
        or successes > accepted_pairs
    ):
        raise ValueError(
            "validated pair yield requires 0 <= successes <= accepted_pairs"
        )
    if accepted_pairs == 0:
        return 0.0, 0.0
    return (
        successes / accepted_pairs,
        _wilson_lower_bound(successes, accepted_pairs),
    )


def _compact_timeout_side(record: PairRuntimeRecord) -> str | None:
    if record.timeout_state == "a_timeout":
        return "a"
    if record.timeout_state == "b_timeout":
        return "b"
    return None


def _runtime_timeout_side(evidence: RuntimeEvidence) -> str | None:
    a_timeouts = sum(sample.a_timeout for sample in evidence.paired_samples)
    b_timeouts = sum(sample.b_timeout for sample in evidence.paired_samples)
    if a_timeouts and not b_timeouts:
        return "a"
    if b_timeouts and not a_timeouts:
        return "b"
    return None


def _stage2_seed(
    entry: RulePlanEntry,
    pair_hash: str,
    *,
    policy: ScreeningPolicy,
) -> int:
    digest = hashlib.sha256(
        (
            f"{entry.shard_seed}:{entry.entry_id}:{pair_hash}:"
            f"{policy.stage2_seed_version}"
        ).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _stage1_seed(
    entry: RulePlanEntry,
    pair_hash: str,
    *,
    policy: ScreeningPolicy,
) -> int:
    digest = hashlib.sha256(
        (
            f"{entry.shard_seed}:{entry.entry_id}:{pair_hash}:"
            f"{policy.stage1_seed_version}"
        ).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_fsync(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _validated_entry_accepted_hashes(
    path: Path,
    *,
    entry: RulePlanEntry,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    source_queries: tuple[str, ...],
) -> dict[str, int]:
    accepted: dict[str, int] = {}
    accepted_ordinals: set[int] = set()
    registry = RuleRegistry.default()
    rule = registry.get(entry.rule_id)
    factory = RulePairFactory(registry)
    for record in _read_jsonl(
        path,
        error_kind=ErrorKind.EVIDENCE_INTEGRITY_ERROR,
        context="accepted pair evidence",
    ):
        pair_hash = record.get("pair_hash")
        query_a = record.get("query_a")
        query_b = record.get("query_b")
        parameters = record.get("parameters")
        certificate = record.get("certificate")
        transform_metadata = record.get("transform_metadata")
        semantic_proof_sha256 = record.get("semantic_proof_sha256")
        source_query_ordinal = record.get("source_query_ordinal")
        try:
            if (
                not isinstance(pair_hash, str)
                or len(pair_hash) != 64
                or not isinstance(query_a, str)
                or not query_a
                or type(source_query_ordinal) is not int
                or source_query_ordinal < 1
                or source_query_ordinal > min(len(source_queries), entry.max_generated_seed_budget)
                or source_queries[source_query_ordinal - 1] != query_a
                or not isinstance(query_b, str)
                or not query_b
                or query_a == query_b
                or not isinstance(parameters, Mapping)
                or not isinstance(certificate, Mapping)
                or not isinstance(transform_metadata, Mapping)
                or not isinstance(semantic_proof_sha256, str)
                or len(semantic_proof_sha256) != 64
                or record.get("entry_id") != entry.entry_id
                or record.get("rule_id") != entry.rule_id
                or record.get("family") != entry.family
                or record.get("tier") != entry.tier
                or record.get("stream") != entry.stream.value
                or record.get("proof_version") != entry.proof_version
                or record.get("graph_snapshot_hash") != graph_snapshot_hash
                or record.get("dbms_fingerprint") != dbms_fingerprint
                or record.get("equivalence") != "complete_typed_equivalent"
            ):
                raise ValueError("record identity does not match the immutable entry")
            certificate_payload = dict(certificate)
            for tuple_field in ("variables_in_scope", "preconditions"):
                value = certificate_payload.get(tuple_field)
                if isinstance(value, list):
                    certificate_payload[tuple_field] = tuple(value)
            rebuilt_certificate = TransformSafetyCertificate(**certificate_payload)
            proof = transform_metadata.get("semantic_proof")
            embedded_certificate = transform_metadata.get("safety_certificate")
            if not isinstance(proof, Mapping) or not isinstance(embedded_certificate, Mapping):
                raise ValueError("record is missing its signed proof evidence")
            if dict(embedded_certificate) != dict(certificate):
                raise ValueError("embedded and top-level certificates differ")
            proof_hash = canonical_semantic_proof_hash(proof)
            if (
                rebuilt_certificate.semantic_proof_hash != proof_hash
                or semantic_proof_sha256 != proof_hash
                or transform_metadata.get("semantic_proof_sha256") != proof_hash
                or record.get("site") != rebuilt_certificate.site_class
                or record.get("query_hash") != portable_ast_hash(query_a)
                or pair_hash
                != certified_pair_hash(query_a, query_b, entry.rule_id, proof_hash)
            ):
                raise ValueError("record hashes do not bind the accepted pair")
            validate_certified_pair(
                rule,
                query_a=query_a,
                query_b=query_b,
                certificate=rebuilt_certificate,
                semantic_proof=proof,
            )
            rebuilt = factory.build(query_a, entry, entry.target)
            if rebuilt.quarantine or rebuilt.pair is None:
                raise ValueError("accepted record cannot be rebuilt by its canonical rule factory")
            expected_pair = rebuilt.pair
            if (
                expected_pair.query_b != query_b
                or to_jsonable(expected_pair.parameters) != dict(parameters)
                or to_jsonable(asdict(expected_pair.certificate)) != dict(certificate)
                or to_jsonable(expected_pair.transform_metadata) != dict(transform_metadata)
                or expected_pair.pair_hash != pair_hash
            ):
                raise ValueError("accepted record differs from its canonical factory reconstruction")
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "durable accepted pair is not bound to this RulePlanEntry",
            ) from exc
        if pair_hash in accepted:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "durable accepted pair hash is duplicated",
            )
        if source_query_ordinal in accepted_ordinals:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "durable accepted pair source query ordinal is duplicated",
            )
        accepted[pair_hash] = source_query_ordinal
        accepted_ordinals.add(source_query_ordinal)
    return accepted


def _is_entry_infrastructure_exception(exc: BaseException) -> bool:
    return _entry_infrastructure_error_kind(exc) is not None


def _entry_infrastructure_error_kind(exc: BaseException) -> ErrorKind | None:
    if isinstance(exc, (QueryTimeoutError, QueryExecutionError)):
        return None
    if isinstance(exc, TimeoutError):
        return ErrorKind.DRIVER_TIMEOUT
    if isinstance(exc, WorkerFailure):
        return exc.kind if exc.kind in {
            ErrorKind.DBMS_UNAVAILABLE,
            ErrorKind.DBMS_CRASHED,
            ErrorKind.DRIVER_TIMEOUT,
            ErrorKind.WORKER_HUNG,
            ErrorKind.WORKER_CRASHED,
            ErrorKind.INFRASTRUCTURE_ERROR,
        } else None
    if isinstance(exc, (MemoryError, OSError, AdapterError)):
        return ErrorKind.INFRASTRUCTURE_ERROR
    return None


def _write_entry_quarantine(
    entry_dir: Path,
    entry: RulePlanEntry,
    *,
    query_a: str,
    query_b: str | None,
    reason: str,
    transform_metadata: Mapping[str, object],
) -> None:
    _append_jsonl(
        entry_dir / "rule_quarantine.jsonl",
        {
            "entry_id": entry.entry_id,
            "family": entry.family,
            "proof_version": entry.proof_version,
            "query_a": query_a,
            "query_a_hash": _query_hash(query_a),
            "query_b": query_b,
            "query_b_hash": _query_hash(query_b) if query_b is not None else None,
            "reason": reason,
            "rule_id": entry.rule_id,
            "stream": entry.stream.value,
            "transform_metadata": to_jsonable(transform_metadata),
        },
    )


def _dialect_for(target: TargetSystem):
    if target is TargetSystem.AGENSGRAPH:
        return AgensGraphDialect()
    if target is TargetSystem.APACHE_AGE:
        return ApacheAgeDialect()
    return NativeCypherDialect(
        multiple_node_labels=target in {TargetSystem.NEO4J, TargetSystem.MEMGRAPH, TargetSystem.FALKORDB}
    )


def _dialect_capability_hash(capabilities: object) -> str:
    values = vars(capabilities)
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _certificate_payload(certificate: object | None) -> dict[str, object] | None:
    if certificate is None:
        return None
    if not is_dataclass(certificate):
        raise TypeError("safety certificate must be a dataclass")
    return asdict(certificate)


def _validation_certificate(pair: EquivalentQueryPair) -> TransformSafetyCertificate:
    if not pair.rule_id or pair.safety_certificate is None:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate lacks a safety certificate")
    rule = RuleRegistry.default().get(pair.rule_id)
    if not isinstance(pair.safety_certificate, TransformSafetyCertificate):
        raise WorkerFailure(
            ErrorKind.INFRASTRUCTURE_ERROR,
            "screen candidate certificate is not the canonical certificate type",
        )
    proof_id = pair.safety_certificate.proof_id
    ast_path = str(getattr(pair.safety_certificate, "ast_path", ""))
    if proof_id != rule.proof_id or not ast_path:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate certificate does not match rule registry")
    site_class = next((site for site in rule.site_classes if _site_matches_path(site, ast_path)), None)
    if site_class is None and len(rule.site_classes) == 1:
        site_class = rule.site_classes[0]
    if site_class is None:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate site class is ambiguous")
    if (
        pair.safety_certificate.rule_id != rule.rule_id
        or pair.safety_certificate.tier != rule.tier.value
        or pair.safety_certificate.proof_version != rule.proof_version
        or pair.safety_certificate.site_class != site_class
    ):
        raise WorkerFailure(
            ErrorKind.INFRASTRUCTURE_ERROR,
            "screen candidate canonical certificate facts drifted",
        )
    return pair.safety_certificate


def _site_matches_path(site_class: str, ast_path: str) -> bool:
    markers = {
        "where_boolean": ".where",
        "match_pattern_component": ".pattern.components[",
        "bound_variable": ".variables[",
        "mandatory_match_clause": ".mandatory_match",
    }
    marker = markers.get(site_class)
    return bool(marker and marker in ast_path)


def _candidate_resolved_config(spec: ExperimentSpec, task: ShardTask) -> dict[str, object]:
    system = spec.systems[task.target]
    return {
        "experiment_schema_version": spec.schema_version,
        "target": task.target.value,
        "shard_index": task.shard_index,
        "runtime": dict(spec.runtime),
        "validation": dict(spec.validation),
        "rules_schema_sha256": spec.rules_schema_sha256,
        "system": {
            "capabilities": system.capabilities,
            "dialect": system.dialect,
            "graph_isolation": system.graph_isolation,
            "default_timeout_seconds": system.default_timeout_seconds,
        },
    }


def _candidate_validation_policy(spec: ExperimentSpec, task: ShardTask) -> ValidationPolicy:
    validation = spec.validation
    timeout = float(spec.systems[task.target].default_timeout_seconds)
    return ValidationPolicy(
        noise_floor_ms=0.0,
        timer_resolution_ms=0.001,
        min_ratio=float(validation["min_ratio"]),
        min_ci_low=float(validation["min_ci_lower_ratio"]),
        max_result_rows=int(validation["max_result_rows"]),
        query_timeout_seconds=timeout,
        session_timeout_seconds=max(300.0, timeout * 10.0),
    )


def _is_truncated(result: Any, max_rows: int) -> bool:
    return bool(getattr(result, "summary", {}).get("truncated")) or len(result.rows) > max_rows


def _has_order_by(query: str) -> bool:
    return bool(re.search(r"\bORDER\s+BY\b", query, flags=re.IGNORECASE))


def _query_hash(query: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", query).strip().encode("utf-8")).hexdigest()


def _pair_hash(query_a: str, query_b: str) -> str:
    ordered = sorted((portable_ast_hash(query_a), portable_ast_hash(query_b)))
    return hashlib.sha256(":".join(ordered).encode("ascii")).hexdigest()


def _graph_snapshot_hash(statements: tuple[str, ...]) -> str:
    return hashlib.sha256(_canonical_graph_payload(statements)).hexdigest()


def _canonical_graph_payload(statements: tuple[str, ...] | list[str]) -> bytes:
    normalized = tuple(statement.strip() for statement in statements if statement.strip())
    return "\n".join(normalized).encode("utf-8")


def _seal_generation_corpus(
    path: Path,
    *,
    task_identity: str,
    generated: GeneratedShard,
    require_unique_query_asts: bool = False,
) -> None:
    if require_unique_query_asts:
        _require_unique_entry_query_asts(generated.query_texts)
    body = {
        "schema_version": 1,
        "task_identity": task_identity,
        "graph_statements": list(generated.graph_statements),
        "query_texts": list(generated.query_texts),
        "generator_metadata": to_jsonable(generated.generator_metadata),
    }
    if path.exists():
        existing = _load_generation_corpus(
            path,
            task_identity=task_identity,
            require_unique_query_asts=require_unique_query_asts,
        )
        if existing != generated:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "sealed generation corpus changed")
        return
    _atomic_write_json(path, {**body, "content_hash": canonical_hash(body)})


def _load_generation_corpus(
    path: Path,
    *,
    task_identity: str,
    require_unique_query_asts: bool = False,
) -> GeneratedShard | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "generation corpus is unreadable") from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "generation corpus is not an object")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    graph = body.get("graph_statements")
    queries = body.get("query_texts")
    metadata = body.get("generator_metadata")
    if (
        body.get("schema_version") != 1
        or body.get("task_identity") != task_identity
        or payload.get("content_hash") != canonical_hash(body)
        or not isinstance(graph, list)
        or not graph
        or not all(isinstance(statement, str) and statement.strip() for statement in graph)
        or not isinstance(queries, list)
        or not queries
        or not all(isinstance(query, str) and query.strip() for query in queries)
        or not isinstance(metadata, dict)
    ):
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "generation corpus identity or hash is invalid")
    generated = GeneratedShard(tuple(graph), tuple(queries), metadata)
    if require_unique_query_asts:
        _require_unique_entry_query_asts(generated.query_texts)
    return generated


def _require_unique_entry_query_asts(queries: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for query in queries:
        try:
            ast_hash = portable_ast_hash(query)
        except (TypeError, ValueError) as exc:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "rule entry query corpus contains an invalid portable AST",
            ) from exc
        if ast_hash in seen:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "rule entry query corpus contains a duplicate portable AST",
            )
        seen.add(ast_hash)


def _runtime_evidence_identity(
    *,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
    dbms_health_epoch: str,
) -> dict[str, str]:
    identity = {
        "graph_snapshot_hash": graph_snapshot_hash,
        "dbms_fingerprint": dbms_fingerprint,
        "dbms_health_epoch": dbms_health_epoch,
    }
    if any(not isinstance(value, str) or len(value) != 64 for value in identity.values()):
        raise WorkerFailure(
            ErrorKind.INFRASTRUCTURE_ERROR,
            "runtime evidence identity values must be 64-character digests",
        )
    return identity


def _entry_sample_upper_bound_us(entry: RulePlanEntry) -> int:
    return math.ceil(float(entry.query_timeout_seconds) * 1_000_000)


def _load_entry_runtime_invalidation(
    entry_dir: Path,
    *,
    entry: RulePlanEntry,
) -> dict[str, object] | None:
    path = entry_dir / "runtime_invalidation.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "runtime invalidation tombstone is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "runtime invalidation tombstone is not an object",
        )
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    identity = body.get("evidence_identity")
    if (
        set(body) != {
            "entry_id",
            "evidence_identity",
            "observed",
            "reason",
            "schema_version",
        }
        or body.get("schema_version") != 1
        or body.get("entry_id") != entry.entry_id
        or not isinstance(body.get("reason"), str)
        or not body["reason"]
        or not isinstance(identity, dict)
        or set(identity) != {
            "dbms_fingerprint",
            "dbms_health_epoch",
            "graph_snapshot_hash",
        }
        or any(not isinstance(value, str) or len(value) != 64 for value in identity.values())
        or payload.get("content_hash") != canonical_hash(body)
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "runtime invalidation tombstone is malformed or has a bad hash",
        )
    return body


def _write_entry_runtime_invalidation(
    entry_dir: Path,
    *,
    entry: RulePlanEntry,
    evidence_identity: Mapping[str, object],
    reason: str,
    observed: object,
) -> None:
    path = entry_dir / "runtime_invalidation.json"
    existing = _load_entry_runtime_invalidation(entry_dir, entry=entry)
    if existing is not None:
        return
    body = {
        "schema_version": 1,
        "entry_id": entry.entry_id,
        "evidence_identity": dict(evidence_identity),
        "reason": reason,
        "observed": to_jsonable(observed),
    }
    _atomic_write_json(path, {**body, "content_hash": canonical_hash(body)})


def _load_entry_progress(path: Path, *, entry: RulePlanEntry) -> dict[str, object]:
    default: dict[str, object] = {
        "schema_version": 6,
        "entry_id": entry.entry_id,
        "elapsed_seconds": 0.0,
        "last_query_ordinal": 0,
        "attempted_query_records": 0,
        "applicable_query_ordinals": [],
        "reject_counts": {},
        "accepted_pair_hashes": [],
        "pending_acceptances": [],
        "legacy_migration_sha256": None,
        "evidence_identity": None,
        "stage1_intent": None,
        "accepted_record_ledger": [],
        "noise_floor_id": None,
        "noise_intent": None,
        "noise_failure_id": None,
        "noise_failure_intent": None,
        "_loaded_schema_version": 6,
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "acceptance progress is unreadable") from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "acceptance progress is not an object")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    schema_version = body.get("schema_version")
    elapsed = body.get("elapsed_seconds")
    last_ordinal = body.get("last_query_ordinal")
    attempted = body.get("attempted_query_records")
    applicable_query_ordinals = body.get("applicable_query_ordinals", [])
    rejects = body.get("reject_counts")
    accepted = body.get("accepted_pair_hashes")
    pending = body.get("pending_acceptances", [])
    legacy_migration_sha256 = body.get("legacy_migration_sha256")
    evidence_identity = body.get("evidence_identity")
    stage1_intent = body.get("stage1_intent")
    accepted_record_ledger = body.get("accepted_record_ledger", [])
    noise_floor_id = body.get("noise_floor_id")
    noise_intent = body.get("noise_intent")
    noise_failure_id = body.get("noise_failure_id")
    noise_failure_intent = body.get("noise_failure_intent")
    max_sample_us = _entry_sample_upper_bound_us(entry)
    valid_rejects = isinstance(rejects, dict) and all(
        isinstance(key, str)
        and key
        and type(value) is int
        and value >= 0
        for key, value in rejects.items()
    )
    valid_accepted = isinstance(accepted, list) and all(
        isinstance(value, str) and len(value) == 64 for value in accepted
    ) and len(set(accepted)) == len(accepted)
    valid_applicable_query_ordinals = (
        isinstance(applicable_query_ordinals, list)
        and all(
            type(value) is int
            and 1 <= value <= entry.max_generated_seed_budget
            for value in applicable_query_ordinals
        )
        and applicable_query_ordinals == sorted(applicable_query_ordinals)
        and len(set(applicable_query_ordinals))
        == len(applicable_query_ordinals)
    )
    valid_pending = isinstance(pending, list) and all(
        isinstance(value, dict)
        and set(value) == {
            "acceptance_a_us",
            "acceptance_b_us",
            "pair_hash",
            "source_query_ordinal",
        }
        and isinstance(value["pair_hash"], str)
        and len(value["pair_hash"]) == 64
        and type(value["source_query_ordinal"]) is int
        and 1 <= value["source_query_ordinal"] <= entry.max_generated_seed_budget
        and type(value["acceptance_a_us"]) is int
        and 0 <= value["acceptance_a_us"] <= max_sample_us
        and type(value["acceptance_b_us"]) is int
        and 0 <= value["acceptance_b_us"] <= max_sample_us
        for value in pending
    )
    pending_hashes = [value["pair_hash"] for value in pending] if valid_pending else []
    pending_ordinals = [value["source_query_ordinal"] for value in pending] if valid_pending else []
    valid_pending = bool(
        valid_pending
        and len(set(pending_hashes)) == len(pending_hashes)
        and len(set(pending_ordinals)) == len(pending_ordinals)
        and not set(pending_hashes).intersection(accepted if isinstance(accepted, list) else ())
    )
    valid_migration = legacy_migration_sha256 is None or (
        isinstance(legacy_migration_sha256, str) and len(legacy_migration_sha256) == 64
    )
    valid_identity = evidence_identity is None or (
        isinstance(evidence_identity, dict)
        and set(evidence_identity) == {
            "dbms_fingerprint",
            "dbms_health_epoch",
            "graph_snapshot_hash",
        }
        and all(
            isinstance(value, str) and len(value) == 64
            for value in evidence_identity.values()
        )
    )
    valid_intent = stage1_intent is None or (
        isinstance(stage1_intent, dict)
        and set(stage1_intent) == {
            "pair_hash",
            "record_digest",
            "source_query_ordinal",
        }
        and isinstance(stage1_intent["pair_hash"], str)
        and len(stage1_intent["pair_hash"]) == 64
        and isinstance(stage1_intent["record_digest"], str)
        and len(stage1_intent["record_digest"]) == 64
        and type(stage1_intent["source_query_ordinal"]) is int
        and 1 <= stage1_intent["source_query_ordinal"] <= entry.max_generated_seed_budget
    )
    valid_ledger = isinstance(accepted_record_ledger, list) and all(
        isinstance(value, dict)
        and set(value) == {
            "acceptance_a_us",
            "acceptance_b_us",
            "pair_hash",
            "record_digest",
            "source_query_ordinal",
        }
        and isinstance(value["pair_hash"], str)
        and len(value["pair_hash"]) == 64
        and isinstance(value["record_digest"], str)
        and len(value["record_digest"]) == 64
        and type(value["source_query_ordinal"]) is int
        and 1 <= value["source_query_ordinal"] <= entry.max_generated_seed_budget
        and type(value["acceptance_a_us"]) is int
        and 0 <= value["acceptance_a_us"] <= max_sample_us
        and type(value["acceptance_b_us"]) is int
        and 0 <= value["acceptance_b_us"] <= max_sample_us
        for value in accepted_record_ledger
    )
    ledger_hashes = (
        [value["pair_hash"] for value in accepted_record_ledger]
        if valid_ledger
        else []
    )
    ledger_ordinals = (
        [value["source_query_ordinal"] for value in accepted_record_ledger]
        if valid_ledger
        else []
    )
    valid_ledger = bool(
        valid_ledger
        and len(set(ledger_hashes)) == len(ledger_hashes)
        and len(set(ledger_ordinals)) == len(ledger_ordinals)
        and ledger_ordinals == sorted(ledger_ordinals)
        and set(accepted if isinstance(accepted, list) else ()) == set(ledger_hashes)
        and len(accepted if isinstance(accepted, list) else ()) == len(ledger_hashes)
    )
    valid_noise_floor = noise_floor_id is None or (
        isinstance(noise_floor_id, str) and len(noise_floor_id) == 64
    )
    valid_noise_intent = noise_intent is None or (
        isinstance(noise_intent, dict)
        and set(noise_intent) == {"noise_floor_id", "probe_pair_hashes"}
        and isinstance(noise_intent["noise_floor_id"], str)
        and len(noise_intent["noise_floor_id"]) == 64
        and isinstance(noise_intent["probe_pair_hashes"], list)
        and len(noise_intent["probe_pair_hashes"]) == 20
        and len(set(noise_intent["probe_pair_hashes"])) == 20
        and all(
            isinstance(value, str) and len(value) == 64
            for value in noise_intent["probe_pair_hashes"]
        )
    )
    valid_noise_failure_id = noise_failure_id is None or (
        isinstance(noise_failure_id, str) and len(noise_failure_id) == 64
    )
    valid_noise_failure_intent = noise_failure_intent is None or (
        isinstance(noise_failure_intent, dict)
        and set(noise_failure_intent) == {"noise_failure_id"}
        and isinstance(noise_failure_intent["noise_failure_id"], str)
        and len(noise_failure_intent["noise_failure_id"]) == 64
    )
    schema_keys = {
        1: {
            "accepted_pair_hashes",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "last_query_ordinal",
            "reject_counts",
            "schema_version",
        },
        2: {
            "accepted_pair_hashes",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "last_query_ordinal",
            "legacy_migration_sha256",
            "pending_acceptances",
            "reject_counts",
            "schema_version",
        },
        3: {
            "accepted_pair_hashes",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "evidence_identity",
            "last_query_ordinal",
            "legacy_migration_sha256",
            "pending_acceptances",
            "reject_counts",
            "schema_version",
            "stage1_intent",
        },
        4: {
            "accepted_pair_hashes",
            "accepted_record_ledger",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "evidence_identity",
            "last_query_ordinal",
            "legacy_migration_sha256",
            "noise_floor_id",
            "noise_intent",
            "pending_acceptances",
            "reject_counts",
            "schema_version",
            "stage1_intent",
        },
        5: {
            "accepted_pair_hashes",
            "accepted_record_ledger",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "evidence_identity",
            "last_query_ordinal",
            "legacy_migration_sha256",
            "noise_failure_id",
            "noise_failure_intent",
            "noise_floor_id",
            "noise_intent",
            "pending_acceptances",
            "reject_counts",
            "schema_version",
            "stage1_intent",
        },
        6: {
            "accepted_pair_hashes",
            "accepted_record_ledger",
            "applicable_query_ordinals",
            "attempted_query_records",
            "elapsed_seconds",
            "entry_id",
            "evidence_identity",
            "last_query_ordinal",
            "legacy_migration_sha256",
            "noise_failure_id",
            "noise_failure_intent",
            "noise_floor_id",
            "noise_intent",
            "pending_acceptances",
            "reject_counts",
            "schema_version",
            "stage1_intent",
        },
    }
    if (
        schema_version not in {1, 2, 3, 4, 5, 6}
        or set(body) != schema_keys.get(schema_version, set())
        or body.get("entry_id") != entry.entry_id
        or payload.get("content_hash") != canonical_hash(body)
        or type(elapsed) not in {int, float}
        or elapsed < 0
        or elapsed != elapsed
        or type(last_ordinal) is not int
        or last_ordinal < 0
        or last_ordinal > entry.max_generated_seed_budget
        or type(attempted) is not int
        or attempted != last_ordinal
        or not valid_rejects
        or not valid_accepted
        or (
            schema_version == 1
            and last_ordinal == 0
            and bool(accepted)
        )
        or (schema_version in {2, 3, 4, 5, 6} and (not valid_pending or not valid_migration))
        or (schema_version in {3, 4, 5, 6} and (not valid_identity or not valid_intent))
        or (
            schema_version == 3
            and bool(accepted or pending or stage1_intent)
        )
        or (
            schema_version in {4, 5, 6}
            and (not valid_ledger or not valid_noise_floor or not valid_noise_intent)
        )
        or (
            schema_version in {4, 5, 6}
            and (
                any(value > last_ordinal for value in ledger_ordinals)
                or any(value > last_ordinal for value in pending_ordinals)
            )
        )
        or (
            schema_version in {5, 6}
            and (not valid_noise_failure_id or not valid_noise_failure_intent)
        )
        or (
            schema_version in {4, 5, 6}
            and noise_floor_id is not None
            and noise_intent is not None
        )
        or (
            schema_version in {5, 6}
            and noise_failure_id is not None
            and noise_failure_intent is not None
        )
        or (
            schema_version in {5, 6}
            and (noise_failure_id is not None or noise_failure_intent is not None)
            and (noise_floor_id is not None or noise_intent is not None)
        )
        or (
            schema_version == 6
            and (
                not valid_applicable_query_ordinals
                or any(
                    value > last_ordinal + 1
                    for value in applicable_query_ordinals
                )
                or sum(
                    value > last_ordinal
                    for value in applicable_query_ordinals
                )
                > 1
            )
        )
    ):
        raise WorkerFailure(
            ErrorKind.EVIDENCE_INTEGRITY_ERROR,
            "acceptance progress identity, hash, or counters are invalid",
        )
    if schema_version == 1:
        body["pending_acceptances"] = []
        body["legacy_migration_sha256"] = None
        body["evidence_identity"] = None
        body["stage1_intent"] = None
    elif schema_version == 2:
        body["evidence_identity"] = None
        body["stage1_intent"] = None
    if schema_version in {1, 2, 3}:
        body["accepted_record_ledger"] = []
        body["noise_floor_id"] = None
        body["noise_intent"] = None
    if schema_version in {1, 2, 3, 4}:
        body["noise_failure_id"] = None
        body["noise_failure_intent"] = None
    if schema_version in {1, 2, 3, 4, 5}:
        if last_ordinal > 0:
            raise WorkerFailure(
                ErrorKind.EVIDENCE_INTEGRITY_ERROR,
                "legacy acceptance progress lacks an applicable-query ledger",
            )
        body["applicable_query_ordinals"] = []
    body["_loaded_schema_version"] = schema_version
    return body


def _persist_entry_progress(
    path: Path,
    *,
    entry: RulePlanEntry,
    prior_elapsed: float,
    attempt_started: float,
    now: float,
    last_query_ordinal: int,
    applicable_query_ordinals: tuple[int, ...],
    rejects: Mapping[str, int],
    accepted_hashes: tuple[str, ...],
    pending_acceptances: tuple[Mapping[str, object], ...] = (),
    legacy_migration_sha256: object | None = None,
    evidence_identity: Mapping[str, object] | None = None,
    stage1_intent: Mapping[str, object] | None = None,
    accepted_record_ledger: tuple[Mapping[str, object], ...] = (),
    noise_floor_id: str | None = None,
    noise_intent: Mapping[str, object] | None = None,
    noise_failure_id: str | None = None,
    noise_failure_intent: Mapping[str, object] | None = None,
) -> None:
    elapsed = prior_elapsed + max(0.0, now - attempt_started)
    body = {
        "schema_version": 6,
        "entry_id": entry.entry_id,
        "elapsed_seconds": elapsed,
        "last_query_ordinal": last_query_ordinal,
        "attempted_query_records": last_query_ordinal,
        "applicable_query_ordinals": list(
            sorted(applicable_query_ordinals)
        ),
        "reject_counts": dict(sorted(rejects.items())),
        "accepted_pair_hashes": list(sorted(accepted_hashes)),
        "pending_acceptances": sorted(
            (dict(value) for value in pending_acceptances),
            key=lambda value: int(value["source_query_ordinal"]),
        ),
        "legacy_migration_sha256": legacy_migration_sha256,
        "evidence_identity": dict(evidence_identity) if evidence_identity is not None else None,
        "stage1_intent": dict(stage1_intent) if stage1_intent is not None else None,
        "accepted_record_ledger": [dict(value) for value in accepted_record_ledger],
        "noise_floor_id": noise_floor_id,
        "noise_intent": dict(noise_intent) if noise_intent is not None else None,
        "noise_failure_id": noise_failure_id,
        "noise_failure_intent": (
            dict(noise_failure_intent) if noise_failure_intent is not None else None
        ),
    }
    _atomic_write_json(path, {**body, "content_hash": canonical_hash(body)})


def _rule_stats_from_entry_progress(
    entry: RulePlanEntry,
    progress: Mapping[str, object],
) -> dict[str, dict[str, int]]:
    accepted = len(progress["accepted_pair_hashes"])
    not_applicable = int(dict(progress["reject_counts"]).get("RULE_NOT_APPLICABLE", 0))
    return {
        "accepted": ({entry.rule_id: accepted} if accepted else {}),
        "not_applicable": ({entry.rule_id: not_applicable} if not_applicable else {}),
    }


def _entry_outcome_from_progress(
    entry: RulePlanEntry,
    progress: Mapping[str, object],
) -> dict[str, object]:
    return {
        "accepted_hashes": tuple(progress["accepted_pair_hashes"]),
        "applicable_query_ordinals": tuple(
            progress["applicable_query_ordinals"]
        ),
        "pending_acceptances": tuple(progress["pending_acceptances"]),
        "legacy_migration_sha256": progress["legacy_migration_sha256"],
        "evidence_identity": progress["evidence_identity"],
        "stage1_intent": progress["stage1_intent"],
        "accepted_record_ledger": tuple(progress["accepted_record_ledger"]),
        "noise_floor_id": progress["noise_floor_id"],
        "noise_intent": progress["noise_intent"],
        "generated_seeds": int(progress["attempted_query_records"]),
        "rejects": dict(progress["reject_counts"]),
        "rule_stats": _rule_stats_from_entry_progress(entry, progress),
    }


def _adapter_dbms_fingerprint(adapter: Any) -> DBMSFingerprint:
    fingerprint = getattr(adapter, "dbms_fingerprint", None)
    if not callable(fingerprint):
        return DBMSFingerprint(None, "adapter_fingerprint_contract_missing")
    result = fingerprint()
    if not isinstance(result, DBMSFingerprint):
        return DBMSFingerprint(None, "adapter_fingerprint_contract_invalid")
    return result


def _adapter_dbms_health_epoch(adapter: Any) -> DBMSHealthEpoch:
    probe = getattr(adapter, "dbms_health_epoch", None)
    if not callable(probe):
        return DBMSHealthEpoch(None, reason="adapter_health_epoch_contract_missing")
    result = probe()
    if not isinstance(result, DBMSHealthEpoch):
        return DBMSHealthEpoch(None, reason="adapter_health_epoch_contract_invalid")
    return result


def _append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    _append_jsonl_fsync(path, payload)


def _ensure_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _jsonl_hashes(path: Path, key: str) -> set[str]:
    hashes: set[str] = set()
    for record in _read_jsonl(path):
        value = record.get(key)
        if isinstance(value, str):
            hashes.add(value)
    return hashes


def _accepted_pair_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for record in _read_jsonl(path):
        pair_hash = record.get("pair_hash")
        if not isinstance(pair_hash, str):
            continue
        hashes.add(pair_hash)
    return hashes


def _accepted_source_ast_counts(path: Path) -> Counter[str]:
    hashes: Counter[str] = Counter()
    for record in _read_jsonl(path):
        value = record.get("query_hash")
        if isinstance(value, str):
            hashes[value] += 1
    return hashes


def _bound_query_timeout_hashes(
    path: Path,
    *,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
) -> set[str]:
    hashes: set[str] = set()
    required = {"attempt", "dbms_fingerprint", "error_kind", "graph_hash", "query_hash"}
    for record in _read_jsonl(path):
        if set(record) != required:
            raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "query timeout quarantine record is invalid")
        query_hash = record.get("query_hash")
        if (
            record.get("error_kind") != ErrorKind.QUERY_TIMEOUT.value
            or not isinstance(record.get("attempt"), int)
            or isinstance(record.get("attempt"), bool)
            or not isinstance(query_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", query_hash) is None
        ):
            raise WorkerFailure(ErrorKind.EVIDENCE_INTEGRITY_ERROR, "query timeout quarantine values are invalid")
        if (
            record.get("graph_hash") == graph_snapshot_hash
            and record.get("dbms_fingerprint") == dbms_fingerprint
        ):
            hashes.add(query_hash)
    return hashes


def _bind_accepted_identity(
    path: Path,
    *,
    records_path: Path,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
) -> None:
    expected = {
        "schema_version": 1,
        "graph_snapshot_hash": graph_snapshot_hash,
        "dbms_fingerprint": dbms_fingerprint,
    }
    if not path.exists():
        if records_path.exists() and records_path.stat().st_size:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                "accepted-pair identity is missing for existing records",
            )
        _atomic_write_json(path, expected)
        return
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "accepted-pair identity is corrupt") from exc
    if not isinstance(actual, dict) or actual.get("graph_snapshot_hash") != graph_snapshot_hash:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "accepted-pair graph hash changed across resume")
    if actual.get("dbms_fingerprint") != dbms_fingerprint:
        raise WorkerFailure(ErrorKind.DBMS_CRASHED, "accepted-pair DBMS fingerprint changed across resume")


def _read_jsonl(
    path: Path,
    *,
    error_kind: ErrorKind = ErrorKind.INFRASTRUCTURE_ERROR,
    context: str = "append-only record",
) -> Iterable[dict[str, object]]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise WorkerFailure(error_kind, f"non-object {context} in {path}")
                yield value
    except FileNotFoundError:
        return
    except WorkerFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(error_kind, f"invalid {context} in {path}") from exc


def _load_terminal_candidate_outcome(
    path: Path,
    *,
    store: ContentAddressedStore,
    candidate_hash: str,
    candidate_ref: object,
) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("candidate_hash") != candidate_hash:
            raise ValueError("candidate outcome identity mismatch")
        stored_ref = value.get("candidate_ref")
        expected_ref = asdict(candidate_ref) if is_dataclass(candidate_ref) else dict(candidate_ref)
        if stored_ref != expected_ref:
            raise ValueError("candidate outcome reference mismatch")
        record = verify_candidate_record_reference(
            store,
            {
                **load_artifact_record(store, stored_ref).to_dict(),
                "record_ref": stored_ref,
            },
        )
        if record.payload.get("candidate_hash") != candidate_hash:
            raise ValueError("candidate outcome breaks the candidate hash chain")
        validation = value.get("validation")
        reduction = value.get("reduction")
        if (
            not isinstance(validation, dict)
            or validation.get("stage") not in {"validation_passed", "rejected", "inconclusive", "invalid"}
            or not isinstance(reduction, dict)
            or reduction.get("status")
            not in {"not_applicable", "reduced_valid", "reduction_partial", "reduction_invalidated"}
        ):
            raise ValueError("candidate outcome is not terminal")
        verify_candidate_outcome_validation_chain(store, value)
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            ErrorKind.INFRASTRUCTURE_ERROR,
            f"cannot resume terminal candidate outcome: {type(exc).__name__}",
        ) from exc


def _terminal_candidate_outcomes_by_pair(
    root: Path,
    *,
    store: ContentAddressedStore,
) -> dict[str, tuple[dict[str, object], dict[str, object]]]:
    outcomes: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    if not root.is_dir():
        return outcomes
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("candidate outcome must be an object")
            candidate_hash = raw.get("candidate_hash")
            candidate_ref = raw.get("candidate_ref")
            pair_hash = raw.get("pair_hash")
            if (
                not isinstance(candidate_hash, str)
                or not isinstance(candidate_ref, dict)
                or not isinstance(pair_hash, str)
            ):
                raise ValueError("candidate outcome identity is incomplete")
            outcome = _load_terminal_candidate_outcome(
                path,
                store=store,
                candidate_hash=candidate_hash,
                candidate_ref=candidate_ref,
            )
            record = {
                **load_artifact_record(store, candidate_ref).to_dict(),
                "record_ref": candidate_ref,
            }
            if record.get("pair_hash") != pair_hash:
                raise ValueError("candidate outcome pair hash mismatch")
            if pair_hash in outcomes:
                raise ValueError("multiple terminal candidates bind one accepted pair")
            outcomes[pair_hash] = (record, outcome)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkerFailure(
                ErrorKind.INFRASTRUCTURE_ERROR,
                f"cannot index terminal candidate outcomes: {type(exc).__name__}",
            ) from exc
    return outcomes


def _gdsmith_connection(target: TargetSystem, env: Mapping[str, str]) -> tuple[str, str, int, str, str]:
    if target is TargetSystem.NEO4J:
        host, port = neo4j_uri_to_host_port(_required(env, "GRAPHSYMPERF_NEO4J_URI"))
        return "neo4j", host, port, _required(env, "GRAPHSYMPERF_NEO4J_USER"), _required(env, "GRAPHSYMPERF_NEO4J_PASSWORD")
    if target is TargetSystem.MEMGRAPH:
        return "memgraph", _required(env, "GRAPHSYMPERF_MEMGRAPH_HOST"), int(env.get("GRAPHSYMPERF_MEMGRAPH_PORT", "7687")), env.get("GRAPHSYMPERF_MEMGRAPH_USER", ""), env.get("GRAPHSYMPERF_MEMGRAPH_PASSWORD", "")
    if target is TargetSystem.FALKORDB:
        return "redisgraph", _required(env, "GRAPHSYMPERF_FALKORDB_HOST"), int(env.get("GRAPHSYMPERF_FALKORDB_PORT", "6379")), env.get("GRAPHSYMPERF_FALKORDB_USER", ""), env.get("GRAPHSYMPERF_FALKORDB_PASSWORD", "")
    if target is TargetSystem.AGENSGRAPH:
        host, port, user, password = _postgres_connection(_required(env, "GRAPHSYMPERF_AGENSGRAPH_URI"))
        return "agensgraph", host, port, env.get("GRAPHSYMPERF_AGENSGRAPH_USER", user), env.get("GRAPHSYMPERF_AGENSGRAPH_PASSWORD", password)
    if target is TargetSystem.ARCADEDB:
        host, port, user, password = _proxy_connection(env, target)
        return "neo4j", host, port, user, password
    if target is TargetSystem.APACHE_AGE:
        host, port, user, password = _proxy_connection(env, target)
        return "agensgraph", host, port, user, password
    if target is TargetSystem.LADYBUGDB:
        host, port, user, password = _proxy_connection(env, target)
        return "neo4j", host, port, user, password
    raise ValueError(f"no GDsmith generation backend for {target.value}")


def _proxy_connection(env: Mapping[str, str], target: TargetSystem) -> tuple[str, int, str, str]:
    prefix = f"GRAPHSYMPERF_GDSMITH_PROXY_{target.value.upper()}"
    uri = _required(env, f"{prefix}_URI")
    if target is TargetSystem.APACHE_AGE:
        host, port, user, password = _postgres_connection(uri)
    else:
        host, port = neo4j_uri_to_host_port(uri)
        user, password = "", ""
    return host, port, env.get(f"{prefix}_USER", user), env.get(f"{prefix}_PASSWORD", password)


def _postgres_connection(uri: str) -> tuple[str, int, str, str]:
    parsed = urlparse(uri)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError(f"invalid PostgreSQL URI for GDsmith: {uri!r}")
    return parsed.hostname, parsed.port or 5432, unquote(parsed.username or "postgres"), unquote(parsed.password or "")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"missing required generation environment variable {name}")
    return value
