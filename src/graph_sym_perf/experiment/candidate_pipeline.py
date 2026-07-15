"""Production runtime-candidate validation and reduction pipeline."""

from __future__ import annotations

import hashlib
import platform
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.adapters.lifecycle import reset_graph_state
from graph_sym_perf.artifacts.chain import ArtifactChainWriter
from graph_sym_perf.artifacts.schema import ArtifactRecord, ArtifactReference
from graph_sym_perf.artifacts.store import ContentAddressedStore
from graph_sym_perf.config import migrate_experiment_spec_to_run_config
from graph_sym_perf.core import TargetSystem
from graph_sym_perf.equivalence.checker import compare_execution_results
from graph_sym_perf.experiment.plan_models import ExperimentSpec, ShardTask
from graph_sym_perf.experiment.provenance import verified_execution_provenance
from graph_sym_perf.reducer import CandidateReducer, ReductionBundle, ReductionStatus, RuleWitness
from graph_sym_perf.reducer.dependencies import analyze_query
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimePolicy
from graph_sym_perf.sampling.yield_store import ValidatedYieldStore
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.issues.durable_registry import committed_owner_outcome, promote_unique_issue
from graph_sym_perf.issues.exporter import export_issue
from graph_sym_perf.issues.receipts import canonical_issue_receipt_payload
from graph_sym_perf.validation.models import CandidateBundle, EquivalenceEvidence, PipelineStage
from graph_sym_perf.validation.promotion import deterministic_issue_id
from graph_sym_perf.validation.validator import IssueValidator
from graph_sym_perf.validation.session_runner import SubprocessSessionRunner


@dataclass(frozen=True)
class _ValidationAdapterFactory:
    spec: ExperimentSpec
    environment: Mapping[str, str]
    target: TargetSystem

    def __call__(self, _candidate: CandidateBundle) -> Any:
        run_config = migrate_experiment_spec_to_run_config(self.spec, self.target)
        return create_adapter(
            run_config.systems[0],
            run_config,
            self.environment,
            allow_create=True,
        )


class _CompleteEquivalenceGate:
    protocol_id = "complete-typed-execute-v1"

    def __init__(self, adapter: Any, candidate: CandidateBundle) -> None:
        self.adapter = adapter
        self.candidate = candidate

    def evaluate(self, pair: EquivalentQueryPair) -> EquivalenceEvidence:
        policy = self.candidate.validation_policy
        left = self.adapter.execute(
            pair.query_a,
            pair.parameters,
            "reducer_equivalence_a",
            max_rows=policy.max_result_rows,
            timeout_seconds=policy.query_timeout_seconds,
        )
        right = self.adapter.execute(
            pair.query_b,
            pair.parameters,
            "reducer_equivalence_b",
            max_rows=policy.max_result_rows,
            timeout_seconds=policy.query_timeout_seconds,
        )
        result = compare_execution_results(
            left,
            right,
            ordered=_has_order_by(pair.query_a) or _has_order_by(pair.query_b),
            max_result_rows=policy.max_result_rows,
        )
        return EquivalenceEvidence(result.equivalent, result.complete_result, result.reason_codes)


class _RuntimeScreen:
    protocol_id = "ordinary-execute-5-pair-v1"

    def __init__(self, adapter: Any, candidate: CandidateBundle) -> None:
        self.adapter = adapter
        self.candidate = candidate

    def evaluate(self, pair: EquivalentQueryPair):
        policy = self.candidate.validation_policy
        runtime_policy = RuntimePolicy(
            warmups=1,
            pairs=5,
            seed=int(hashlib.sha256((pair.query_a + "\0" + pair.query_b).encode()).hexdigest()[:8], 16),
            require_exact_balance=False,
            timeout_ms=policy.query_timeout_seconds * 1000,
        )
        evidence = RuntimeHarness().measure_pair(
            self.adapter,
            pair.query_a,
            pair.query_b,
            parameters=pair.parameters,
            policy=runtime_policy,
            expected_slow_query=self.candidate.discovery_slow_query,
        )
        return replace(
            evidence,
            graph_snapshot_hash=pair.graph_snapshot_hash,
            dbms_fingerprint=pair.dbms_fingerprint,
            measurement_source="ordinary_execute",
            measurement_protocol="ordinary-execute-5-pair-v1",
        )


class _FinalValidator:
    protocol_id = "independent-two-session-v1"

    def __init__(self, adapter_factory: _ValidationAdapterFactory) -> None:
        self.adapter_factory = adapter_factory

    def validate(self, candidate: CandidateBundle):
        return IssueValidator().validate(candidate, SubprocessSessionRunner(self.adapter_factory))


