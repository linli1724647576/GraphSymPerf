"""Checkpoint persistence for resumable experiment shards."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.core import stable_json_dumps, to_jsonable
from graph_sym_perf.coverage import CoverageUnit, PMCCoverageState
from graph_sym_perf.feedback.model import FeedbackState, RuleKey, RuleStats


@dataclass
class CheckpointState:
    completed_case_ids: set[str] = field(default_factory=set)
    failed_case_ids: set[str] = field(default_factory=set)
    skipped_case_ids: set[str] = field(default_factory=set)
    last_seed_index: int = -1
    feedback_state: FeedbackState | None = None
    pmc_state: PMCCoverageState | None = None
    deterministic_seed_info: Mapping[str, Any] = field(default_factory=dict)


def load_checkpoint(run_dir: str | Path) -> CheckpointState:
    path = Path(run_dir)
    checkpoint_path = path / "checkpoint.json"
    if checkpoint_path.exists():
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return CheckpointState(
            completed_case_ids=set(raw.get("completed_case_ids") or ()),
            failed_case_ids=set(raw.get("failed_case_ids") or ()),
            skipped_case_ids=set(raw.get("skipped_case_ids") or ()),
            last_seed_index=int(raw.get("last_seed_index", -1)),
            feedback_state=_feedback_state_from_mapping(raw.get("feedback_state")),
            pmc_state=_pmc_state_from_mapping(raw.get("pmc_state")),
            deterministic_seed_info=raw.get("deterministic_seed_info") or {},
        )
    completed, failed, skipped = _rebuild_case_sets(path)
    return CheckpointState(
        completed_case_ids=completed,
        failed_case_ids=failed,
        skipped_case_ids=skipped,
        last_seed_index=-1,
    )


def save_checkpoint(run_dir: str | Path, checkpoint: CheckpointState) -> Path:
    path = Path(run_dir) / "checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "completed_case_ids": sorted(checkpoint.completed_case_ids),
                "failed_case_ids": sorted(checkpoint.failed_case_ids),
                "skipped_case_ids": sorted(checkpoint.skipped_case_ids),
                "last_seed_index": checkpoint.last_seed_index,
                "feedback_state": _feedback_state_to_mapping(checkpoint.feedback_state),
                "pmc_state": _pmc_state_to_mapping(checkpoint.pmc_state),
                "deterministic_seed_info": dict(checkpoint.deterministic_seed_info),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def append_checkpoint_event(run_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    path = Path(run_dir) / "checkpoint_events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(stable_json_dumps(payload) + "\n")
    return path


def _rebuild_case_sets(run_dir: Path) -> tuple[set[str], set[str], set[str]]:
    path = run_dir / "cases.jsonl"
    completed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    if not path.exists():
        return completed, failed, skipped
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        case_id = str(raw.get("case_id") or "")
        state = str(raw.get("state") or "")
        if not case_id:
            continue
        if state == "error":
            failed.add(case_id)
        elif state == "skipped":
            skipped.add(case_id)
        else:
            completed.add(case_id)
    return completed, failed, skipped


def _feedback_state_to_mapping(state: FeedbackState | None) -> Mapping[str, Any] | None:
    if state is None:
        return None
    return {
        "exploration_rate": state.exploration_rate,
        "min_rules_per_case": state.min_rules_per_case,
        "max_rules_per_case": state.max_rules_per_case,
        "seed": state.seed,
        "rule_stats": [to_jsonable(item) for item in state.rule_stats.values()],
        "family_stats": {key: to_jsonable(value) for key, value in state.family_stats.items()},
    }


def _feedback_state_from_mapping(raw: Any) -> FeedbackState | None:
    if not isinstance(raw, Mapping):
        return None
    state = FeedbackState(
        exploration_rate=float(raw.get("exploration_rate", 0.15)),
        min_rules_per_case=int(raw.get("min_rules_per_case", 1)),
        max_rules_per_case=(int(raw["max_rules_per_case"]) if raw.get("max_rules_per_case") is not None else None),
        seed=int(raw.get("seed", 20260522)),
    )
    for item in raw.get("rule_stats") or ():
        if not isinstance(item, Mapping):
            continue
        stats = _rule_stats_from_mapping(item)
        state.rule_stats[RuleKey(family=stats.family, rule=stats.rule)] = stats
    for family, item in (raw.get("family_stats") or {}).items():
        if not isinstance(item, Mapping):
            continue
        state.family_stats[str(family)] = _rule_stats_from_mapping(item)
    return state


def _rule_stats_from_mapping(raw: Mapping[str, Any]) -> RuleStats:
    return RuleStats(
        family=str(raw.get("family") or ""),
        rule=str(raw.get("rule") or ""),
        attempted=int(raw.get("attempted", 0)),
        generated=int(raw.get("generated", 0)),
        skipped=int(raw.get("skipped", 0)),
        equivalence_passed=int(raw.get("equivalence_passed", 0)),
        equivalence_failed=int(raw.get("equivalence_failed", 0)),
        profile_available=int(raw.get("profile_available", 0)),
        candidate_count=int(raw.get("candidate_count", 0)),
        strong_candidate_count=int(raw.get("strong_candidate_count", 0)),
        pmc_units=int(raw.get("pmc_units", 0)),
        pmc_new_units=int(raw.get("pmc_new_units", 0)),
        timeouts=int(raw.get("timeouts", 0)),
        errors=int(raw.get("errors", 0)),
        reward=float(raw.get("reward", 0.0)),
    )


def _pmc_state_to_mapping(state: PMCCoverageState | None) -> Mapping[str, Any] | None:
    if state is None:
        return None
    return {
        "seen_unit_keys": sorted(state.seen_unit_keys),
        "unit_counts": dict(state.unit_counts),
        "unit_kind_counts": dict(state.unit_kind_counts),
        "transform_rule_counts": dict(state.transform_rule_counts),
        "transform_family_counts": dict(state.transform_family_counts),
        "novelty_weights": dict(state.novelty_weights),
        "new_unit_kind_counts": dict(state.new_unit_kind_counts),
        "candidate_unit_kind_counts": dict(state.candidate_unit_kind_counts),
        "tier_unit_kind_counts": {tier: dict(counter) for tier, counter in state.tier_unit_kind_counts.items()},
        "unit_key_counts": dict(state.unit_key_counts),
        "unit_registry": {key: to_jsonable(unit) for key, unit in state.unit_registry.items()},
    }


def _pmc_state_from_mapping(raw: Any) -> PMCCoverageState | None:
    if not isinstance(raw, Mapping):
        return None
    state = PMCCoverageState(novelty_weights=raw.get("novelty_weights") or {})
    state.seen_unit_keys = set(raw.get("seen_unit_keys") or ())
    state.unit_counts = Counter({str(key): int(value) for key, value in (raw.get("unit_counts") or {}).items()})
    state.unit_kind_counts = Counter({str(key): int(value) for key, value in (raw.get("unit_kind_counts") or {}).items()})
    state.transform_rule_counts = Counter(
        {str(key): int(value) for key, value in (raw.get("transform_rule_counts") or {}).items()}
    )
    state.transform_family_counts = Counter(
        {str(key): int(value) for key, value in (raw.get("transform_family_counts") or {}).items()}
    )
    state.new_unit_kind_counts = Counter(
        {str(key): int(value) for key, value in (raw.get("new_unit_kind_counts") or {}).items()}
    )
    state.candidate_unit_kind_counts = Counter(
        {str(key): int(value) for key, value in (raw.get("candidate_unit_kind_counts") or {}).items()}
    )
    state.tier_unit_kind_counts = {
        str(tier): Counter({str(key): int(value) for key, value in counter.items()})
        for tier, counter in (raw.get("tier_unit_kind_counts") or {}).items()
        if isinstance(counter, Mapping)
    }
    state.unit_key_counts = Counter({str(key): int(value) for key, value in (raw.get("unit_key_counts") or {}).items()})
    state.unit_registry = {
        str(key): _coverage_unit_from_mapping(unit)
        for key, unit in (raw.get("unit_registry") or {}).items()
        if isinstance(unit, Mapping)
    }
    return state


def _coverage_unit_from_mapping(raw: Mapping[str, Any]) -> CoverageUnit:
    return CoverageUnit(
        kind=str(raw.get("kind") or ""),
        key=str(raw.get("key") or ""),
        label=str(raw.get("label") or ""),
        weight=float(raw.get("weight", 1.0)),
        metadata=raw.get("metadata") or {},
    )
