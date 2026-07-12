from __future__ import annotations

import re
import hashlib
from statistics import median
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.validation.models import canonical_content_hash
from graph_sym_perf.runtime.statistics import bootstrap_ratio_ci, percentile
from graph_sym_perf.transforms.rule_models import RuleRegistry, SafetyTier


@dataclass(frozen=True)
class ArtifactReference:
    content_hash: str
    relative_path: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is None:
            raise ValueError("artifact reference requires a lowercase SHA-256")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or self.relative_path == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.relative_path
        ):
            raise ValueError("artifact reference path must be relative")
        if self.size_bytes < 0 or not self.media_type:
            raise ValueError("artifact size/media type is invalid")
        if self.schema_version != 1:
            raise ValueError("unsupported artifact reference schema version")


@dataclass(frozen=True)
class ArtifactRecord:
    level: object
    payload: Mapping[str, object]
    schema_version: int = 2
    measurement_protocol: str | None = None

    def __post_init__(self) -> None:
        level = getattr(self.level, "value", self.level)
        if level not in {"minimal", "candidate", "validation", "issue"}:
            raise ValueError("unknown artifact level")
        if self.schema_version != 2:
            raise ValueError("unsupported artifact schema version")
        normalized = dict(self.payload)
        if level == "minimal":
            allowed = {
                "query_hash", "pair_hash", "rule_id", "site", "equivalence",
                "screen_runtime_ms", "screen_ratio", "error_kind",
            }
            if set(normalized) - allowed:
                raise ValueError("minimal artifact contains embedded query/profile or an unknown field")
            if not ({"query_hash", "pair_hash"} & set(normalized)) or not normalized.get("rule_id"):
                raise ValueError("minimal artifact requires query identity and rule_id")
            if not normalized.get("equivalence") and not normalized.get("error_kind"):
                raise ValueError("minimal artifact requires an equivalence summary or typed error")
            protocol = self.measurement_protocol or "accepted-equivalence-v3"
            if protocol != "accepted-equivalence-v3":
                raise ValueError("minimal artifact measurement protocol mismatch")
        elif level == "candidate":
            required = {
                "candidate_hash", "query_a", "query_b", "discovery_paired_timing",
                "graph_snapshot_ref", "rule_id", "safety_certificate", "content_hashes",
            }
            allowed = required | {
                "parameters", "graph_snapshot_hash", "dbms_fingerprint",
                "discovery_median_ratio", "discovery_median_delta_ms", "discovery_slow_query",
                "pair_hash",
                "record_ref",
            }
            _require_fields(normalized, required, level)
            _reject_unknown_fields(normalized, allowed, level)
            _reject_explanation_payload(normalized, level)
            _validate_hash(normalized["candidate_hash"], "candidate_hash")
            _validate_reference(normalized["graph_snapshot_ref"], "graph_snapshot_ref")
            if "record_ref" in normalized:
                _validate_reference(normalized["record_ref"], "record_ref")
            _validate_candidate_chain(normalized)
            protocol = self.measurement_protocol or "execute-only-screen-v3"
            if protocol != "execute-only-screen-v3":
                raise ValueError("candidate artifact measurement protocol mismatch")
        elif level == "validation":
            required = {
                "candidate_hash", "candidate_ref", "validation_hash", "sessions",
                "environment", "dbms_fingerprint", "stage", "validation_protocol",
                "equivalence",
            }
            allowed = required | {"validated_ratio", "validated_delta_ms", "reason_codes", "environment_ref"}
            _require_fields(normalized, required, level)
            _reject_unknown_fields(normalized, allowed, level)
            sessions = normalized["sessions"]
            if not isinstance(sessions, list | tuple) or len(sessions) != 2:
                raise ValueError("validation artifact requires exactly two independent sessions")
            _reject_explanation_payload(normalized, level)
            _validate_hash(normalized["candidate_hash"], "candidate_hash")
            _validate_hash(normalized["dbms_fingerprint"], "dbms_fingerprint")
            _validate_reference(normalized["candidate_ref"], "candidate_ref")
            if "environment_ref" in normalized:
                _validate_reference(normalized["environment_ref"], "environment_ref")
            _validate_validation_payload(normalized)
            _validate_seal(normalized, "validation_hash")
            protocol = self.measurement_protocol or "independent-two-session-v1"
            if protocol != "independent-two-session-v1":
                raise ValueError("validation artifact measurement protocol mismatch")
        else:
            required = {
                "candidate_hash", "reduced_candidate_hash", "reduced_query_a",
                "reduced_query_b", "reduced_content_hashes", "reproducer_ref",
                "original_validation_ref", "final_validation_ref", "reduction_ref", "issue_hash",
                "stage", "reduction_status", "certificate", "duplicate_clearance",
                "validated_ratio", "dbms_version",
            }
            allowed = required | {"explanation_ref", "issue_id", "rule_id"}
            _require_fields(normalized, required, level)
            _reject_unknown_fields(normalized, allowed, level)
            _validate_hash(normalized["candidate_hash"], "candidate_hash")
            _validate_hash(normalized["reduced_candidate_hash"], "reduced_candidate_hash")
            if normalized["candidate_hash"] == normalized["reduced_candidate_hash"]:
                raise ValueError("issue artifact requires a distinct reduced candidate hash")
            _validate_reference(normalized["reproducer_ref"], "reproducer_ref")
            _validate_reference(normalized["original_validation_ref"], "original_validation_ref")
            _validate_reference(normalized["final_validation_ref"], "final_validation_ref")
            _validate_reference(normalized["reduction_ref"], "reduction_ref")
            if "explanation_ref" in normalized and normalized["explanation_ref"] is not None:
                _validate_reference(normalized["explanation_ref"], "explanation_ref")
            _validate_issue_payload(normalized)
            _validate_seal(normalized, "issue_hash")
            protocol = self.measurement_protocol or "submit-ready-v3"
            if protocol != "submit-ready-v3":
                raise ValueError("old measurement protocol cannot be promoted as an issue artifact")
        if not isinstance(protocol, str) or not protocol:
            raise ValueError("artifact measurement protocol is required")
        object.__setattr__(self, "payload", MappingProxyType(normalized))
        object.__setattr__(self, "measurement_protocol", protocol)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "level": getattr(self.level, "value", self.level),
            "measurement_protocol": self.measurement_protocol,
            **dict(self.payload),
        }

    @classmethod
    def seal_validation(cls, payload: Mapping[str, object]) -> "ArtifactRecord":
        normalized = dict(payload)
        if "validation_hash" in normalized:
            raise ValueError("validation_hash is computed by seal_validation")
        normalized["validation_hash"] = _sealed_hash(normalized)
        return cls(level="validation", payload=normalized)

    @classmethod
    def seal_issue(cls, payload: Mapping[str, object]) -> "ArtifactRecord":
        normalized = dict(payload)
        if "issue_hash" in normalized:
            raise ValueError("issue_hash is computed by seal_issue")
        normalized["issue_hash"] = _sealed_hash(normalized)
        return cls(level="issue", payload=normalized)


