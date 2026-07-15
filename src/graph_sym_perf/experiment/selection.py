"""Deterministic, fail-closed pilot gates and P1-to-P2 selection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping


SELECTOR_PROTOCOL_VERSION = "validated-yield-selector-v1"
_CONTENT_HASH = frozenset("0123456789abcdef")


class SelectionIntegrityError(ValueError):
    """Raised when pilot evidence is ambiguous, mixed, or content-invalid."""


@dataclass(frozen=True)
class CellSummary:
    campaign_id: str
    phase: str
    input_manifest_hash: str
    target: str
    rule_id: str
    family: str
    stream: str
    graph_scale: int
    shard_seed: int
    accepted_pairs: int
    quota: int
    applicability_count: int
    stage2_count: int
    validation_passed_count: int
    control_validation_passed_count: int
    validation_pair_yield: float
    validation_yield_ci_low: float
    best_validated_delta_ms: float
    best_validated_ratio: float
    best_ratio_ci_low: float
    best_delta_over_noise: float
    typed_mismatches: int
    certificate_complete: bool
    health_events: int
    artifact_complete: bool
    timeout_rate: float
    terminal_state: str

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "phase",
            "target",
            "rule_id",
            "family",
            "stream",
            "terminal_state",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise SelectionIntegrityError(f"{name} must be a non-empty string")
        if not _is_sha256(self.input_manifest_hash):
            raise SelectionIntegrityError("input_manifest_hash must be a lowercase sha256")
        for name in (
            "graph_scale",
            "shard_seed",
            "accepted_pairs",
            "quota",
            "applicability_count",
            "stage2_count",
            "validation_passed_count",
            "control_validation_passed_count",
            "typed_mismatches",
            "health_events",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SelectionIntegrityError(f"{name} must be a non-negative integer")
        if self.graph_scale <= 0:
            raise SelectionIntegrityError("graph_scale must be positive")
        for name in (
            "validation_pair_yield",
            "validation_yield_ci_low",
            "best_validated_delta_ms",
            "best_validated_ratio",
            "best_ratio_ci_low",
            "best_delta_over_noise",
            "timeout_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(float(value)):
                raise SelectionIntegrityError(f"{name} must be finite")
        if not 0.0 <= self.timeout_rate <= 1.0:
            raise SelectionIntegrityError("timeout_rate must be between zero and one")

    @property
    def identity(self) -> tuple[str, str, str, int]:
        return self.target, self.stream, self.rule_id, self.graph_scale


@dataclass(frozen=True)
class AffectedVersionRegressionReport:
    passed: bool
    artifact_complete: bool


@dataclass(frozen=True)
class P0GateReport:
    passed: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PilotCellDecision:
    summary: CellSummary
    eligible: bool
    reason_codes: tuple[str, ...]
    ranking_components: tuple[tuple[str, str, float | int | str], ...]
    selection_reason: str


@dataclass(frozen=True)
class PilotSelection:
    campaign_id: str
    input_p1_manifest_hash: str
    selector_protocol_version: str
    status: str
    reason_codes: tuple[str, ...]
    decisions: tuple[PilotCellDecision, ...]
    cells: tuple[CellSummary, ...]


def evaluate_p1_cell(summary: CellSummary) -> PilotCellDecision:
    """Evaluate a single pilot cell without interpreting runtime evidence."""

    reasons: list[str] = []
    if summary.stream != "discovery":
        reasons.append("non_discovery_stream")
    if summary.typed_mismatches:
        reasons.append("typed_equivalence_mismatch")
    if summary.accepted_pairs < summary.quota:
        reasons.append("underfilled")
    elif summary.accepted_pairs > summary.quota:
        reasons.append("overfilled")
    if not summary.certificate_complete:
        reasons.append("certificate_incomplete")
    if summary.health_events:
        reasons.append("health_event")
    if not summary.artifact_complete:
        reasons.append("artifact_incomplete")
    if summary.terminal_state != "completed":
        reasons.append(summary.terminal_state if summary.terminal_state in {
            "underfilled",
            "quarantined",
            "resource_exhausted",
            "infrastructure_failed",
        } else "non_completed_terminal")
    return PilotCellDecision(
        summary=summary,
        eligible=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        ranking_components=_ranking_components(summary),
        selection_reason="pending" if not reasons else "hard_gate_failed",
    )


def evaluate_p0_gate(
    *,
    core_cells: Iterable[CellSummary],
    control_cells: Iterable[CellSummary],
    regression: AffectedVersionRegressionReport,
) -> P0GateReport:
    reasons: list[str] = []
    core = tuple(core_cells)
    controls = tuple(control_cells)
    if not core:
        reasons.append("missing_core_cells")
    for cell in core:
        decision = evaluate_p1_cell(cell)
        reasons.extend(decision.reason_codes)
    if not controls:
        reasons.append("missing_control_cells")
    for cell in controls:
        if cell.stream != "control":
            reasons.append("invalid_control_stream")
        if cell.accepted_pairs != cell.quota:
            reasons.append("control_quota_mismatch")
        if cell.typed_mismatches:
            reasons.append("typed_equivalence_mismatch")
        if not cell.certificate_complete:
            reasons.append("certificate_incomplete")
        if cell.health_events:
            reasons.append("health_event")
        if not cell.artifact_complete:
            reasons.append("artifact_incomplete")
        if cell.terminal_state != "completed":
            reasons.append("control_non_completed_terminal")
        if cell.control_validation_passed_count:
            reasons.append("control_validation_pass")
    if not regression.artifact_complete:
        reasons.append("regression_artifact_incomplete")
    if not regression.passed:
        reasons.append("affected_version_regression_failed")
    normalized = tuple(dict.fromkeys(reasons))
    return P0GateReport(passed=not normalized, reason_codes=normalized)


def select_p2_cells(
    summaries: Iterable[CellSummary],
    *,
    max_cells: int = 6,
    campaign_id: str,
    input_p1_manifest_hash: str,
) -> PilotSelection:
    if not isinstance(max_cells, int) or isinstance(max_cells, bool) or not 1 <= max_cells <= 6:
        raise SelectionIntegrityError("max_cells must be an integer between one and six")
    if not campaign_id:
        raise SelectionIntegrityError("campaign_id must be non-empty")
    if not _is_sha256(input_p1_manifest_hash):
        raise SelectionIntegrityError("input P1 manifest hash must be a lowercase sha256")
    cells = tuple(summaries)
    identities: set[tuple[str, str, str, int]] = set()
    for cell in cells:
        if cell.phase != "p1":
            raise SelectionIntegrityError("selection input phase must be p1")
        if cell.campaign_id != campaign_id:
            raise SelectionIntegrityError("selection input campaign identity mismatch")
        if cell.input_manifest_hash != input_p1_manifest_hash:
            raise SelectionIntegrityError("selection input manifest identity mismatch")
        if cell.identity in identities:
            raise SelectionIntegrityError(f"duplicate cell identity: {cell.identity!r}")
        identities.add(cell.identity)

    decisions = [evaluate_p1_cell(cell) for cell in cells]
    decisions.sort(key=_decision_sort_key)
    if any(
        decision.summary.stream == "control"
        and decision.summary.control_validation_passed_count > 0
        for decision in decisions
    ):
        return _selection_result(
            campaign_id,
            input_p1_manifest_hash,
            decisions,
            (),
            status="control_contamination",
            reasons=("control_validation_pass",),
        )

    eligible = [decision for decision in decisions if decision.eligible]
    winners = [decision for decision in eligible if decision.summary.validation_passed_count > 0]
    winner_counts_by_rule: dict[str, int] = {}
    for winner in winners:
        winner_counts_by_rule[winner.summary.rule_id] = (
            winner_counts_by_rule.get(winner.summary.rule_id, 0) + 1
        )
    rule_scale_overflow = any(count > 2 for count in winner_counts_by_rule.values())
    if len(winners) > max_cells or rule_scale_overflow:
        overflow_reasons = (
            ("selection_overflow", "rule_scale_overflow")
            if rule_scale_overflow
            else ("selection_overflow",)
        )
        return _selection_result(
            campaign_id,
            input_p1_manifest_hash,
            decisions,
            (),
            status="selection_overflow",
            reasons=overflow_reasons,
        )

    selected: list[PilotCellDecision] = list(winners)
    selected_ids = {decision.summary.identity for decision in selected}
    selected_rules: dict[str, list[PilotCellDecision]] = {}
    for decision in selected:
        selected_rules.setdefault(decision.summary.rule_id, []).append(decision)
    fallback_families: set[str] = set()
    for decision in eligible:
        if len(selected) >= max_cells:
            break
        cell = decision.summary
        if cell.identity in selected_ids or cell.validation_passed_count > 0:
            continue
        if cell.family not in {"bool", "sym"} or cell.family in fallback_families:
            continue
        same_rule = selected_rules.get(cell.rule_id, [])
        if same_rule and not all(value.summary.validation_passed_count > 0 for value in (*same_rule, decision)):
            continue
        selected.append(decision)
        selected_ids.add(cell.identity)
        selected_rules.setdefault(cell.rule_id, []).append(decision)
        fallback_families.add(cell.family)

    selected_cells = tuple(decision.summary for decision in selected)
    status = "selected" if selected_cells else "no_eligible_cells"
    reasons = () if selected_cells else ("no_eligible_cells",)
    return _selection_result(
        campaign_id,
        input_p1_manifest_hash,
        decisions,
        selected_cells,
        status=status,
        reasons=reasons,
    )


def write_pilot_selection(selection: PilotSelection, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = _selection_payload(selection)
    payload = {**body, "content_hash": _canonical_hash(body)}
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_pilot_selection(
    path: str | Path,
    *,
    expected_campaign_id: str | None = None,
    expected_p1_manifest_hash: str | None = None,
) -> PilotSelection:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionIntegrityError(f"cannot read pilot selection: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SelectionIntegrityError("pilot selection must be an object")
    content_hash = payload.get("content_hash")
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    if content_hash != _canonical_hash(body):
        raise SelectionIntegrityError("pilot selection content hash mismatch")
    try:
        selection = _selection_from_payload(body)
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectionIntegrityError(f"pilot selection schema is invalid: {exc}") from exc
    if selection.selector_protocol_version != SELECTOR_PROTOCOL_VERSION:
        raise SelectionIntegrityError("pilot selection protocol version mismatch")
    if expected_campaign_id is not None and selection.campaign_id != expected_campaign_id:
        raise SelectionIntegrityError("pilot selection campaign identity mismatch")
    if (
        expected_p1_manifest_hash is not None
        and selection.input_p1_manifest_hash != expected_p1_manifest_hash
    ):
        raise SelectionIntegrityError("pilot selection P1 manifest identity mismatch")
    selected_by_identity = {cell.identity: cell for cell in selection.cells}
    if len(selected_by_identity) != len(selection.cells):
        raise SelectionIntegrityError("pilot selection contains duplicate selected cells")
    decision_by_identity = {decision.summary.identity: decision for decision in selection.decisions}
    if len(decision_by_identity) != len(selection.decisions):
        raise SelectionIntegrityError("pilot selection contains duplicate decisions")
    for identity, cell in selected_by_identity.items():
        decision = decision_by_identity.get(identity)
        if decision is None or decision.summary != cell or not decision.eligible:
            raise SelectionIntegrityError("selected cell is not bound to one eligible input decision")
    return selection


def _selection_result(
    campaign_id: str,
    manifest_hash: str,
    decisions: Iterable[PilotCellDecision],
    cells: tuple[CellSummary, ...],
    *,
    status: str,
    reasons: tuple[str, ...],
) -> PilotSelection:
    selected = {cell.identity for cell in cells}
    finalized = tuple(
        replace(
            decision,
            selection_reason=(
                "validation_winner"
                if decision.summary.identity in selected and decision.summary.validation_passed_count > 0
                else "family_fallback"
                if decision.summary.identity in selected
                else "not_selected"
                if decision.eligible
                else "hard_gate_failed"
            ),
        )
        for decision in decisions
    )
    return PilotSelection(
        campaign_id=campaign_id,
        input_p1_manifest_hash=manifest_hash,
        selector_protocol_version=SELECTOR_PROTOCOL_VERSION,
        status=status,
        reason_codes=reasons,
        decisions=finalized,
        cells=cells,
    )


def _ranking_components(summary: CellSummary) -> tuple[tuple[str, str, float | int | str], ...]:
    return (
        ("validation_pass_presence", "descending", int(summary.validation_passed_count > 0)),
        ("validation_yield_ci_low", "descending", summary.validation_yield_ci_low),
        ("best_validated_delta_ms", "descending", summary.best_validated_delta_ms),
        ("best_validated_ratio", "descending", summary.best_validated_ratio),
        ("best_ratio_ci_low", "descending", summary.best_ratio_ci_low),
        ("best_delta_over_noise", "descending", summary.best_delta_over_noise),
        ("applicability_count", "descending", summary.applicability_count),
        ("timeout_rate", "ascending", summary.timeout_rate),
        ("rule_id", "ascending", summary.rule_id),
        ("graph_scale", "ascending", summary.graph_scale),
    )


def _decision_sort_key(decision: PilotCellDecision) -> tuple[object, ...]:
    cell = decision.summary
    return (
        0 if decision.eligible else 1,
        -int(cell.validation_passed_count > 0),
        -cell.validation_yield_ci_low,
        -cell.best_validated_delta_ms,
        -cell.best_validated_ratio,
        -cell.best_ratio_ci_low,
        -cell.best_delta_over_noise,
        -cell.applicability_count,
        cell.timeout_rate,
        cell.rule_id,
        cell.graph_scale,
        cell.target,
        cell.stream,
        cell.shard_seed,
    )


def _selection_payload(selection: PilotSelection) -> dict[str, Any]:
    return {
        "campaign_id": selection.campaign_id,
        "input_p1_manifest_hash": selection.input_p1_manifest_hash,
        "selector_protocol_version": selection.selector_protocol_version,
        "status": selection.status,
        "reason_codes": list(selection.reason_codes),
        "decisions": [asdict(decision) for decision in selection.decisions],
        "selected_cells": [asdict(cell) for cell in selection.cells],
    }


def _selection_from_payload(payload: Mapping[str, Any]) -> PilotSelection:
    decisions = tuple(
        PilotCellDecision(
            summary=CellSummary(**item["summary"]),
            eligible=_strict_bool(item["eligible"]),
            reason_codes=tuple(_strict_str_list(item["reason_codes"])),
            ranking_components=tuple(
                (_strict_str(component[0]), _strict_str(component[1]), component[2])
                for component in item["ranking_components"]
            ),
            selection_reason=_strict_str(item["selection_reason"]),
        )
        for item in payload["decisions"]
    )
    cells = tuple(CellSummary(**item) for item in payload["selected_cells"])
    return PilotSelection(
        campaign_id=_strict_str(payload["campaign_id"]),
        input_p1_manifest_hash=_strict_str(payload["input_p1_manifest_hash"]),
        selector_protocol_version=_strict_str(payload["selector_protocol_version"]),
        status=_strict_str(payload["status"]),
        reason_codes=tuple(_strict_str_list(payload["reason_codes"])),
        decisions=decisions,
        cells=cells,
    )


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _CONTENT_HASH
        and value == value.lower()
    )


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value


def _strict_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _strict_str_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("expected string list")
    return value


__all__ = [
    "AffectedVersionRegressionReport",
    "CellSummary",
    "P0GateReport",
    "PilotCellDecision",
    "PilotSelection",
    "SELECTOR_PROTOCOL_VERSION",
    "SelectionIntegrityError",
    "evaluate_p0_gate",
    "evaluate_p1_cell",
    "load_pilot_selection",
    "select_p2_cells",
    "write_pilot_selection",
]
