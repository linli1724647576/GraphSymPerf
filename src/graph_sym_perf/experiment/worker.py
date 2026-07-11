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
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.adapters.base import DBMSFingerprint, DBMSHealthEpoch
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import GdsmithConfig, QueryTimeoutError, TargetSystem
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.base import DecisionKind
from graph_sym_perf.dialects.native import NativeCypherDialect
from graph_sym_perf.equivalence.checker import compare_typed_results
from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.experiment.plan_models import ExperimentSpec, ShardTask
from graph_sym_perf.experiment.scheduling import AttemptContext
from graph_sym_perf.experiment.state import ErrorKind, ShardState, _append_jsonl_fsync, _atomic_write_json, _fsync_directory
from graph_sym_perf.generation.corpus import GraphCorpus, PortableQuery, portable_ast_hash
from graph_sym_perf.generation.gdsmith import GdsmithGenerator, GenerationRequest
from graph_sym_perf.gdsmith.replay import gdsmith_read_query_is_replayable
from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port, run_gdsmith
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.reliability.findings import ReliabilityFindingStore
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.workload.acceptance import AcceptedPairBuilder
from graph_sym_perf.workload.models import (
    AcceptanceStatus,
    AcceptedPairCorpus,
    AcceptedPairRequest,
    PairProposal,
    PairValidation,
)
from graph_sym_perf.workload.registry import AcceptedCorpusRegistry


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
        inflight = {
            "dbms_fingerprint": self._dbms_fingerprint,
            "deadline": started_at + timeout_seconds,
            "graph_hash": self.graph_hash,
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
    ) -> None:
        self.spec = spec
        self.environment = dict(os.environ if environment is None else environment)
        self._generation_runner = generation_runner or self._generate_with_gdsmith
        self._adapter_factory = adapter_factory or self._create_adapter
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
            generated = self._generation_runner(task, attempt_dir / "gdsmith_raw")
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
                accepted, acceptance, accepted_hashes = self._build_accepted_pairs(
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
                _ensure_jsonl(shard_dir / "accepted_pairs.jsonl")
        finally:
            load_adapter.close()

        candidate_hashes: set[str] = set()
        candidates = 0
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
                candidate_records = self._screen_pairs(measurement_proxy, task, accepted)
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
                existing_candidates = _jsonl_hashes(candidate_path, "pair_hash")
                for record in candidate_records:
                    pair_hash = str(record["pair_hash"])
                    if pair_hash in existing_candidates:
                        continue
                    _append_jsonl(candidate_path, record)
                    existing_candidates.add(pair_hash)
                candidate_hashes = existing_candidates
                candidates = len(candidate_hashes)
            finally:
                measurement_adapter.close()
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
        adapter.execute("MATCH (n) DETACH DELETE n", {}, "shard_reset")
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
    ) -> tuple[list[EquivalentQueryPair], AcceptedPairCorpus, set[str]]:
        accepted: list[EquivalentQueryPair] = []
        max_rows = int(self.spec.validation["max_result_rows"])
        default_timeout = self.spec.systems[task.target].default_timeout_seconds
        acceptance_deadline = time.monotonic() + max_seconds
        records_path = shard_dir / "accepted_pairs.jsonl"
        _ensure_jsonl(records_path)
        seen = _accepted_pair_hashes(
            records_path,
            graph_snapshot_hash=graph_snapshot_hash,
            dbms_fingerprint=dbms_fingerprint,
        )
        seen_source_asts = _accepted_source_ast_hashes(records_path)
        materialized: set[str] = set()

        dialect = _dialect_for(task.target)
        capability_hash = _dialect_capability_hash(dialect.capabilities)

        def provider(context):
            if context.ordinal > len(queries):
                return None
            query = queries[context.ordinal - 1]
            pair = _certified_boolean_pair(query)
            if pair is None:
                return None
            base, transformed = pair
            certificate = SafetyCertificate(
                rule_id="null_complete_excluded_middle",
                ast_path="where[0]",
                variables_in_scope=(),
                expression_type="boolean",
                nullability="three_valued",
                clause_context="where",
                preconditions=("pure_read_query", "three_valued_boolean_identity"),
                proof_id="three_valued_excluded_middle_v1",
                capability_hash=capability_hash,
            )
            return PairProposal(
                PortableQuery(base),
                PortableQuery(transformed),
                certificate.rule_id,
                certificate,
                context.seed,
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
                        )
                    )
                    materialized.add(pair_hash)
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
            )
            accepted.append(accepted_pair)
            materialized.add(pair_hash)
            _append_jsonl(
                records_path,
                {
                    "pair_hash": pair_hash,
                    "source_ast_hash": source_ast_hash,
                    "query_a_ast_hash": portable_ast_hash(base),
                    "query_b_ast_hash": portable_ast_hash(transformed),
                    "query_a_hash": _query_hash(base),
                    "query_b_hash": _query_hash(transformed),
                    "graph_snapshot_hash": graph_snapshot_hash,
                    "dbms_fingerprint": dbms_fingerprint,
                    "rule_id": "null_complete_excluded_middle",
                    "equivalence": "complete_typed_equivalent",
                },
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
        return accepted, corpus, seen

    def _screen_pairs(
        self,
        adapter: Any,
        task: ShardTask,
        pairs: list[EquivalentQueryPair],
    ) -> list[dict[str, object]]:
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
        for index, pair in enumerate(pairs):
            evidence = harness.measure_discovery(
                adapter,
                pair,
                policy=RuntimePolicy(**{**policy.__dict__, "seed": task.seed + index}),
            )
            if evidence.verdict is not RuntimeVerdict.PASS or evidence.median_ratio is None or evidence.median_ratio < threshold:
                continue
            candidates.append(
                {
                    "pair_hash": _pair_hash(pair.query_a, pair.query_b),
                    "query_a": pair.query_a,
                    "query_b": pair.query_b,
                    "parameters": dict(pair.parameters),
                    "graph_snapshot_hash": pair.graph_snapshot_hash,
                    "dbms_fingerprint": pair.dbms_fingerprint,
                    "discovery_median_ratio": evidence.median_ratio,
                    "discovery_median_delta_ms": evidence.median_delta_ms,
                    "discovery_slow_query": evidence.slow_query,
                    "runtime_protocol": "execute-only-screen-v3",
                }
            )
        return candidates

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
        result_holder: dict[str, object] = {}

        def invoke(request: GenerationRequest) -> tuple[str, ...]:
            # Leave a 30-second supervisor margin so timeout is emitted as an
            # auditable partial corpus instead of a killed worker process.
            timeout_seconds = max(1.0, min(float(task.max_runtime_seconds) - 300.0, 6 * 3600.0))
            result = run_gdsmith(
                config=GdsmithConfig(timeout_seconds=timeout_seconds),
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
            if result.returncode not in {0, 124}:
                raise RuntimeError(f"GDsmith failed with code {result.returncode}")
            if result.returncode == 124 and not result.parsed.accepted_queries:
                raise RuntimeError("GDsmith exhausted its time budget without a replayable query")
            return tuple(record.statement for record in result.parsed.accepted_queries)

        corpus = GdsmithGenerator(invoke).generate(
            GenerationRequest(seed=task.seed, limit=task.max_generated_seeds)
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
                "generated_query_record_limit": task.max_generated_seeds,
                "gdsmith_returncode": result.returncode,
                "duplicate_ast_records": len(corpus.duplicate_ast_hashes),
                "generation_stop_reason": (
                    "TIME_BUDGET_EXHAUSTED" if result.returncode == 124 else "PROCESS_COMPLETED"
                ),
            },
        )


def _generation_backend_provenance(target: TargetSystem, backend: str) -> str:
    if target is TargetSystem.APACHE_AGE:
        if backend != "agensgraph":
            raise ValueError("Apache AGE generation must use the isolated AgensGraph proxy")
        return "agensgraph_proxy"
    if target is TargetSystem.LADYBUGDB:
        return "neo4j_proxy"
    return backend


def _certified_boolean_pair(query: str) -> tuple[str, str] | None:
    purity = assess_query_purity(query)
    if not purity.safe or not gdsmith_read_query_is_replayable(query):
        return None
    match = re.search(r"\bWHERE\b(?P<predicate>.*?)(?=\bRETURN\b)", query, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    predicate = match.group("predicate").strip()
    if not predicate:
        return None
    certificate = f"(({predicate}) OR NOT ({predicate}) OR ({predicate}) IS NULL)"
    transformed = query[: match.start("predicate")] + f" ({predicate}) AND {certificate} " + query[match.end("predicate") :]
    return query, transformed


def _dialect_for(target: TargetSystem):
    if target is TargetSystem.AGENSGRAPH:
        return AgensGraphDialect()
    if target is TargetSystem.APACHE_AGE:
        return ApacheAgeDialect()
    return NativeCypherDialect()


def _dialect_capability_hash(capabilities: object) -> str:
    values = vars(capabilities)
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    normalized = tuple(statement.strip() for statement in statements if statement.strip())
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def _accepted_pair_hashes(
    path: Path,
    *,
    graph_snapshot_hash: str,
    dbms_fingerprint: str,
) -> set[str]:
    hashes: set[str] = set()
    for record in _read_jsonl(path):
        pair_hash = record.get("pair_hash")
        if not isinstance(pair_hash, str):
            continue
        if record.get("graph_snapshot_hash") != graph_snapshot_hash:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, "accepted-pair graph hash changed across resume")
        if record.get("dbms_fingerprint") != dbms_fingerprint:
            raise WorkerFailure(ErrorKind.DBMS_CRASHED, "accepted-pair DBMS fingerprint changed across resume")
        hashes.add(pair_hash)
    return hashes


def _accepted_source_ast_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for record in _read_jsonl(path):
        value = record.get("source_ast_hash")
        if isinstance(value, str):
            hashes.add(value)
    return hashes


def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, f"invalid append-only record in {path}") from exc
        if not isinstance(value, dict):
            raise WorkerFailure(ErrorKind.INFRASTRUCTURE_ERROR, f"non-object append-only record in {path}")
        records.append(value)
    return tuple(records)


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
        parsed = urlparse(_required(env, "GRAPHSYMPERF_ARCADEDB_HTTP_URL"))
        if not parsed.hostname:
            raise ValueError("GRAPHSYMPERF_ARCADEDB_HTTP_URL must contain a hostname")
        return "arcadedb", parsed.hostname, parsed.port or 2480, env.get("GRAPHSYMPERF_ARCADEDB_USER", "root"), env.get("GRAPHSYMPERF_ARCADEDB_PASSWORD", "")
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