def _require_fields(payload: Mapping[str, object], required: set[str], level: str) -> None:
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{level} artifact missing required fields: {', '.join(missing)}")


def _reject_explanation_payload(payload: Mapping[str, object], level: str) -> None:
    forbidden = _explanation_keys(payload)
    if forbidden:
        raise ValueError(f"{level} artifact cannot contain explanation-only fields: {sorted(forbidden)}")


def _explanation_keys(value: object, *, prefix: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if "plan" in lowered or "profile" in lowered or "pmc" in lowered:
                found.add(path)
            found.update(_explanation_keys(item, prefix=path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.update(_explanation_keys(item, prefix=f"{prefix}[{index}]"))
    return found


def _reject_unknown_fields(payload: Mapping[str, object], allowed: set[str], level: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{level} artifact contains unknown fields: {', '.join(unknown)}")


def _validate_hash(value: object, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")


def _validate_candidate_chain(payload: Mapping[str, object]) -> None:
    content_hashes = payload["content_hashes"]
    required = {
        "query_a", "query_b", "parameters", "graph_snapshot", "dbms_fingerprint",
        "dbms_version", "resolved_config", "discovery_slow_query", "discovery_ratio",
        "validation_policy", "certificate",
    }
    if not isinstance(content_hashes, Mapping) or set(content_hashes) != required:
        raise ValueError("candidate content_hashes do not match the validation input contract")
    for name, value in content_hashes.items():
        _validate_hash(value, f"content_hashes.{name}")
    expected_candidate = hashlib.sha256(
        stable_json_dumps(dict(content_hashes)).encode("utf-8")
    ).hexdigest()
    if payload["candidate_hash"] != expected_candidate:
        raise ValueError("candidate_hash does not bind content_hashes")
    query_a_hash = hashlib.sha256(str(payload["query_a"]).encode("utf-8")).hexdigest()
    query_b_hash = hashlib.sha256(str(payload["query_b"]).encode("utf-8")).hexdigest()
    if content_hashes["query_a"] != query_a_hash or content_hashes["query_b"] != query_b_hash:
        raise ValueError("candidate query text does not match content_hashes")
    visible_bindings = {
        "parameters": payload.get("parameters", {}),
        "dbms_fingerprint": payload.get("dbms_fingerprint"),
        "discovery_slow_query": payload.get("discovery_slow_query"),
        "discovery_ratio": payload.get("discovery_median_ratio"),
        "certificate": payload.get("safety_certificate"),
    }
    for name, value in visible_bindings.items():
        if value is not None and content_hashes[name] != canonical_content_hash(value):
            raise ValueError(f"candidate {name} does not match content_hashes")
    certificate = payload.get("safety_certificate")
    if not isinstance(certificate, Mapping) or certificate.get("rule_id") != payload.get("rule_id"):
        raise ValueError("candidate safety certificate does not bind rule_id")
    reference = payload["graph_snapshot_ref"]
    reference_hash = reference.content_hash if isinstance(reference, ArtifactReference) else reference.get("content_hash")
    if (
        reference_hash != content_hashes["graph_snapshot"]
        or payload.get("graph_snapshot_hash") != reference_hash
    ):
        raise ValueError("candidate graph snapshot reference breaks the content hash chain")


def _validate_validation_payload(payload: Mapping[str, object]) -> None:
    if payload["stage"] not in {"validation_passed", "rejected", "inconclusive", "invalid"}:
        raise ValueError("validation artifact has an invalid pipeline stage")
    if payload["validation_protocol"] != "independent-two-session-v1":
        raise ValueError("validation artifact has an unsupported validation protocol")
    equivalence = payload["equivalence"]
    if not isinstance(equivalence, Mapping) or not {"equivalent", "complete_result", "reason_codes"} <= set(equivalence):
        raise ValueError("validation artifact equivalence evidence is incomplete")
    if payload["stage"] == "validation_passed" and (
        equivalence.get("equivalent") is not True
        or equivalence.get("complete_result") is not True
        or tuple(equivalence.get("reason_codes", ()))
        or tuple(payload.get("reason_codes", ()))
    ):
        raise ValueError("passed validation requires complete typed equivalence")
    environment = payload["environment"]
    required_environment = {
        "dbms_version", "driver_version", "execution_mode",
        "execution_artifact_digest", "cpu_architecture", "config_hash",
    }
    if not isinstance(environment, Mapping) or not required_environment <= set(environment):
        raise ValueError("validation environment evidence is incomplete")
    if any(not isinstance(environment[key], str) or not environment[key].strip() or environment[key] == "unknown" for key in required_environment):
        raise ValueError("validation environment evidence cannot be unknown")
    _validate_hash(environment["config_hash"], "environment.config_hash")
    if environment["execution_mode"] not in {"container_image", "embedded_package"}:
        raise ValueError("validation environment has an invalid execution provenance mode")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", str(environment["execution_artifact_digest"])) is None:
        raise ValueError("validation environment requires a pinned execution artifact digest")
    sessions = payload["sessions"]
    session_ids: set[str] = set()
    process_ids: set[int] = set()
    connection_ids: set[str] = set()
    session_directions: list[str] = []
    combined_samples: list[tuple[float, float]] = []
    control_deltas: list[float] = []
    for index, session in enumerate(sessions):
        if not isinstance(session, Mapping):
            raise ValueError("validation session must be an object")
        required_session = {
            "session_id", "process_id", "graph_fingerprint", "dbms_fingerprint",
            "query_a_hash", "query_b_hash", "slow_query", "measurement_source",
            "measurement_protocol", "runtime_verdict", "runtime_reason_codes",
            "paired_timing", "negative_controls", "lifecycle",
        }
        if not required_session <= set(session):
            raise ValueError("validation session evidence is incomplete")
        session_id = session["session_id"]
        process_id = session["process_id"]
        if not isinstance(session_id, str) or not session_id or not isinstance(process_id, int) or isinstance(process_id, bool):
            raise ValueError("validation session identity is invalid")
        session_ids.add(session_id)
        process_ids.add(process_id)
        _validate_hash(session["graph_fingerprint"], f"sessions[{index}].graph_fingerprint")
        _validate_hash(session["dbms_fingerprint"], f"sessions[{index}].dbms_fingerprint")
        _validate_hash(session["query_a_hash"], f"sessions[{index}].query_a_hash")
        _validate_hash(session["query_b_hash"], f"sessions[{index}].query_b_hash")
        if session["dbms_fingerprint"] != payload["dbms_fingerprint"]:
            raise ValueError("validation session DBMS fingerprint mismatch")
        if (
            session["slow_query"] not in {"a", "b"}
            or session["measurement_source"] != "ordinary_execute"
            or session["measurement_protocol"] != "ordinary-execute-20-pair-v1"
            or session["runtime_verdict"] not in {"pass", "fail", "inconclusive", "invalid"}
            or not isinstance(session["runtime_reason_codes"], list | tuple)
            or any(not isinstance(reason, str) for reason in session["runtime_reason_codes"])
        ):
            raise ValueError("validation session runtime identity is invalid")
        _validate_paired_timing(session["paired_timing"], f"sessions[{index}].paired_timing")
        paired_timing = tuple(session["paired_timing"])
        if (
            len(paired_timing) != 20
            or sum(sample["order"] == "AB" for sample in paired_timing) != 10
            or sum(sample["order"] == "BA" for sample in paired_timing) != 10
        ):
            raise ValueError("validation session requires exactly 20 balanced paired samples")
        direction_delta = float(median(sample["b_ms"] - sample["a_ms"] for sample in paired_timing))
        if direction_delta == 0:
            raise ValueError("validation session has no runtime direction")
        measured_direction = "b" if direction_delta > 0 else "a"
        if session["slow_query"] != measured_direction:
            runtime_reasons = tuple(session.get("runtime_reason_codes", ()))
            if (
                payload["stage"] == "validation_passed"
                or session.get("runtime_verdict") == "pass"
                or "direction_mismatch" not in runtime_reasons
            ):
                raise ValueError("validation session slow_query contradicts raw samples")
        session_directions.append(measured_direction)
        controls = session["negative_controls"]
        if not isinstance(controls, list | tuple) or len(controls) != 2:
            raise ValueError("validation session requires A/A and B/B negative controls")
        for control_index, control in enumerate(controls):
            if not isinstance(control, Mapping) or not isinstance(control.get("kind"), str):
                raise ValueError("validation negative control evidence is incomplete")
            _validate_paired_timing(
                control.get("paired_timing"),
                f"sessions[{index}].negative_controls[{control_index}].paired_timing",
            )
            control_samples = tuple(control["paired_timing"])
            if (
                len(control_samples) != 20
                or sum(sample["order"] == "AB" for sample in control_samples) != 10
                or sum(sample["order"] == "BA" for sample in control_samples) != 10
            ):
                raise ValueError("validation negative control requires 20 balanced paired samples")
            control_deltas.extend(abs(sample["b_ms"] - sample["a_ms"]) for sample in control_samples)
        if {control.get("kind") for control in controls} != {"A/A", "B/B"}:
            raise ValueError("validation negative controls must be exactly A/A and B/B")
        lifecycle = session["lifecycle"]
        if not isinstance(lifecycle, Mapping) or lifecycle.get("complete") is not True:
            raise ValueError("validation session lifecycle is incomplete")
        connection_id = lifecycle.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("validation session connection identity is missing")
        connection_ids.add(connection_id)
    if len(session_ids) != 2 or len(process_ids) != 2 or len(connection_ids) != 2:
        raise ValueError("validation sessions are not independent")
    if payload["stage"] == "validation_passed":
        if len(set(session_directions)) != 1:
            raise ValueError("validation sessions disagree on runtime direction")
        slow_query = session_directions[0]
        for session in sessions:
            for sample in session["paired_timing"]:
                combined_samples.append(
                    (sample["a_ms"], sample["b_ms"])
                    if slow_query == "b"
                    else (sample["b_ms"], sample["a_ms"])
                )
        if len(combined_samples) != 40 or any(fast <= 0 for fast, _slow in combined_samples):
            raise ValueError("validation combined sample set is incomplete")
        ratio = float(median(slow / fast for fast, slow in combined_samples))
        delta = float(median(slow - fast for fast, slow in combined_samples))
        ci = bootstrap_ratio_ci(tuple(combined_samples))
        control_p99 = percentile(tuple(control_deltas), 0.99) if control_deltas else 0.0
        if ratio < 3.0 or ci.low < 2.0 or delta <= max(control_p99, 0.01):
            raise ValueError("validation raw samples do not satisfy the runtime promotion gate")
        recorded_ratio = payload.get("validated_ratio")
        if not isinstance(recorded_ratio, int | float) or abs(float(recorded_ratio) - ratio) > 1e-12:
            raise ValueError("validation validated_ratio does not match raw samples")
        recorded_delta = payload.get("validated_delta_ms")
        if recorded_delta is not None and (
            not isinstance(recorded_delta, int | float)
            or isinstance(recorded_delta, bool)
            or abs(float(recorded_delta) - delta) > 1e-12
        ):
            raise ValueError("validation validated_delta_ms does not match raw samples")


def _validate_paired_timing(value: object, field: str) -> None:
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{field} must contain raw paired timing")
    for sample in value:
        if not isinstance(sample, Mapping) or sample.get("order") not in {"AB", "BA"}:
            raise ValueError(f"{field} contains an invalid sample")
        for key in ("a_ms", "b_ms"):
            timing = sample.get(key)
            if not isinstance(timing, int | float) or isinstance(timing, bool) or timing < 0:
                raise ValueError(f"{field} contains an invalid timing")
        if sample.get("a_timeout", False) is not False or sample.get("b_timeout", False) is not False:
            raise ValueError(f"{field} contains a timeout sample")


def _validate_issue_payload(payload: Mapping[str, object]) -> None:
    if payload["stage"] != "submit_ready" or payload["reduction_status"] != "reduced_valid":
        raise ValueError("issue artifact is not at the submit-ready reduced-valid gate")
    ratio = payload["validated_ratio"]
    if not isinstance(ratio, int | float) or isinstance(ratio, bool) or ratio < 3.0:
        raise ValueError("issue artifact validated_ratio is below the promotion gate")
    if not isinstance(payload["dbms_version"], str) or not payload["dbms_version"].strip():
        raise ValueError("issue artifact DBMS version is unavailable")
    certificate = payload["certificate"]
    if (
        not isinstance(certificate, Mapping)
        or set(certificate) != {"rule_id", "tier", "proof_version", "site_class"}
        or certificate.get("tier") != "core"
        or any(not isinstance(certificate.get(key), str) or not certificate.get(key) for key in certificate)
    ):
        raise ValueError("issue artifact CORE SafetyCertificate is incomplete")
    try:
        rule = RuleRegistry.default().get(str(certificate["rule_id"]))
    except KeyError as exc:
        raise ValueError("issue artifact certificate references an unknown rule") from exc
    if (
        rule.tier is not SafetyTier.CORE
        or not rule.submit_eligible
        or certificate["proof_version"] != rule.proof_id
        or certificate["site_class"] not in rule.site_classes
        or (payload.get("rule_id") is not None and payload.get("rule_id") != certificate["rule_id"])
    ):
        raise ValueError("issue artifact certificate does not match the audited CORE registry")
    clearance = payload["duplicate_clearance"]
    required_clearance = {
        "issue_id", "possible_duplicate", "cluster_members", "cluster_version_hash",
        "evidence_url", "registry_signature",
    }
    if not isinstance(clearance, Mapping) or set(clearance) != required_clearance:
        raise ValueError("issue artifact duplicate clearance is incomplete")
    if clearance.get("issue_id") != payload.get("issue_id"):
        raise ValueError("issue artifact duplicate clearance identity mismatch")
    if not isinstance(clearance.get("possible_duplicate"), bool):
        raise ValueError("issue artifact duplicate clearance type is invalid")
    members = clearance.get("cluster_members")
    if not isinstance(members, list | tuple) or clearance.get("issue_id") not in members:
        raise ValueError("issue artifact duplicate cluster is invalid")
    _validate_hash(clearance.get("cluster_version_hash"), "duplicate_clearance.cluster_version_hash")
    _validate_hash(clearance.get("registry_signature"), "duplicate_clearance.registry_signature")
    if clearance.get("possible_duplicate") and not clearance.get("evidence_url"):
        raise ValueError("possible duplicate issue requires distinctness evidence")
    hashes = payload["reduced_content_hashes"]
    if not isinstance(hashes, Mapping) or set(hashes) != {"query_a", "query_b"}:
        raise ValueError("issue reduced_content_hashes must bind both reduced queries")
    for name, value in hashes.items():
        _validate_hash(value, f"reduced_content_hashes.{name}")
    actual_a = hashlib.sha256(str(payload["reduced_query_a"]).encode("utf-8")).hexdigest()
    actual_b = hashlib.sha256(str(payload["reduced_query_b"]).encode("utf-8")).hexdigest()
    if hashes["query_a"] != actual_a or hashes["query_b"] != actual_b:
        raise ValueError("issue reduced query text does not match reduced_content_hashes")


def _sealed_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(stable_json_dumps(dict(payload)).encode("utf-8")).hexdigest()


def _validate_seal(payload: Mapping[str, object], field: str) -> None:
    _validate_hash(payload[field], field)
    body = {key: value for key, value in payload.items() if key != field}
    if payload[field] != _sealed_hash(body):
        raise ValueError(f"{field} does not bind the artifact payload")


def _validate_reference(value: object, field: str) -> None:
    if isinstance(value, ArtifactReference):
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an ArtifactReference")
    try:
        ArtifactReference(**dict(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ArtifactReference") from exc


__all__ = ["ArtifactRecord", "ArtifactReference"]
