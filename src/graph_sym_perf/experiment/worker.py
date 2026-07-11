"""One shard's runtime-first generation, acceptance, and screening lifecycle.

The worker deliberately owns only a single `(DBMS, shard)` execution context.
It does not use plan or PROFILE APIs, and it stores only minimal accepted-pair
records until a pair earns candidate retention through ordinary runtime screen.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.adapters.lifecycle import reset_graph_state
from graph_sym_perf.artifacts.policy import ArtifactLevel
from graph_sym_perf.artifacts.schema import ArtifactRecord
from graph_sym_perf.artifacts.store import (
    ContentAddressedStore,
    enforce_shard_budget,
    load_artifact_record,
    verify_candidate_record_reference,
)
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import GdsmithConfig, QueryTimeoutError, TargetSystem, to_jsonable
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.base import DecisionKind
from graph_sym_perf.dialects.native import NativeCypherDialect
from graph_sym_perf.equivalence.checker import compare_typed_results
from graph_sym_perf.experiment.plan_models import ExperimentSpec, ShardTask
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import ErrorKind, ShardState, _append_jsonl_fsync, _atomic_write_json, _fsync_directory
from graph_sym_perf.generation.corpus import GraphCorpus, portable_ast_hash
from graph_sym_perf.generation.gdsmith import GdsmithGenerator, GenerationRequest
from graph_sym_perf.gdsmith.replay import gdsmith_read_query_is_replayable
from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port, run_gdsmith
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.reliability.findings import ReliabilityFindingStore
from graph_sym_perf.transforms.provider import CoreRulePairProvider
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    SafetyCertificate as ValidationSafetyCertificate,
    ValidationPolicy,
)
from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPairCorpus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
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
    def __call__(self, task: ShardTask) -> Any: ...


class _GenerationRunner(Protocol):
    def __call__(self, task: ShardTask, raw_dir: Path) -> GeneratedShard: ...


class _CandidatePipeline(Protocol):
    def process(self, candidate: CandidateBundle, **context: object) -> Mapping[str, object]: ...


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
        spec: ExperimentSpec,
        *,
        environment: Mapping[str, str] | None = None,
        generation_runner: _GenerationRunner | None = None,
        adapter_factory: _AdapterFactory | None = None,
        candidate_pipeline: _CandidatePipeline | None = None,
    ) -> None:
        self.spec = spec
        self.environment = dict(os.environ if environment is None else environment)
        self._generation_runner = generation_runner or self._generate_with_gdsmith
        self._adapter_factory = adapter_factory or self._create_adapter
        self._candidate_pipeline = candidate_pipeline
        self._run_dir: Path | None = None
        self._attempt_dir: Path | None = None
        self._attempt_context: AttemptContext | None = None

    def bind_run_dir(self, run_dir: str | Path) -> None:
        self._run_dir = Path(run_dir)

    def bind_attempt_dir(self, attempt_dir: str | Path) -> None:
        self._attempt_dir = Path(attempt_dir)

    def bind_attempt_context(self, context: AttemptContext) -> None:
        self._attempt_context = context
        attempt_dir = getattr(context, "attempt_dir", None)
        if attempt_dir is not None:
            self._attempt_dir = Path(attempt_dir)

    def __call__(self, task: ShardTask) -> dict[str, object]:
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
                    graph_snapshot_hash=graph_hash,
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
        _atomic_write_json(attempt_dir / "summary.json", summary)
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
        _atomic_write_json(attempt_dir / "summary.json", summary)
        return summary

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
            {
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
            },
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
        seen_source_asts = _accepted_source_ast_hashes(records_path)
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
            if source_ast_hash in seen_source_asts:
                return PairValidation(False, "DUPLICATE_AST")
            if len(seen) >= task.accepted_query_pairs:
                return PairValidation(False, "ACCEPTED_TARGET_ALREADY_REACHED")
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
            seen_source_asts.add(source_ast_hash)
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

    def _create_adapter(self, task: ShardTask) -> Any:
        run_config = migrate_experiment_spec_to_run_config(self.spec, task.target)
        system = run_config.systems[0]
        return create_adapter(system, run_config, self.environment, allow_create=True)

    def _generate_with_gdsmith(self, task: ShardTask, raw_dir: Path) -> GeneratedShard:
        backend, host, port, username, password = _gdsmith_connection(task.target, self.environment)
        provenance = _generation_backend_provenance(task.target, backend)
        if task.generation_backend not in {"unspecified", provenance}:
            raise RuntimeError(
                f"planned generation backend {task.generation_backend!r} does not match runtime {provenance!r}"
            )
        requires_single_label = "single_node_label" in self.spec.systems[task.target].capabilities
        generation_query_limit = _generation_query_limit(task, self.spec)
        result_holder: dict[str, object] = {}

        def invoke(request: GenerationRequest) -> tuple[str, ...]:
            # Leave a 30-second supervisor margin so timeout is emitted as an
            # auditable partial corpus instead of a killed worker process.
            timeout_seconds = max(1.0, min(float(task.max_runtime_seconds) - 300.0, 6 * 3600.0))
            result = run_gdsmith(
                config=GdsmithConfig(
                    expected_source_commit=self.spec.generation.expected_commit,
                    backend_extra_args=(
                        {backend: ("--label-num", "1")}
                        if requires_single_label
                        else {}
                    ),
                    timeout_seconds=timeout_seconds,
                    max_raw_artifact_bytes=min(
                        350 * 1024 * 1024,
                        int(self.spec.artifacts["shard_budget_mb"]) * 1024 * 1024 * 7 // 10,
                    ),
                ),
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
                raise RuntimeError(f"GDsmith failed with code {result.returncode}: {reason}")
            if replayable_nonzero_exit and not result.parsed.accepted_queries:
                raise RuntimeError("GDsmith stopped without a replayable query")
            return tuple(record.statement for record in result.parsed.accepted_queries)

        corpus = GdsmithGenerator(invoke).generate(
            GenerationRequest(seed=task.seed, limit=generation_query_limit)
        )
        result = result_holder["result"]
        return GeneratedShard(
            graph_statements=result.parsed.graph_statements,
            query_texts=tuple(query.text for query in corpus.queries),
            generator_metadata={
                "backend": backend,
                "generation_backend": provenance,
                "generation_source": "gdsmith",
                "generation_seed": task.seed,
                "generated_query_record_limit": generation_query_limit,
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


def _generation_query_limit(task: ShardTask, spec: ExperimentSpec) -> int:
    capabilities = spec.systems[task.target].capabilities
    if "very_low_yield_generation" in capabilities:
        multiplier = 10
    elif "low_yield_generation" in capabilities:
        multiplier = 5
    else:
        multiplier = 2
    return min(task.max_generated_seeds, task.accepted_query_pairs * multiplier)


def _generation_backend_provenance(target: TargetSystem, backend: str) -> str:
    if target is TargetSystem.APACHE_AGE:
        if backend != "agensgraph":
            raise ValueError("Apache AGE generation must use the isolated AgensGraph proxy")
        return "agensgraph_proxy"
    if target is TargetSystem.LADYBUGDB:
        return "neo4j_proxy"
    return backend


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


def _validation_certificate(pair: EquivalentQueryPair) -> ValidationSafetyCertificate:
    if not pair.rule_id or pair.safety_certificate is None:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate lacks a safety certificate")
    rule = RuleRegistry.default().get(pair.rule_id)
    proof_id = getattr(pair.safety_certificate, "proof_id", None)
    ast_path = str(getattr(pair.safety_certificate, "ast_path", ""))
    if proof_id != rule.proof_id or not ast_path:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate certificate does not match rule registry")
    site_class = next((site for site in rule.site_classes if _site_matches_path(site, ast_path)), None)
    if site_class is None and len(rule.site_classes) == 1:
        site_class = rule.site_classes[0]
    if site_class is None:
        raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "screen candidate site class is ambiguous")
    return ValidationSafetyCertificate(
        rule_id=rule.rule_id,
        tier=rule.tier.value,
        proof_version=rule.proof_id,
        site_class=site_class,
    )


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
) -> None:
    body = {
        "schema_version": 1,
        "task_identity": task_identity,
        "graph_statements": list(generated.graph_statements),
        "query_texts": list(generated.query_texts),
        "generator_metadata": to_jsonable(generated.generator_metadata),
    }
    if path.exists():
        existing = _load_generation_corpus(path, task_identity=task_identity)
        if existing != generated:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "sealed generation corpus changed")
        return
    _atomic_write_json(path, {**body, "content_hash": canonical_hash(body)})


def _load_generation_corpus(path: Path, *, task_identity: str) -> GeneratedShard | None:
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
    return GeneratedShard(tuple(graph), tuple(queries), metadata)


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


def _accepted_source_ast_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for record in _read_jsonl(path):
        value = record.get("query_hash")
        if isinstance(value, str):
            hashes.add(value)
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


def _read_jsonl(path: Path) -> Iterable[dict[str, object]]:
    try:
        stream = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, f"invalid append-only record in {path}") from exc
            if not isinstance(value, dict):
                raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, f"non-object append-only record in {path}")
            yield value


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
        parsed = urlparse(_required(env, "GRAPHSYMPERF_ARCADEDB_POSTGRES_URI"))
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("GRAPHSYMPERF_ARCADEDB_POSTGRES_URI must be a PostgreSQL URI")
        if parsed.path != "/mydb":
            raise ValueError("GRAPHSYMPERF_ARCADEDB_POSTGRES_URI must select /mydb for the pinned GDsmith provider")
        return (
            "arcadedb",
            parsed.hostname,
            parsed.port or 5432,
            env.get("GRAPHSYMPERF_ARCADEDB_USER", parsed.username or "root"),
            env.get("GRAPHSYMPERF_ARCADEDB_PASSWORD", parsed.password or ""),
        )
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
