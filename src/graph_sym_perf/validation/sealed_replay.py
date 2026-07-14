"""Strict reconstruction of typed validation evidence from sealed JSON payloads."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from graph_sym_perf.core import to_jsonable
from graph_sym_perf.runtime.models import PairedSample, RuntimeEvidence, RuntimeVerdict
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    IssueValidationReport,
    SessionLifecycle,
    ValidationSession,
)
from graph_sym_perf.validation.session_runner import validation_schedule_seed
from graph_sym_perf.validation.validator import IssueValidator


def replay_issue_validation_report(
    candidate: CandidateBundle,
    report_payload: object,
) -> IssueValidationReport:
    """Rebuild an IssueValidationReport from its raw sessions and require an exact match."""

    if not isinstance(report_payload, dict):
        raise ValueError("validation report payload must be an object")
    equivalence = equivalence_evidence_from_payload(report_payload.get("equivalence"))
    sessions = validation_sessions_from_payload(report_payload.get("sessions"))
    expected_schedule_seeds = tuple(
        validation_schedule_seed(candidate.candidate_hash, session_number)
        for session_number in (1, 2)
    )
    if (
        tuple(session.schedule_seed for session in sessions) != expected_schedule_seeds
        or expected_schedule_seeds[0] == expected_schedule_seeds[1]
    ):
        raise ValueError("validation session schedule seed identity differs")
    recomputed = IssueValidator().validate(
        candidate,
        SealedValidationReplayRunner(equivalence, sessions),
    )
    if to_jsonable(asdict(recomputed)) != report_payload:
        raise ValueError("validation report differs from canonical raw-session replay")
    return recomputed


@dataclass(frozen=True)
class SealedValidationReplayRunner:
    equivalence: EquivalenceEvidence
    sessions: tuple[ValidationSession, ValidationSession]

    def run_session(
        self,
        candidate: CandidateBundle,
        session_number: int,
        *,
        expected_slow_query: str,
        noise_floor_ms: float,
        timer_resolution_ms: float,
    ) -> tuple[EquivalenceEvidence, ValidationSession]:
        if (
            expected_slow_query != candidate.discovery_slow_query
            or noise_floor_ms != candidate.validation_policy.noise_floor_ms
            or timer_resolution_ms != candidate.validation_policy.timer_resolution_ms
            or session_number not in {1, 2}
        ):
            raise ValueError("sealed validation replay policy differs")
        return self.equivalence, self.sessions[session_number - 1]


def runtime_evidence_from_payload(payload: object) -> RuntimeEvidence:
    if not isinstance(payload, dict) or set(payload) != set(
        RuntimeEvidence.__dataclass_fields__
    ):
        raise ValueError("RuntimeEvidence shape differs")
    verdict = payload["verdict"]
    if not isinstance(verdict, str):
        raise ValueError("RuntimeEvidence verdict must be a string enum value")
    reasons = payload["reason_codes"]
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) for reason in reasons
    ):
        raise ValueError("RuntimeEvidence reasons are malformed")
    samples = payload["paired_samples"]
    if not isinstance(samples, list) or any(
        not isinstance(sample, dict)
        or set(sample) != set(PairedSample.__dataclass_fields__)
        for sample in samples
    ):
        raise ValueError("paired samples are malformed")
    for sample in samples:
        if sample["order"] not in {"AB", "BA"}:
            raise ValueError("paired sample order is malformed")
        if type(sample["a_timeout"]) is not bool or type(sample["b_timeout"]) is not bool:
            raise ValueError("paired sample timeout flags must be booleans")
        for field in ("a_ms", "b_ms"):
            value = sample[field]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("paired sample timings must be finite non-negative numbers")
    optional_numeric_fields = {
        "median_ratio",
        "median_delta_ms",
        "ci_ratio_low",
        "ci_ratio_high",
        "relative_mad",
        "timeout_lower_bound_ratio",
        "median_a_ms",
        "median_b_ms",
        "ci_delta_low_ms",
        "ci_delta_high_ms",
    }
    for field in optional_numeric_fields | {"direction_consistency"}:
        value = payload[field]
        if value is None and field in optional_numeric_fields:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"RuntimeEvidence {field} must be finite")
    for field in ("query_a_hash", "query_b_hash"):
        if not isinstance(payload[field], str):
            raise ValueError(f"RuntimeEvidence {field} must be a string")
    for field in (
        "slow_query",
        "graph_snapshot_hash",
        "dbms_fingerprint",
        "measurement_source",
        "measurement_protocol",
        "slower_side",
    ):
        if payload[field] is not None and not isinstance(payload[field], str):
            raise ValueError(f"RuntimeEvidence {field} must be a string or null")
    return RuntimeEvidence(
        **{
            **payload,
            "verdict": RuntimeVerdict(verdict),
            "paired_samples": tuple(PairedSample(**sample) for sample in samples),
            "reason_codes": tuple(reasons),
        }
    )


def equivalence_evidence_from_payload(payload: object) -> EquivalenceEvidence:
    if not isinstance(payload, dict) or set(payload) != set(
        EquivalenceEvidence.__dataclass_fields__
    ):
        raise ValueError("EquivalenceEvidence shape differs")
    if type(payload["equivalent"]) is not bool or type(payload["complete_result"]) is not bool:
        raise ValueError("EquivalenceEvidence flags must be booleans")
    reasons = payload["reason_codes"]
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise ValueError("EquivalenceEvidence reasons are malformed")
    return EquivalenceEvidence(
        payload["equivalent"],
        payload["complete_result"],
        tuple(reasons),
    )


def validation_sessions_from_payload(
    payload: object,
) -> tuple[ValidationSession, ValidationSession]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("validation report requires exactly two sessions")
    sessions = tuple(_validation_session_from_payload(value) for value in payload)
    return sessions[0], sessions[1]


def _validation_session_from_payload(payload: object) -> ValidationSession:
    if not isinstance(payload, dict) or set(payload) != set(
        ValidationSession.__dataclass_fields__
    ):
        raise ValueError("ValidationSession shape differs")
    controls = payload["negative_controls"]
    if not isinstance(controls, list):
        raise ValueError("ValidationSession negative controls are malformed")
    lifecycle_payload = payload["lifecycle"]
    if not isinstance(lifecycle_payload, dict) or set(lifecycle_payload) != set(
        SessionLifecycle.__dataclass_fields__
    ):
        raise ValueError("ValidationSession lifecycle is malformed")
    if (
        not isinstance(payload["session_id"], str)
        or not payload["session_id"]
        or type(payload["process_id"]) is not int
        or payload["process_id"] <= 0
        or not isinstance(payload["graph_fingerprint"], str)
        or not isinstance(payload["dbms_fingerprint"], str)
        or (
            payload["health_event"] is not None
            and not isinstance(payload["health_event"], str)
        )
        or (
            payload["schedule_seed"] is not None
            and type(payload["schedule_seed"]) is not int
        )
    ):
        raise ValueError("ValidationSession scalar fields are malformed")
    lifecycle_flags = set(SessionLifecycle.__dataclass_fields__) - {"connection_id"}
    if any(type(lifecycle_payload[field]) is not bool for field in lifecycle_flags) or not isinstance(
        lifecycle_payload["connection_id"], str
    ):
        raise ValueError("ValidationSession lifecycle field types are malformed")
    return ValidationSession(
        session_id=payload["session_id"],
        process_id=payload["process_id"],
        graph_fingerprint=payload["graph_fingerprint"],
        dbms_fingerprint=payload["dbms_fingerprint"],
        evidence=runtime_evidence_from_payload(payload["evidence"]),
        negative_controls=tuple(
            runtime_evidence_from_payload(control) for control in controls
        ),
        health_event=payload["health_event"],
        lifecycle=SessionLifecycle(**lifecycle_payload),
        schedule_seed=payload["schedule_seed"],
    )


__all__ = [
    "SealedValidationReplayRunner",
    "equivalence_evidence_from_payload",
    "replay_issue_validation_report",
    "runtime_evidence_from_payload",
    "validation_sessions_from_payload",
]
