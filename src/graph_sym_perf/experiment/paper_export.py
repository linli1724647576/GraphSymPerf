"""Paper-facing table and figure-data export."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

import yaml

from graph_sym_perf.core import is_profile_supported_level, support_level_value


PAPER_TABLES = (
    "system_setup.csv",
    "effectiveness_by_system.csv",
    "candidate_funnel.csv",
    "transform_family_contribution.csv",
    "rule_contribution.csv",
    "ablation_comparison.csv",
    "pmc_by_family.csv",
    "pmc_by_rule.csv",
    "pmc_by_kind.csv",
    "root_cause_distribution.csv",
    "oracle_reliability.csv",
    "reported_issues.csv",
)

PAPER_FIGURES = (
    "pmc_growth_curve.csv",
    "candidate_funnel_by_system.csv",
    "rule_reward_curve.csv",
    "duplicate_reduction_curve.csv",
    "runtime_ratio_distribution.csv",
)


def export_paper_artifacts(
    run_dir: str | Path,
    *,
    ablation_runs: Iterable[str | Path] = (),
    oracle_run: str | Path | None = None,
) -> Mapping[str, Any]:
    """Export the schedule-required paper tables and figure data."""

    root = Path(run_dir)
    tables = root / "tables"
    figures = root / "figures"
    records = _load_case_records(root)
    signatures = _load_signatures(root)
    pmc_evidence = _load_pmc_evidence(root)
    rule_stats = _load_rule_stats(root)
    aggregate = _load_json(root / "aggregate_summary.json") or {}
    unique_issues = _load_json(root / "aggregate_unique_issues.json") or []
    pmc_summary = _load_json(root / "aggregate_pmc_summary.json") or _load_json(root / "pmc_summary.json") or {}
    reported = _load_reported_issues(root)

    system_setup = _system_setup_rows(root)
    effectiveness = _effectiveness_by_system_rows(records, signatures, unique_issues)
    candidate_funnel = _candidate_funnel_rows(records, signatures, unique_issues, reported)
    family_contribution = _transform_family_rows(signatures)
    rule_contribution = _rule_rows(signatures, rule_stats)
    ablation = _ablation_rows(tuple(Path(path) for path in ablation_runs))
    pmc_family = _counter_rows(pmc_summary.get("units_by_transform_family") or {}, "family", "pmc_units")
    pmc_rule = _counter_rows(pmc_summary.get("units_by_transform_rule") or {}, "rule", "pmc_units")
    pmc_kind = _pmc_kind_rows(pmc_summary)
    root_causes = _root_cause_rows(signatures)
    oracle = _oracle_rows(Path(oracle_run) if oracle_run else None)
    reported_rows = _reported_issue_rows(reported)

    table_payloads = {
        "system_setup.csv": (system_setup, ("system", "state", "dependency_ok", "environment_ok", "connectivity_ok", "required_env", "capabilities")),
        "effectiveness_by_system.csv": (
            effectiveness,
            (
                "system",
                "records",
                "equivalence_passed",
                "profile_supported_candidates",
                "latency_supported_candidates",
                "candidate_count",
                "unique_issue_count",
            ),
        ),
        "candidate_funnel.csv": (
            candidate_funnel,
            (
                "scope",
                "system",
                "records",
                "equivalence_passed",
                "profile_supported_candidates",
                "latency_supported_candidates",
                "candidate_count",
                "unique_issue_count",
                "reportable_issues",
                "duplicate_reduction_rate",
            ),
        ),
        "transform_family_contribution.csv": (
            family_contribution,
            ("transform_family", "candidate_count", "unique_issue_count", "profile_supported_candidates", "pmc_new_units"),
        ),
        "rule_contribution.csv": (
            rule_contribution,
            (
                "transform_rule",
                "transform_family",
                "attempted",
                "generated",
                "candidate_count",
                "unique_issue_count",
                "profile_supported_candidates",
                "pmc_new_units",
                "reward",
            ),
        ),
        "ablation_comparison.csv": (
            ablation,
            (
                "strategy",
                "run_dir",
                "record_count",
                "candidate_count",
                "unique_issue_count",
                "pmc_units_per_1k_cases",
                "profile_candidates_per_1k_cases",
                "unique_issues_per_1k_cases",
                "duplicate_rate",
            ),
        ),
        "pmc_by_family.csv": (pmc_family, ("family", "pmc_units")),
        "pmc_by_rule.csv": (pmc_rule, ("rule", "pmc_units")),
        "pmc_by_kind.csv": (pmc_kind, ("kind", "pmc_units", "new_pmc_units", "candidate_units")),
        "root_cause_distribution.csv": (root_causes, ("root_cause", "candidate_count")),
        "oracle_reliability.csv": (oracle, ("run_dir", "record_count", "candidate_count", "unique_issue_count", "support_levels")),
        "reported_issues.csv": (
            reported_rows,
            ("case_id", "system", "developer_status", "status", "issue_url", "duplicate_of", "support_level"),
        ),
    }
    figure_payloads = {
        "pmc_growth_curve.csv": (
            _pmc_growth_rows(pmc_evidence),
            ("event_index", "shard", "case_id", "transform_family", "transform_rule", "new_units", "total_units_seen"),
        ),
        "candidate_funnel_by_system.csv": (
            [row for row in candidate_funnel if row.get("scope") == "system"],
            (
                "scope",
                "system",
                "records",
                "equivalence_passed",
                "profile_supported_candidates",
                "latency_supported_candidates",
                "candidate_count",
                "unique_issue_count",
                "reportable_issues",
                "duplicate_reduction_rate",
            ),
        ),
        "rule_reward_curve.csv": (
            _rule_reward_rows(rule_stats),
            ("rank", "family", "rule", "reward", "candidate_count", "pmc_new_units"),
        ),
        "duplicate_reduction_curve.csv": (
            _duplicate_reduction_rows(signatures),
            ("candidate_index", "system", "case_id", "raw_candidates", "unique_duplicate_keys", "duplicate_reduction_rate"),
        ),
        "runtime_ratio_distribution.csv": (
            _runtime_ratio_rows(signatures),
            ("bucket", "count", "median", "p90", "max"),
        ),
    }
    for filename, (rows, fields) in table_payloads.items():
        _write_csv(tables / filename, rows, fields)
    for filename, (rows, fields) in figure_payloads.items():
        _write_csv(figures / filename, rows, fields)
    return {
        "run_dir": str(root),
        "tables": {name: str(tables / name) for name in PAPER_TABLES},
        "figures": {name: str(figures / name) for name in PAPER_FIGURES},
        "table_count": len(PAPER_TABLES),
        "figure_count": len(PAPER_FIGURES),
        "record_count": len(records),
        "candidate_count": int(aggregate.get("candidate_count") or len(signatures)),
    }


def _shard_dirs(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("shard_"))) if root.exists() else ()


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_case_records(root: Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for shard in _shard_dirs(root):
        records.extend(_load_jsonl(shard / "cases.jsonl"))
    if not records:
        records.extend(_load_jsonl(root / "cases.jsonl"))
    return records


def _load_signatures(root: Path) -> list[Mapping[str, Any]]:
    rows = _load_jsonl(root / "aggregate_candidate_signatures.jsonl")
    if rows:
        return rows
    signatures: list[Mapping[str, Any]] = []
    for shard in _shard_dirs(root):
        signatures.extend(_load_jsonl(shard / "candidate_signatures.jsonl"))
    return signatures


def _load_pmc_evidence(root: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for shard in _shard_dirs(root):
        for item in _load_jsonl(shard / "pmc_evidence.jsonl"):
            rows.append({"shard": shard.name, **dict(item)})
    if not rows:
        rows.extend({"shard": root.name, **dict(item)} for item in _load_jsonl(root / "pmc_evidence.jsonl"))
    return rows


def _load_rule_stats(root: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    aggregate_feedback = _load_json(root / "aggregate_feedback_summary.json") or {}
    for item in aggregate_feedback.get("top_rules") or ():
        rows.append(dict(item))
    if rows:
        return rows
    for shard in _shard_dirs(root):
        payload = _load_json(shard / "rule_stats.json") or []
        rows.extend(dict(item) for item in payload)
    return rows


def _load_reported_issues(root: Path) -> list[Mapping[str, Any]]:
    yaml_path = root / "reported_issues.yaml"
    if yaml_path.exists():
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, Mapping):
            return [dict(item) for item in payload.get("issues") or () if isinstance(item, Mapping)]
    reportable = _load_json(root / "reportable_issues.json") or {}
    return [dict(item) for item in reportable.get("issues") or () if isinstance(item, Mapping)]


def _system_setup_rows(root: Path) -> list[Mapping[str, Any]]:
    rows_by_system: dict[str, Mapping[str, Any]] = {}
    for shard in _shard_dirs(root):
        preflight = _load_json(shard / "preflight.json") or {}
        for item in preflight.get("systems") or ():
            status = item.get("status") if isinstance(item, Mapping) and "status" in item else item
            if not isinstance(status, Mapping):
                continue
            system = str(status.get("system") or item.get("system") or "")
            if not system:
                continue
            rows_by_system[system] = {
                "system": system,
                "state": status.get("state"),
                "dependency_ok": status.get("dependency_ok"),
                "environment_ok": status.get("environment_ok"),
                "connectivity_ok": status.get("connectivity_ok"),
                "required_env": ",".join(status.get("missing_env") or ()),
                "capabilities": json.dumps(item.get("capabilities") or {}, ensure_ascii=False, sort_keys=True) if isinstance(item, Mapping) else "",
            }
    manifest = _load_json(root / "aggregate_experiment_manifest.json") or {}
    for system in manifest.get("systems") or ():
        if not isinstance(system, Mapping):
            continue
        target = str(system.get("target") or "")
        if target and target not in rows_by_system:
            rows_by_system[target] = {
                "system": target,
                "state": "manifest_only",
                "dependency_ok": "",
                "environment_ok": "",
                "connectivity_ok": "",
                "required_env": ",".join(system.get("required_env") or ()),
                "capabilities": json.dumps(system.get("capabilities") or {}, ensure_ascii=False, sort_keys=True),
            }
    return [rows_by_system[key] for key in sorted(rows_by_system)]


def _effectiveness_by_system_rows(
    records: list[Mapping[str, Any]],
    signatures: list[Mapping[str, Any]],
    unique_issues: Any,
) -> list[Mapping[str, Any]]:
    systems = sorted({str(record.get("system") or "") for record in records} | {str(sig.get("system") or "") for sig in signatures})
    return [
        _effectiveness_row(system, records, signatures, _unique_issue_count(unique_issues, system=system))
        for system in systems
        if system
    ]


def _effectiveness_row(
    system: str,
    records: list[Mapping[str, Any]],
    signatures: list[Mapping[str, Any]],
    unique_issue_count: int,
) -> Mapping[str, Any]:
    system_records = [record for record in records if str(record.get("system") or "") == system]
    system_signatures = [sig for sig in signatures if str(sig.get("system") or "") == system]
    return {
        "system": system,
        "records": len(system_records),
        "equivalence_passed": sum(1 for record in system_records if _equivalence_passed(record)),
        "profile_supported_candidates": sum(1 for sig in system_signatures if _is_profile_supported(sig.get("support_level"))),
        "latency_supported_candidates": sum(1 for sig in system_signatures if _support_value(sig.get("support_level")) == "latency_supported"),
        "candidate_count": len(system_signatures),
        "unique_issue_count": unique_issue_count,
    }


def _support_value(value: Any) -> str:
    return support_level_value(value)


def _is_profile_supported(value: Any) -> bool:
    return is_profile_supported_level(value)


def _candidate_funnel_rows(
    records: list[Mapping[str, Any]],
    signatures: list[Mapping[str, Any]],
    unique_issues: Any,
    reported: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    systems = sorted({str(record.get("system") or "") for record in records} | {str(sig.get("system") or "") for sig in signatures})
    for system in systems:
        if system:
            row = dict(_effectiveness_row(system, records, signatures, _unique_issue_count(unique_issues, system=system)))
            row.update(
                {
                    "scope": "system",
                    "reportable_issues": sum(1 for item in reported if str(item.get("system") or "") == system),
                    "duplicate_reduction_rate": _duplicate_reduction_rate(row["candidate_count"], row["unique_issue_count"]),
                }
            )
            rows.append(row)
    overall = {
        "scope": "overall",
        "system": "overall",
        "records": len(records),
        "equivalence_passed": sum(1 for record in records if _equivalence_passed(record)),
        "profile_supported_candidates": sum(1 for sig in signatures if _is_profile_supported(sig.get("support_level"))),
        "latency_supported_candidates": sum(1 for sig in signatures if _support_value(sig.get("support_level")) == "latency_supported"),
        "candidate_count": len(signatures),
        "unique_issue_count": _unique_issue_count(unique_issues),
        "reportable_issues": len(reported),
    }
    overall["duplicate_reduction_rate"] = _duplicate_reduction_rate(overall["candidate_count"], overall["unique_issue_count"])
    return [overall, *rows]


def _transform_family_rows(signatures: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sig in signatures:
        groups[str(sig.get("transform_family") or "unknown")].append(sig)
    return [
        {
            "transform_family": family,
            "candidate_count": len(items),
            "unique_issue_count": len({str(item.get("unique_issue_key") or "") for item in items if item.get("unique_issue_key")}),
            "profile_supported_candidates": sum(1 for item in items if _is_profile_supported(item.get("support_level"))),
            "pmc_new_units": sum(_float((item.get("metadata") or {}).get("pmc_new_units")) for item in items),
        }
        for family, items in sorted(groups.items())
    ]


def _rule_rows(signatures: list[Mapping[str, Any]], rule_stats: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    by_rule: dict[str, dict[str, Any]] = defaultdict(dict)
    for stat in rule_stats:
        rule = str(stat.get("rule") or "unknown")
        by_rule[rule].update(dict(stat))
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sig in signatures:
        grouped[str(sig.get("transform_rule") or "unknown")].append(sig)
    for rule, items in grouped.items():
        row = by_rule.setdefault(rule, {"rule": rule})
        row.setdefault("family", str(items[0].get("transform_family") or "unknown"))
        row["candidate_count"] = max(int(row.get("candidate_count") or 0), len(items))
        row["unique_issue_count"] = len({str(item.get("unique_issue_key") or "") for item in items if item.get("unique_issue_key")})
        row["profile_supported_candidates"] = sum(1 for item in items if _is_profile_supported(item.get("support_level")))
        row["pmc_new_units"] = max(_float(row.get("pmc_new_units")), sum(_float((item.get("metadata") or {}).get("pmc_new_units")) for item in items))
    return [
        {
            "transform_rule": rule,
            "transform_family": row.get("family") or row.get("transform_family"),
            "attempted": row.get("attempted", 0),
            "generated": row.get("generated", 0),
            "candidate_count": row.get("candidate_count", 0),
            "unique_issue_count": row.get("unique_issue_count", 0),
            "profile_supported_candidates": row.get("profile_supported_candidates", 0),
            "pmc_new_units": row.get("pmc_new_units", 0),
            "reward": row.get("reward", 0),
        }
        for rule, row in sorted(by_rule.items())
    ]


def _ablation_rows(paths: tuple[Path, ...]) -> list[Mapping[str, Any]]:
    rows = []
    for path in paths:
        summary = _load_json(path / "aggregate_summary.json") or {}
        pmc = _load_json(path / "aggregate_pmc_summary.json") or _load_json(path / "pmc_summary.json") or {}
        record_count = int(summary.get("record_count") or 0)
        candidate_count = int(summary.get("candidate_count") or 0)
        unique_issue_count = int(summary.get("unique_issue_count") or 0)
        support_levels = summary.get("support_levels") or {}
        profile_candidates = sum(
            int(count or 0)
            for level, count in support_levels.items()
            if _is_profile_supported(level)
        )
        rows.append(
            {
                "strategy": path.name,
                "run_dir": str(path),
                "record_count": record_count,
                "candidate_count": candidate_count,
                "unique_issue_count": unique_issue_count,
                "pmc_units_per_1k_cases": _per_1k(int(pmc.get("total_units_seen") or 0), record_count),
                "profile_candidates_per_1k_cases": _per_1k(profile_candidates, record_count),
                "unique_issues_per_1k_cases": _per_1k(unique_issue_count, record_count),
                "duplicate_rate": _duplicate_reduction_rate(candidate_count, unique_issue_count),
            }
        )
    return rows


def _pmc_kind_rows(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    kinds = sorted(
        set((summary.get("units_by_kind") or {}).keys())
        | set((summary.get("new_units_by_kind") or {}).keys())
        | set((summary.get("candidate_units_by_kind") or {}).keys())
    )
    return [
        {
            "kind": kind,
            "pmc_units": (summary.get("units_by_kind") or {}).get(kind, 0),
            "new_pmc_units": (summary.get("new_units_by_kind") or {}).get(kind, 0),
            "candidate_units": (summary.get("candidate_units_by_kind") or {}).get(kind, 0),
        }
        for kind in kinds
    ]


def _root_cause_rows(signatures: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    counter: Counter[str] = Counter()
    for sig in signatures:
        labels = tuple(sig.get("root_cause_labels") or ())
        counter[str(labels[0] if labels else "UnknownPlanAnomaly")] += 1
    return [{"root_cause": key, "candidate_count": value} for key, value in sorted(counter.items())]


def _oracle_rows(path: Path | None) -> list[Mapping[str, Any]]:
    if path is None:
        return []
    summary = _load_json(path / "aggregate_summary.json") or {}
    return [
        {
            "run_dir": str(path),
            "record_count": summary.get("record_count", 0),
            "candidate_count": summary.get("candidate_count", 0),
            "unique_issue_count": summary.get("unique_issue_count", 0),
            "support_levels": json.dumps(summary.get("support_levels") or {}, ensure_ascii=False, sort_keys=True),
        }
    ]


def _reported_issue_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        {
            "case_id": row.get("case_id"),
            "system": row.get("system"),
            "developer_status": row.get("developer_status"),
            "status": row.get("status"),
            "issue_url": row.get("issue_url"),
            "duplicate_of": row.get("duplicate_of"),
            "support_level": row.get("support_level"),
        }
        for row in rows
    ]


def _pmc_growth_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    output: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        unit_keys = [str(unit.get("key") or "") for unit in row.get("units") or () if isinstance(unit, Mapping)]
        new_units = [key for key in unit_keys if key and key not in seen]
        seen.update(key for key in unit_keys if key)
        output.append(
            {
                "event_index": index,
                "shard": row.get("shard"),
                "case_id": row.get("case_id"),
                "transform_family": row.get("transform_family"),
                "transform_rule": row.get("transform_rule"),
                "new_units": len(new_units),
                "total_units_seen": len(seen),
            }
        )
    return output


def _rule_reward_rows(rule_stats: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    ordered = sorted(rule_stats, key=lambda row: (-_float(row.get("reward")), str(row.get("family") or ""), str(row.get("rule") or "")))
    return [
        {
            "rank": index,
            "family": row.get("family"),
            "rule": row.get("rule"),
            "reward": row.get("reward", 0),
            "candidate_count": row.get("candidate_count", 0),
            "pmc_new_units": row.get("pmc_new_units", 0),
        }
        for index, row in enumerate(ordered, start=1)
    ]


def _duplicate_reduction_rows(signatures: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    rows: list[Mapping[str, Any]] = []
    for index, sig in enumerate(signatures, start=1):
        key = str(sig.get("duplicate_key") or sig.get("unique_issue_key") or sig.get("case_id") or index)
        seen.add(key)
        rows.append(
            {
                "candidate_index": index,
                "system": sig.get("system"),
                "case_id": sig.get("case_id"),
                "raw_candidates": index,
                "unique_duplicate_keys": len(seen),
                "duplicate_reduction_rate": _duplicate_reduction_rate(index, len(seen)),
            }
        )
    return rows


def _runtime_ratio_rows(signatures: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for sig in signatures:
        ratio = _float((sig.get("metadata") or {}).get("latency_ratio"))
        if ratio <= 0:
            continue
        buckets[_ratio_bucket(ratio)].append(ratio)
    return [{"bucket": key, **_distribution(values)} for key, values in sorted(buckets.items())]


def _counter_rows(counter: Mapping[str, Any], key_name: str, value_name: str) -> list[Mapping[str, Any]]:
    return [{key_name: key, value_name: value} for key, value in sorted(counter.items())]


def _unique_issue_count(unique_issues: Any, *, system: str | None = None) -> int:
    if isinstance(unique_issues, list):
        if system is None:
            return len(unique_issues)
        return sum(1 for item in unique_issues if isinstance(item, Mapping) and str(item.get("system") or "") == system)
    return 0


def _equivalence_passed(record: Mapping[str, Any]) -> bool:
    equivalences = tuple(record.get("equivalences") or ())
    if equivalences:
        return any(bool(item.get("passed")) for item in equivalences if isinstance(item, Mapping))
    return str(record.get("state") or "") not in {"error", "skipped", "equivalence_failed"}


def _duplicate_reduction_rate(candidate_count: Any, unique_count: Any) -> float | None:
    candidate = _float(candidate_count)
    unique = _float(unique_count)
    if candidate <= 0:
        return None
    return max(0.0, 1.0 - unique / candidate)


def _per_1k(value: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return value * 1000.0 / denominator


def _ratio_bucket(value: float) -> str:
    for bound in (1.5, 2.0, 5.0, 10.0, 50.0, 100.0, 1000.0):
        if value < bound:
            return f"<{bound:g}x"
    return ">=1000x"


def _distribution(values: list[float]) -> Mapping[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "median": None, "p90": None, "max": None}
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.9) - 1))
    return {"count": len(ordered), "median": float(median(ordered)), "p90": ordered[index], "max": max(ordered)}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in items:
            writer.writerow({field: row.get(field) for field in fieldnames})