class _ReducerNotApplicable(ValueError):
    """The validated pair cannot be represented by the safe reducer model."""


class ProductionCandidatePipeline:
    """Fail-closed candidate processor used only by the formal experiment entry."""

    def __init__(
        self,
        spec: ExperimentSpec,
        *,
        environment: Mapping[str, str],
        run_dir: str | Path,
    ) -> None:
        self.spec = spec
        self.environment = dict(environment)
        self.run_dir = Path(run_dir)

    def process(self, candidate: CandidateBundle, **context: object) -> Mapping[str, object]:
        task = context.get("task")
        store = context.get("artifact_store")
        candidate_ref = context.get("candidate_ref")
        attempt_dir = context.get("attempt_dir")
        graph_snapshot_ref = context.get("graph_snapshot_ref")
        driver_version = context.get("driver_version")
        if (
            not isinstance(task, ShardTask)
            or not isinstance(store, ContentAddressedStore)
            or not isinstance(candidate_ref, ArtifactReference)
            or not isinstance(attempt_dir, Path)
            or not isinstance(graph_snapshot_ref, Mapping)
            or not isinstance(driver_version, str)
            or not driver_version
        ):
            raise TypeError("candidate pipeline requires a bound shard artifact context")
        adapter_factory = _ValidationAdapterFactory(self.spec, self.environment, task.target)
        validator = IssueValidator(
            yield_store=ValidatedYieldStore(self.run_dir / "validated_yield" / f"{task.target.value}.jsonl")
        )
        validation = validator.validate(candidate, SubprocessSessionRunner(adapter_factory))
        validation_record = ArtifactChainWriter(store).persist_validation(
            validation,
            candidate_ref=candidate_ref,
            environment=_validation_environment(
                candidate,
                task,
                self.environment,
                driver_version=driver_version,
            ),
        )
        validation_ref = asdict(validation_record.reference)
        validation_outcome = {
            "candidate_hash": candidate.candidate_hash,
            "reason_codes": list(validation.reason_codes),
            "stage": validation.stage.value,
            "validation_ref": validation_ref,
        }
        if validation.stage is not PipelineStage.VALIDATION_PASSED:
            return {
                "candidate_hash": candidate.candidate_hash,
                "validation": validation_outcome,
                "reduction": {
                    "candidate_hash": candidate.candidate_hash,
                    "reason": "validation_not_passed",
                    "status": "not_applicable",
                },
            }
        try:
            rule_witness = _rule_witness(candidate)
        except _ReducerNotApplicable as exc:
            reduction_payload = {
                "candidate_hash": candidate.candidate_hash,
                "promotion": "blocked_reducer_not_applicable",
                "reason": str(exc),
                "status": "not_applicable",
            }
            reduction_ref = store.put_json(
                f"reduction-report-{candidate.candidate_hash}.json",
                reduction_payload,
            )
            return {
                "candidate_hash": candidate.candidate_hash,
                "validation": validation_outcome,
                "reduction": {
                    **reduction_payload,
                    "reduction_ref": asdict(reduction_ref),
                },
            }
        reduction = self._reduce(
            candidate,
            validation,
            task=task,
            adapter_factory=adapter_factory,
            checkpoint_path=attempt_dir / f"reducer-{candidate.candidate_hash}.json",
            rule_witness=rule_witness,
        )
        issue_outcome = self._materialize_issue(
            candidate,
            validation,
            reduction,
            task=task,
            store=store,
            original_validation_ref=validation_record.reference,
            graph_snapshot_ref=graph_snapshot_ref,
            environment=_validation_environment(
                candidate,
                task,
                self.environment,
                driver_version=driver_version,
            ),
        )
        if issue_outcome.get("promotion") == "submit_ready" and issue_outcome.get("resumed"):
            validation_outcome["validation_ref"] = issue_outcome["original_validation_ref"]
        resumed_summary = issue_outcome.get("reduction_summary")
        if resumed_summary is not None and not (
            issue_outcome.get("promotion") == "submit_ready"
            and issue_outcome.get("resumed")
            and isinstance(resumed_summary, Mapping)
        ):
            raise ValueError("terminal reduction summary is only valid for a resumed owner")
        summary = (
            dict(resumed_summary)
            if isinstance(resumed_summary, Mapping)
            else _reduction_summary(
                reduction,
                original_candidate_hash=candidate.candidate_hash,
                original_validation_ref=issue_outcome.get("original_validation_ref"),
                reduced_validation_ref=issue_outcome.get("reduced_validation_ref"),
            )
        )
        reduction_ref = store.put_json(
            f"reduction-report-{candidate.candidate_hash}.json",
            summary,
        )
        visible_issue_outcome = {
            name: value
            for name, value in issue_outcome.items()
            if name != "reduction_summary"
        }
        return {
            "candidate_hash": candidate.candidate_hash,
            "validation": validation_outcome,
            "reduction": {
                "candidate_hash": candidate.candidate_hash,
                "reason": summary.get("stop_reason", reduction.stop_reason),
                "reduced_candidate_hash": summary.get(
                    "reduced_candidate_hash",
                    reduction.reduced_candidate_hash or None,
                ),
                "reduction_ref": asdict(reduction_ref),
                "status": summary.get("status", reduction.status.value),
                **visible_issue_outcome,
            },
        }

    def _materialize_issue(
        self,
        original_candidate: CandidateBundle,
        original_validation: object,
        reduction: object,
        *,
        task: ShardTask,
        store: ContentAddressedStore,
        original_validation_ref: ArtifactReference,
        graph_snapshot_ref: Mapping[str, object],
        environment: Mapping[str, object],
    ) -> dict[str, object]:
        final_validation = getattr(reduction, "final_validation", None)
        screen_evidence = getattr(reduction, "screen_evidence", None)
        if final_validation is None:
            if getattr(reduction, "status") is not ReductionStatus.REDUCED_VALID:
                return {"promotion": "not_applicable"}
            return {"promotion": "blocked_missing_reduced_evidence"}
        manifest = final_validation.manifest
        reduced_candidate = CandidateBundle.create(
            manifest.pair,
            graph_snapshot=manifest.graph_snapshot,
            dbms_version=manifest.dbms_version,
            resolved_config=manifest.resolved_config,
            discovery_slow_query=manifest.discovery_slow_query,
            discovery_ratio=manifest.discovery_ratio,
            validation_policy=final_validation.validation_policy,
            certificate=manifest.certificate,
        )
        if reduced_candidate.candidate_hash != reduction.reduced_candidate_hash:
            return {"promotion": "blocked_reduced_candidate_hash_mismatch"}
        reduced_record = _candidate_record(
            reduced_candidate,
            screen_evidence,
            graph_snapshot_ref=graph_snapshot_ref,
        )
        reduced_ref = store.put_json(
            f"candidate-{reduced_candidate.candidate_hash}.json",
            reduced_record,
        )
        final_record = ArtifactChainWriter(store).persist_validation(
            final_validation,
            candidate_ref=reduced_ref,
            environment=environment,
        )
        validation_provenance = {
            "original_validation_hash": reduction.original_validation_hash,
            "reduced_validation_hash": reduction.reduced_validation_hash,
            "original_validation_ref": asdict(original_validation_ref),
            "reduced_validation_ref": asdict(final_record.reference),
        }
        if getattr(reduction, "status") is not ReductionStatus.REDUCED_VALID:
            return {"promotion": "not_applicable", **validation_provenance}
        if screen_evidence is None:
            return {"promotion": "blocked_missing_reduced_evidence", **validation_provenance}
        materialized: dict[str, object] = {}

        def materialize(issue: object) -> dict[str, object]:
            reproducer = (
                "// setup\n"
                + "\n".join(original_candidate.graph_snapshot.statements)
                + "\n\n// reduced original\n"
                + reduction.reduced_pair.query_a
                + "\n\n// reduced transformed\n"
                + reduction.reduced_pair.query_b
                + "\n"
            ).encode("utf-8")
            reproducer_ref = store.put(
                f"reproducer-{issue.issue_id}.cypher",
                reproducer,
                media_type="text/plain; charset=utf-8",
            )
            issue_record = ArtifactChainWriter(store).persist_issue(
                issue,
                original_validation_ref=original_validation_ref,
                final_validation_ref=final_record.reference,
                reproducer_ref=reproducer_ref,
            )
            export_path = self.run_dir / "issues" / "submit-ready" / f"{issue.issue_id}.json"
            export_issue(
                issue,
                export_path,
                original_validation_ref=original_validation_ref,
                reduced_validation_ref=final_record.reference,
            )
            export_hash = hashlib.sha256(export_path.read_bytes()).hexdigest()
            index_path = self.run_dir / "issues" / "index" / f"{issue.issue_id}.json"
            receipt = canonical_issue_receipt_payload(
                {
                    "artifact_store": str(store.root.relative_to(self.run_dir)),
                    "export_path": str(export_path.relative_to(self.run_dir)),
                    "export_sha256": export_hash,
                    "issue_id": issue.issue_id,
                    "issue_ref": asdict(issue_record.reference),
                },
                issue_id=issue.issue_id,
            )
            _atomic_json(index_path, receipt)
            materialized.update(issue_id=issue.issue_id, index_path=index_path)
            return receipt

        issue, duplicate_status = promote_unique_issue(
            self.run_dir / "issues",
            validation=original_validation,
            reduction=reduction,
            certificate=original_candidate.certificate,
            dbms=task.target.value,
            known_regression_manifest=_required_regression_manifest(self.spec),
            original_validation_ref=original_validation_ref,
            reduced_validation_ref=final_record.reference,
            materialize=materialize,
        )
        if issue is None:
            if duplicate_status == "owner_already_committed":
                committed = committed_owner_outcome(
                    self.run_dir / "issues",
                    issue_id=deterministic_issue_id(original_validation, reduction),
                )
                return {
                    "promotion": "submit_ready",
                    "duplicate_status": duplicate_status,
                    "resumed": True,
                    **committed,
                }
            return {
                "promotion": "blocked",
                "duplicate_status": duplicate_status,
                **validation_provenance,
            }
        return {
            "promotion": "submit_ready",
            "issue_id": materialized["issue_id"],
            "issue_index": str(materialized["index_path"].relative_to(self.run_dir)),
            **validation_provenance,
        }

    def _reduce(
        self,
        candidate: CandidateBundle,
        validation: object,
        *,
        task: ShardTask,
        adapter_factory: _ValidationAdapterFactory,
        checkpoint_path: Path,
        rule_witness: RuleWitness,
    ):
        raw_adapter = adapter_factory(candidate)
        from graph_sym_perf.experiment.worker import AdapterQueryProxy

        adapter = AdapterQueryProxy(
            raw_adapter,
            attempt_dir=checkpoint_path.parent,
            graph_hash=candidate.graph_snapshot.content_hash,
            dbms_fingerprint=candidate.pair.dbms_fingerprint,
            default_timeout_seconds=candidate.validation_policy.query_timeout_seconds,
        )
        try:
            connected = adapter.connectivity_check()
            if not isinstance(connected, tuple) or not connected or connected[0] is not True:
                raise RuntimeError("reducer_connection_check_failed")
            reset_graph_state(
                adapter,
                query_kind="reducer_reset",
                timeout_seconds=candidate.validation_policy.query_timeout_seconds,
            )
            adapter.load_gdsmith_graph(candidate.graph_snapshot.statements)
            bundle = ReductionBundle.create(
                original_candidate=candidate,
                original_validation=validation,
                rule_witness=rule_witness,
                equivalence_gate=_CompleteEquivalenceGate(adapter, candidate),
                runtime_screen=_RuntimeScreen(adapter, candidate),
                final_validator=_FinalValidator(adapter_factory),
            )
            reducer_config = self.spec.reducer
            return CandidateReducer(
                max_attempts=int(reducer_config["max_attempts"]),
                deadline_seconds=float(reducer_config["max_seconds"]),
                checkpoint_path=checkpoint_path,
            ).reduce(bundle)
        finally:
            raw_adapter.close()


