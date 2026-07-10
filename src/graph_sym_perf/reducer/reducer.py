"""Evidence-bound paired AST reducer with independent final validation."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.reducer.ddmin import (
    DDMinAttempt,
    DDMinCandidate,
    DDMinEvaluation,
    HierarchicalDDMin,
    reduction_cache_key,
)
from graph_sym_perf.reducer.dependencies import analyze_query
from graph_sym_perf.reducer.models import (
    MinimalityProof,
    MinimalityUniverse,
    ReductionBundle,
    ReductionReport,
    ReductionStatus,
    ReductionStep,
    reduction_pair_hash,
)
from graph_sym_perf.reducer.proposals import hierarchical_paired_proposals, paired_reduction_proposals
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.runtime.oracle import evaluate_runtime


@dataclass(frozen=True)
class CandidateGateDecision:
    preserved: bool
    reason: str
    evidence_hash: str
    gate_trace: tuple[str, ...]
    runtime_evidence: RuntimeEvidence | None = None
    conclusive_rejection: bool = False


def evaluate_reduction_candidate(bundle: ReductionBundle, pair: EquivalentQueryPair) -> CandidateGateDecision:
    """Run reducer gates in the only allowed order; later gates never run after failure."""

    from graph_sym_perf.validation.models import EquivalenceEvidence

    trace: list[str] = []
    graph_a = analyze_query(pair.query_a)
    graph_b = analyze_query(pair.query_b)
    if (
        not graph_a.valid
        or not graph_b.valid
        or graph_a.aggregate_sensitive
        or graph_b.aggregate_sensitive
        or graph_a.output_schema != bundle.rule_witness.output_schema
        or graph_b.output_schema != bundle.rule_witness.output_schema
        or pair.parameters != bundle.pair.parameters
        or pair.graph_snapshot_hash != bundle.pair.graph_snapshot_hash
        or pair.dbms_fingerprint != bundle.pair.dbms_fingerprint
    ):
        return _gate_failure(
            "parse_type_dependency_failed",
            trace,
            (graph_a.reason_codes, graph_b.reason_codes),
            conclusive=True,
        )
    trace.append("parse_type_dependency_passed")
    if not bundle.rule_witness.verify(pair, bundle.certificate):
        return _gate_failure("rule_witness_not_preserved", trace, bundle.rule_witness.content_hash, conclusive=True)
    trace.append("rule_witness_passed")
    try:
        equivalence = bundle.equivalence_gate.evaluate(pair)
    except Exception as exc:
        return _gate_failure(f"equivalence_error:{type(exc).__name__}", trace, str(exc))
    if not isinstance(equivalence, EquivalenceEvidence):
        return _gate_failure("full_typed_equivalence_failed", trace, equivalence)
    if not equivalence.complete_result:
        return _gate_failure("full_typed_equivalence_incomplete", trace, equivalence)
    if not equivalence.equivalent:
        return _gate_failure("full_typed_equivalence_failed", trace, equivalence, conclusive=True)
    trace.append("full_typed_equivalence_passed")
    try:
        evidence = bundle.runtime_screen.evaluate(pair)
    except Exception as exc:
        return _gate_failure(f"runtime_screen_error:{type(exc).__name__}", trace, str(exc))
    if not _ordinary_five_pair_protocol(evidence, pair, _original_slow_query(bundle)):
        return _gate_failure("ordinary_runtime_protocol_invalid", trace, evidence, runtime_evidence=evidence)
    trace.append("ordinary_runtime_protocol_passed")
    policy = bundle.validation_policy
    strict = evaluate_runtime(
        evidence,
        noise_floor_ms=float(getattr(policy, "noise_floor_ms")),
        min_ratio=float(getattr(policy, "min_ratio")),
        min_ci_low=float(getattr(policy, "min_ci_low")),
        timer_resolution_ms=float(getattr(policy, "timer_resolution_ms")),
    )
    if strict.verdict is not RuntimeVerdict.PASS:
        return _gate_failure(
            "runtime_symptom_not_preserved",
            trace,
            strict,
            runtime_evidence=strict,
            conclusive=True,
        )
    trace.append("runtime_symptom_preserved")
    return CandidateGateDecision(True, "symptom_preserved", _evidence_hash(strict), tuple(trace), strict)


class CandidateReducer:
    def __init__(
        self,
        *,
        max_attempts: int = 500,
        deadline_seconds: float = 1800.0,
        checkpoint_path: str | Path | None = None,
        clock=None,
    ) -> None:
        self.max_attempts = max_attempts
        self.deadline_seconds = deadline_seconds
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self.clock = clock

    def reduce(self, bundle: ReductionBundle) -> ReductionReport:
        if not isinstance(bundle, ReductionBundle):
            raise TypeError("CandidateReducer requires a ReductionBundle")
        if bundle.verify_integrity():
            raise ValueError("ReductionBundle integrity check failed")
        cache: dict[str, CandidateGateDecision] = {}
        original = bundle.pair
        policy_hash = bundle.content_hash("validation_policy")

        def key(pair: EquivalentQueryPair) -> str:
            return reduction_cache_key(pair, policy_hash=policy_hash)

        def evaluate(pair: EquivalentQueryPair) -> DDMinEvaluation:
            cache_key = key(pair)
            decision = cache.get(cache_key)
            if decision is None:
                try:
                    decision = evaluate_reduction_candidate(bundle, pair)
                except Exception as exc:
                    decision = _gate_failure(
                        f"evaluation_error:{type(exc).__name__}",
                        [],
                        {"error_type": type(exc).__name__, "message": str(exc)},
                    )
                cache[cache_key] = decision
            return DDMinEvaluation(decision.preserved, decision.reason, decision.evidence_hash)

        def proposals(pair: EquivalentQueryPair) -> tuple[DDMinCandidate[EquivalentQueryPair], ...]:
            return tuple(
                DDMinCandidate(item.canonical_id, item.candidate, item.layer)
                for item in hierarchical_paired_proposals(pair)
            )

        def checkpoint(pair: EquivalentQueryPair, attempt: DDMinAttempt) -> None:
            if self.checkpoint_path is not None:
                _atomic_checkpoint(self.checkpoint_path, bundle, pair, attempt)

        engine_kwargs = {
            "max_attempts": self.max_attempts,
            "deadline_seconds": self.deadline_seconds,
        }
        if self.clock is not None:
            engine_kwargs["clock"] = self.clock
        run = HierarchicalDDMin[EquivalentQueryPair](**engine_kwargs).run(
            original,
            proposals=proposals,
            evaluate=evaluate,
            cache_key=key,
            checkpoint=checkpoint,
        )
        accepted_steps, rejected_steps = _steps_from_attempts(run.attempts, cache)
        if not run.exhausted:
            evidence = _best_runtime_evidence(run.best, cache, key, original)
            return _report(
                ReductionStatus.REDUCTION_PARTIAL,
                bundle,
                run.best,
                accepted_steps,
                rejected_steps,
                evidence,
                attempts=run.attempts,
                stop_reason=run.stop_reason,
            )

        final_fast = cache.get(key(run.best))
        if final_fast is None:
            final_fast = evaluate_reduction_candidate(bundle, run.best)
            cache[key(run.best)] = final_fast
        if not final_fast.preserved or final_fast.runtime_evidence is None:
            conclusive = final_fast.conclusive_rejection
            return _report(
                (
                    ReductionStatus.REDUCTION_INVALIDATED
                    if conclusive
                    else ReductionStatus.REDUCTION_PARTIAL
                ),
                bundle,
                run.best,
                accepted_steps,
                rejected_steps,
                final_fast.runtime_evidence or _invalid_evidence(run.best, final_fast.reason),
                attempts=run.attempts,
                stop_reason=("final_runtime_failed" if conclusive else "final_runtime_inconclusive"),
            )

        universe = MinimalityUniverse.recompute(run.best)
        proof_results: list[tuple[str, str, str]] = []
        for deletion in universe.deletions:
            proposal = next(
                (item for item in paired_reduction_proposals(run.best) if item.canonical_id == deletion.canonical_id),
                None,
            )
            if proposal is None:
                return _report(
                    ReductionStatus.REDUCTION_PARTIAL,
                    bundle,
                    run.best,
                    accepted_steps,
                    rejected_steps,
                    _best_runtime_evidence(run.best, cache, key, original),
                    attempts=run.attempts,
                    stop_reason="minimality_universe_changed",
                )
            decision = cache.get(key(proposal.candidate))
            if decision is None or decision.preserved:
                return _report(
                    ReductionStatus.REDUCTION_PARTIAL,
                    bundle,
                    run.best,
                    accepted_steps,
                    rejected_steps,
                    _best_runtime_evidence(run.best, cache, key, original),
                    attempts=run.attempts,
                    stop_reason="minimality_not_exhausted",
                )
            if not decision.conclusive_rejection:
                return _report(
                    ReductionStatus.REDUCTION_PARTIAL,
                    bundle,
                    run.best,
                    accepted_steps,
                    rejected_steps,
                    final_fast.runtime_evidence,
                    attempts=run.attempts,
                    stop_reason="minimality_inconclusive",
                )
            proof_results.append((deletion.canonical_id, "symptom_not_preserved", decision.evidence_hash))
        proof = MinimalityProof.from_results(run.best, tuple(proof_results))
        if not proof.verify(run.best):
            return _report(
                ReductionStatus.REDUCTION_PARTIAL,
                bundle,
                run.best,
                accepted_steps,
                rejected_steps,
                _best_runtime_evidence(run.best, cache, key, original),
                attempts=run.attempts,
                stop_reason="minimality_proof_invalid",
            )

        if run.best == original or not accepted_steps:
            return _report(
                ReductionStatus.REDUCTION_PARTIAL,
                bundle,
                run.best,
                accepted_steps,
                rejected_steps,
                final_fast.runtime_evidence,
                minimality_proof=proof,
                attempts=run.attempts,
                stop_reason="no_reduction_accepted",
            )

        from graph_sym_perf.validation.models import CandidateBundle

        reduced_candidate = CandidateBundle.create(
            run.best,
            graph_snapshot=bundle.graph_snapshot,
            dbms_version=getattr(bundle.original_candidate, "dbms_version"),
            resolved_config=bundle.resolved_config,
            discovery_slow_query=_original_slow_query(bundle),
            discovery_ratio=final_fast.runtime_evidence.median_ratio,
            validation_policy=bundle.validation_policy,
            certificate=bundle.certificate,
        )
        try:
            final_validation = bundle.final_validator.validate(reduced_candidate)
        except Exception as exc:
            return _report(
                ReductionStatus.REDUCTION_PARTIAL,
                bundle,
                run.best,
                accepted_steps,
                rejected_steps,
                final_fast.runtime_evidence,
                minimality_proof=proof,
                reduced_candidate_hash=reduced_candidate.candidate_hash,
                attempts=run.attempts,
                stop_reason=f"final_validation_error:{type(exc).__name__}",
            )
        final_evidence = _final_validation_evidence(final_validation) or final_fast.runtime_evidence
        valid = _final_validation_matches(bundle, reduced_candidate, final_validation, final_evidence)
        conclusive = _final_validation_failure_is_conclusive(final_validation)
        return _report(
            (
                ReductionStatus.REDUCED_VALID
                if valid
                else ReductionStatus.REDUCTION_INVALIDATED
                if conclusive
                else ReductionStatus.REDUCTION_PARTIAL
            ),
            bundle,
            run.best,
            accepted_steps,
            rejected_steps,
            final_evidence,
            minimality_proof=proof,
            final_validation=final_validation,
            reduced_candidate_hash=reduced_candidate.candidate_hash,
            attempts=run.attempts,
            stop_reason=(
                "exhausted_and_validated"
                if valid
                else "final_validation_failed"
                if conclusive
                else "final_validation_inconclusive"
            ),
        )


def _steps_from_attempts(
    attempts: tuple[DDMinAttempt, ...],
    cache: dict[str, CandidateGateDecision],
) -> tuple[tuple[ReductionStep, ...], tuple[ReductionStep, ...]]:
    accepted: list[ReductionStep] = []
    rejected: list[ReductionStep] = []
    for attempt in attempts:
        decision = cache.get(attempt.cache_key) or CandidateGateDecision(
            False,
            attempt.outcome,
            attempt.evidence_hash,
            (),
        )
        step = ReductionStep(
            proposal=attempt.proposal_id,
            accepted=decision.preserved,
            reason=decision.reason,
            attempt_id=attempt.attempt_id,
            index=attempt.index,
            layer=attempt.level,
            candidate_hash=attempt.cache_key,
            gate_trace=decision.gate_trace,
            evidence_hash=decision.evidence_hash,
        )
        (accepted if decision.preserved else rejected).append(step)
    return tuple(accepted), tuple(rejected)


def _ordinary_five_pair_protocol(evidence: RuntimeEvidence, pair: EquivalentQueryPair, slow_query: str) -> bool:
    samples = evidence.paired_samples
    return bool(
        evidence.verdict is RuntimeVerdict.PASS
        and len(samples) == 5
        and {sum(sample.order == "AB" for sample in samples), sum(sample.order == "BA" for sample in samples)} == {2, 3}
        and all(
            sample.a_ms is not None
            and sample.b_ms is not None
            and not sample.a_timeout
            and not sample.b_timeout
            for sample in samples
        )
        and evidence.query_a_hash == hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest()
        and evidence.query_b_hash == hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest()
        and evidence.graph_snapshot_hash == pair.graph_snapshot_hash
        and evidence.dbms_fingerprint == pair.dbms_fingerprint
        and evidence.measurement_source == "ordinary_execute"
        and evidence.measurement_protocol == "ordinary-execute-5-pair-v1"
        and evidence.slow_query == slow_query
    )


def _original_slow_query(bundle: ReductionBundle) -> str:
    sessions = getattr(bundle.original_validation, "sessions", ())
    return str(sessions[0].evidence.slow_query) if sessions else ""


def _final_validation_matches(
    bundle: ReductionBundle,
    candidate: object,
    validation: object,
    evidence: RuntimeEvidence,
) -> bool:
    from graph_sym_perf.validation.evidence import issue_validation_report_is_complete
    from graph_sym_perf.validation.models import IssueValidationReport, PipelineStage

    return bool(
        isinstance(validation, IssueValidationReport)
        and validation.stage is PipelineStage.VALIDATION_PASSED
        and issue_validation_report_is_complete(validation)
        and not validation.verify_integrity()
        and validation.candidate_hash == candidate.candidate_hash
        and validation.query_a_hash == hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest()
        and validation.query_b_hash == hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest()
        and validation.policy_hash == bundle.content_hash("validation_policy")
        and validation.certificate_hash == bundle.content_hash("certificate")
        and validation.rule_id == bundle.rule_witness.rule_id
        and validation.proof_version == bundle.rule_witness.proof_version
        and evidence.verdict is RuntimeVerdict.PASS
        and evidence.query_a_hash == hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest()
        and evidence.query_b_hash == hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest()
        and evidence.slow_query == _original_slow_query(bundle)
        and evidence.graph_snapshot_hash == candidate.pair.graph_snapshot_hash
        and evidence.dbms_fingerprint == candidate.pair.dbms_fingerprint
        and evidence.measurement_source == "ordinary_execute"
        and len(evidence.paired_samples) == 40
        and sum(sample.order == "AB" for sample in evidence.paired_samples) == 20
        and sum(sample.order == "BA" for sample in evidence.paired_samples) == 20
        and evidence.measurement_protocol == "ordinary-execute-40-pair-combined-v1"
    )


def _final_validation_evidence(validation: object) -> RuntimeEvidence | None:
    from graph_sym_perf.validation.evidence import combined_runtime_evidence

    return combined_runtime_evidence(validation)


def _final_validation_failure_is_conclusive(validation: object) -> bool:
    from graph_sym_perf.validation.models import IssueValidationReport, PipelineStage

    if not isinstance(validation, IssueValidationReport) or validation.stage is not PipelineStage.REJECTED:
        return False
    if validation.equivalence.complete_result and not validation.equivalence.equivalent:
        return True
    if set(validation.equivalence.reason_codes) & {"typed_result_mismatch", "equivalence_failed"}:
        return True
    runtime_loss_reasons = {
        "ratio_below_threshold",
        "ci_lower_below_threshold",
        "absolute_delta_within_noise",
        "negative_control_noise",
        "direction_mismatch",
    }
    return bool(set(validation.reason_codes) & runtime_loss_reasons)


def _gate_failure(
    reason: str,
    trace: list[str],
    evidence: object,
    *,
    runtime_evidence: RuntimeEvidence | None = None,
    conclusive: bool = False,
) -> CandidateGateDecision:
    return CandidateGateDecision(
        False,
        reason,
        _evidence_hash(evidence),
        tuple(trace),
        runtime_evidence,
        conclusive,
    )


def _evidence_hash(evidence: object) -> str:
    try:
        payload = stable_json_dumps(evidence)
    except (TypeError, ValueError):
        payload = stable_json_dumps(
            {
                "unsupported_evidence_type": f"{type(evidence).__module__}.{type(evidence).__qualname__}",
            }
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _invalid_evidence(pair: EquivalentQueryPair, reason: str) -> RuntimeEvidence:
    return RuntimeEvidence(
        RuntimeVerdict.INVALID,
        hashlib.sha256(pair.query_a.encode("utf-8")).hexdigest(),
        hashlib.sha256(pair.query_b.encode("utf-8")).hexdigest(),
        (),
        None,
        None,
        None,
        None,
        0.0,
        None,
        (reason,),
        slow_query=None,
        graph_snapshot_hash=pair.graph_snapshot_hash,
        dbms_fingerprint=pair.dbms_fingerprint,
    )


def _best_runtime_evidence(
    pair: EquivalentQueryPair,
    cache: dict[str, CandidateGateDecision],
    key,
    original: EquivalentQueryPair,
) -> RuntimeEvidence:
    decision = cache.get(key(pair))
    if decision is not None and decision.runtime_evidence is not None:
        return decision.runtime_evidence
    return _invalid_evidence(pair, "partial_without_final_runtime")


def _report(
    status: ReductionStatus,
    bundle: ReductionBundle,
    reduced: EquivalentQueryPair,
    accepted: tuple[ReductionStep, ...],
    rejected: tuple[ReductionStep, ...],
    final_evidence: RuntimeEvidence,
    *,
    minimality_proof: MinimalityProof | None = None,
    final_validation: object | None = None,
    reduced_candidate_hash: str = "",
    attempts: tuple[DDMinAttempt, ...] = (),
    stop_reason: str,
) -> ReductionReport:
    original_graphs = (analyze_query(bundle.pair.query_a), analyze_query(bundle.pair.query_b))
    reduced_graphs = (analyze_query(reduced.query_a), analyze_query(reduced.query_b))
    return ReductionReport(
        status=status,
        original_pair=bundle.pair,
        reduced_pair=reduced,
        accepted_steps=accepted,
        rejected_steps=rejected,
        final_evidence=final_evidence,
        minimality_proof=minimality_proof,
        final_validation=final_validation,
        reduced_candidate_hash=reduced_candidate_hash,
        validation_policy_hash=bundle.content_hash("validation_policy"),
        certificate_hash=bundle.content_hash("certificate"),
        original_ast_hash=_evidence_hash(original_graphs),
        reduced_ast_hash=_evidence_hash(reduced_graphs),
        original_char_count=len(bundle.pair.query_a) + len(bundle.pair.query_b),
        reduced_char_count=len(reduced.query_a) + len(reduced.query_b),
        original_ast_node_count=sum(graph.ast_node_count for graph in original_graphs),
        reduced_ast_node_count=sum(graph.ast_node_count for graph in reduced_graphs),
        attempts=attempts,
        stop_reason=stop_reason,
        bundle_hash=bundle.bundle_hash,
        rule_witness_hash=bundle.rule_witness.content_hash,
    )


def _atomic_checkpoint(
    path: Path,
    bundle: ReductionBundle,
    pair: EquivalentQueryPair,
    attempt: DDMinAttempt,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bundle_hash": bundle.bundle_hash,
        "attempt_id": attempt.attempt_id,
        "attempt_index": attempt.index,
        "pair_hash": reduction_pair_hash(pair),
        "query_a": pair.query_a,
        "query_b": pair.query_b,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