def _rule_witness(candidate: CandidateBundle) -> RuleWitness:
    graph_a = analyze_query(candidate.pair.query_a)
    graph_b = analyze_query(candidate.pair.query_b)
    if not graph_a.valid or not graph_b.valid or graph_a.output_schema != graph_b.output_schema:
        raise _ReducerNotApplicable("dependency_graph_unavailable")
    source_certificate = candidate.pair.safety_certificate
    ast_path = str(getattr(source_certificate, "ast_path", ""))
    try:
        clause_index = _clause_index(ast_path)
    except (TypeError, ValueError) as exc:
        raise _ReducerNotApplicable("certificate_clause_unavailable") from exc
    if clause_index >= len(graph_a.clauses) or clause_index >= len(graph_b.clauses):
        raise _ReducerNotApplicable("certificate_clause_outside_dependency_graph")
    rule = RuleRegistry.default().get(candidate.certificate.rule_id)
    return RuleWitness.create(
        rule_id=rule.rule_id,
        proof_version=rule.proof_id,
        relation=rule.proof_theorem,
        required_fragments_a=(graph_a.clauses[clause_index].text,),
        required_fragments_b=(graph_b.clauses[clause_index].text,),
        required_variables=tuple(getattr(source_certificate, "variables_in_scope", ())),
        output_schema=graph_a.output_schema,
    )


def _clause_index(ast_path: str) -> int:
    if not ast_path.startswith("clauses[") or "]" not in ast_path:
        raise ValueError("candidate safety certificate has no clause path")
    return int(ast_path.split("[", 1)[1].split("]", 1)[0])


def _validation_environment(
    candidate: CandidateBundle,
    task: ShardTask,
    environment: Mapping[str, str],
    *,
    driver_version: str,
) -> dict[str, object]:
    provenance = verified_execution_provenance(environment, task.target)
    if provenance is None:
        raise ValueError("validation cannot verify the actual DBMS execution artifact")
    return {
        "dbms_version": candidate.dbms_version,
        "driver_version": driver_version,
        "execution_mode": provenance.mode,
        "execution_artifact_digest": provenance.digest,
        "cpu_architecture": platform.machine(),
        "config_hash": candidate.content_hashes["resolved_config"],
    }


def _reduction_summary(
    report: object,
    *,
    original_candidate_hash: str,
    original_validation_ref: ArtifactReference | Mapping[str, object] | None = None,
    reduced_validation_ref: ArtifactReference | Mapping[str, object] | None = None,
) -> dict[str, object]:
    summary = {
        "accepted_steps": [asdict(step) for step in getattr(report, "accepted_steps", ())],
        "candidate_hash": original_candidate_hash,
        "bundle_hash": getattr(report, "bundle_hash", ""),
        "reduced_candidate_hash": getattr(report, "reduced_candidate_hash", ""),
        "reduced_query_a": getattr(report, "reduced_pair").query_a,
        "reduced_query_b": getattr(report, "reduced_pair").query_b,
        "rejected_steps": [asdict(step) for step in getattr(report, "rejected_steps", ())],
        "status": getattr(report, "status").value,
        "stop_reason": getattr(report, "stop_reason", ""),
    }
    if original_validation_ref is not None or reduced_validation_ref is not None:
        if original_validation_ref is None or reduced_validation_ref is None:
            raise ValueError("reduction summary requires both validation references")
        summary.update(
            original_validation_hash=getattr(report, "original_validation_hash", ""),
            reduced_validation_hash=getattr(report, "reduced_validation_hash", ""),
            original_validation_ref=_reference_mapping(original_validation_ref),
            reduced_validation_ref=_reference_mapping(reduced_validation_ref),
        )
    return summary


def _reference_mapping(
    reference: ArtifactReference | Mapping[str, object],
) -> dict[str, object]:
    return asdict(reference) if isinstance(reference, ArtifactReference) else dict(reference)


def _candidate_record(
    candidate: CandidateBundle,
    evidence: object,
    *,
    graph_snapshot_ref: Mapping[str, object],
) -> dict[str, object]:
    record = ArtifactRecord(
        level="candidate",
        payload={
            "candidate_hash": candidate.candidate_hash,
            "content_hashes": dict(candidate.content_hashes),
            "pair_hash": hashlib.sha256(
                (candidate.pair.query_a + "\0" + candidate.pair.query_b).encode("utf-8")
            ).hexdigest(),
            "query_a": candidate.pair.query_a,
            "query_b": candidate.pair.query_b,
            "parameters": dict(candidate.pair.parameters),
            "rule_id": candidate.certificate.rule_id,
            "safety_certificate": asdict(candidate.certificate),
            "graph_snapshot_hash": candidate.graph_snapshot.content_hash,
            "graph_snapshot_ref": dict(graph_snapshot_ref),
            "dbms_fingerprint": candidate.pair.dbms_fingerprint,
            "discovery_median_ratio": candidate.discovery_ratio,
            "discovery_median_delta_ms": getattr(evidence, "median_delta_ms", None),
            "discovery_slow_query": candidate.discovery_slow_query,
            "discovery_paired_timing": tuple(
                asdict(sample) for sample in getattr(evidence, "paired_samples", ())
            ),
        },
    )
    return record.to_dict()


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _required_regression_manifest(spec: object) -> Path:
    regression = getattr(spec, "regression", None)
    manifest = regression.get("manifest") if isinstance(regression, Mapping) else None
    if not isinstance(manifest, Path):
        raise ValueError("formal issue promotion requires the strict regression manifest")
    return manifest


def _has_order_by(query: str) -> bool:
    return "ORDER BY" in query.upper()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["ProductionCandidatePipeline"]
