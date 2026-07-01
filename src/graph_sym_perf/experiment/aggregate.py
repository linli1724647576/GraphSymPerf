"""Aggregate shard-level artifacts into experiment-level summaries."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from graph_sym_perf.cluster.signature import CandidateSignature
from graph_sym_perf.core import MetricDelta, stable_json_dumps
from graph_sym_perf.coverage import PMCCoverageState, PMCEvidence, PMCSummary
from graph_sym_perf.coverage.report import write_pmc_artifacts
from graph_sym_perf.experiment.tables import write_table


@dataclass(frozen=True)
class LoadedSignature:
    signature: CandidateSignature
    shard_run_id: str
    shard_dir: Path


def aggregate_run_root(run_root: str | Path) -> Mapping[str, Any]:
    root = Path(run_root)
    shard_dirs = tuple(sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("shard_")))
    if not shard_dirs:
        raise FileNotFoundError(f"no shard directories found under {root}")

    records = _load_all_case_records(shard_dirs)
    signatures = _load_all_signatures(shard_dirs)
    deduped_signatures = _dedupe_signatures(signatures)
    pmc_summary = _aggregate_pmc(shard_dirs, root)
    feedback_summary = _aggregate_feedback(shard_dirs)
    unique_issues = _group_signature_payloads(deduped_signatures.values(), "unique_issue_key")
    issue_families = _group_signature_payloads(deduped_signatures.values(), "issue_family_key")
    cluster_summary = {
        "run_root": str(root),
        "candidate_count": len(deduped_signatures),
        "unique_issue_count": len(unique_issues),
        "issue_family_count": len(issue_families),
        "shard_count": len(shard_dirs),
    }
    summary = {
        "run_root": str(root),
        "shard_count": len(shard_dirs),
        "record_count": len(records),
        "candidate_count": len(deduped_signatures),
        "unique_issue_count": len(unique_issues),
        "issue_family_count": len(issue_families),
        "systems": _system_summary(records),
        "states": dict(sorted(Counter(str(record.get("state") or "") for record in records).items())),
        "support_levels": dict(sorted(Counter(item.signature.support_level for item in deduped_signatures.values()).items())),
        "ratio_distributions": _ratio_distributions(deduped_signatures.values()),
        "artifacts": {
            "aggregate_pmc_summary": "aggregate_pmc_summary.json",
            "aggregate_feedback_summary": "aggregate_feedback_summary.json",
            "aggregate_clustered_summary": "aggregate_clustered_summary.json",
            "aggregate_candidate_signatures": "aggregate_candidate_signatures.jsonl",
            "aggregate_unique_issues": "aggregate_unique_issues.json",
            "aggregate_issue_families": "aggregate_issue_families.json",
        },
    }

    _write_json(root / "aggregate_summary.json", summary)
    (root / "aggregate_summary.md").write_text(_render_aggregate_markdown(summary), encoding="utf-8")
    _write_json(root / "aggregate_pmc_summary.json", pmc_summary)
    _write_json(root / "aggregate_feedback_summary.json", feedback_summary)
    _write_json(root / "aggregate_clustered_summary.json", cluster_summary)
    _write_json(root / "aggregate_unique_issues.json", list(unique_issues))
    _write_json(root / "aggregate_issue_families.json", list(issue_families))
    _write_signatures(root / "aggregate_candidate_signatures.jsonl", deduped_signatures.values())
    _write_tables(root, summary, unique_issues, issue_families, deduped_signatures.values())
    _copy_first_manifest(shard_dirs, root)
    return summary


def _aggregate_pmc(shard_dirs: tuple[Path, ...], root: Path) -> Mapping[str, Any]:
    state = PMCCoverageState()
    evidences: list[PMCEvidence] = []
    for shard_dir in shard_dirs:
        path = shard_dir / "pmc_evidence.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            evidence = _pmc_from_mapping(json.loads(line))
            evidences.append(state.observe(evidence))
    summary = state.snapshot(root.name)
    write_pmc_artifacts(root, summary, evidences)
    return _summary_to_mapping(summary)


def _aggregate_feedback(shard_dirs: tuple[Path, ...]) -> Mapping[str, Any]:
    combined: dict[tuple[str, str], Counter[str]] = {}
    top_rows: list[Mapping[str, Any]] = []
    for shard_dir in shard_dirs:
        path = shard_dir / "rule_stats.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw or ():
            key = (str(row.get("family") or ""), str(row.get("rule") or ""))
            bucket = combined.setdefault(key, Counter())
            for field in (
                "attempted",
                "generated",
                "skipped",
                "equivalence_passed",
                "equivalence_failed",
                "profile_available",
                "candidate_count",
                "strong_candidate_count",
                "pmc_units",
                "pmc_new_units",
                "timeouts",
                "errors",
            ):
                bucket[field] += int(row.get(field, 0))
    for (family, rule), counter in combined.items():
        top_rows.append({"family": family, "rule": rule, **dict(counter)})
    top_rows.sort(
        key=lambda row: (
            -int(row.get("candidate_count", 0)),
            -int(row.get("pmc_new_units", 0)),
            row.get("family", ""),
            row.get("rule", ""),
        )
    )
    return {"top_rules": top_rows[:20], "rule_count": len(top_rows)}


def _group_signature_payloads(items: Iterable[LoadedSignature], field: str) -> tuple[Mapping[str, Any], ...]:
    grouped: dict[str, list[LoadedSignature]] = defaultdict(list)
    for item in items:
        grouped[str(getattr(item.signature, field))].append(item)
    payloads: list[Mapping[str, Any]] = []
    for key, group in grouped.items():
        representative = sorted(group, key=_triage_signature_sort_key)[0]
        sig = representative.signature
        payloads.append(
            {
                "cluster_key": key,
                "size": len(group),
                "representative_case_id": sig.case_id,
                "system": sig.system,
                "support_level": sig.support_level,
                "claim_level": sig.claim_level,
                "transform_family": sig.transform_family,
                "transform_rule": sig.transform_rule,
                "root_cause_labels": sig.root_cause_labels,
                "candidate_types": sig.candidate_types,
                "latency_ratio": sig.metadata.get("latency_ratio"),
                "latency_delta_ms": sig.metadata.get("latency_delta_ms"),
                "db_hits_ratio": sig.metadata.get("db_hits_ratio"),
                "rows_ratio": sig.metadata.get("rows_ratio"),
                "pmc_new_units": sig.metadata.get("pmc_new_units", 0),
                "query_length": sig.metadata.get("query_length"),
                "safety_tier": sig.safety_tier,
                "shard_run_id": representative.shard_run_id,
                "case_dir": str(representative.shard_dir / sig.system / sig.case_id),
                "artifact_paths": sig.metadata.get("artifact_paths") or {},
                "case_ids": [item.signature.case_id for item in sorted(group, key=lambda row: row.signature.case_id)],
            }
        )
    payloads.sort(key=_triage_payload_sort_key)
    return tuple(payloads)


def _load_all_case_records(shard_dirs: tuple[Path, ...]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for shard_dir in shard_dirs:
        path = shard_dir / "cases.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _load_all_signatures(shard_dirs: tuple[Path, ...]) -> list[LoadedSignature]:
    items: list[LoadedSignature] = []
    for shard_dir in shard_dirs:
        path = shard_dir / "candidate_signatures.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            signature = _signature_from_mapping(json.loads(line))
            items.append(LoadedSignature(signature=signature, shard_run_id=shard_dir.name, shard_dir=shard_dir))
    return items


def _dedupe_signatures(signatures: Iterable[LoadedSignature]) -> dict[str, LoadedSignature]:
    deduped: dict[str, LoadedSignature] = {}
    for item in signatures:
        existing = deduped.get(item.signature.duplicate_key)
        if existing is None or _triage_signature_sort_key(item) < _triage_signature_sort_key(existing):
            deduped[item.signature.duplicate_key] = item
    return deduped


def _system_summary(records: Iterable[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        system = str(record.get("system") or "unknown")
        counts[system]["records"] += 1
        if ((record.get("oracle") or {}).get("is_candidate")):
            counts[system]["strict_candidates"] += 1
        support = str((record.get("confirmation") or {}).get("support_level") or "none")
        counts[system][f"support::{support}"] += 1
    return {system: dict(counter) for system, counter in sorted(counts.items())}


def _ratio_distributions(items: Iterable[LoadedSignature]) -> Mapping[str, Any]:
    metrics = {
        "latency_ratio": defaultdict(list),
        "latency_delta_ms": defaultdict(list),
        "db_hits_ratio": defaultdict(list),
        "rows_ratio": defaultdict(list),
        "pmc_new_units": defaultdict(list),
    }
    for item in items:
        system = item.signature.system
        for metric in metrics:
            value = item.signature.metadata.get(metric)
            if value is None:
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            metrics[metric]["overall"].append(parsed)
            metrics[metric][system].append(parsed)
    return {
        metric: {bucket: _distribution(values) for bucket, values in sorted(groups.items())}
        for metric, groups in metrics.items()
    }


def _distribution(values: list[float]) -> Mapping[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "median": None, "p90": None, "max": None}
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.9) - 1))
    return {
        "count": len(ordered),
        "median": float(median(ordered)),
        "p90": float(ordered[index]),
        "max": float(max(ordered)),
    }


def _write_tables(
    root: Path,
    summary: Mapping[str, Any],
    unique_issues: Iterable[Mapping[str, Any]],
    issue_families: Iterable[Mapping[str, Any]],
    signatures: Iterable[LoadedSignature],
) -> None:
    write_table(root / "tables" / "overall_summary.csv", [summary])
    write_table(
        root / "tables" / "system_summary.csv",
        [{"system": key, **value} for key, value in (summary.get("systems") or {}).items()],
    )
    write_table(root / "tables" / "unique_issues.csv", unique_issues)
    write_table(root / "tables" / "issue_families.csv", issue_families)
    write_table(
        root / "tables" / "candidate_signatures.csv",
        [
            {
                "case_id": item.signature.case_id,
                "system": item.signature.system,
                "transform_rule": item.signature.transform_rule,
                "support_level": item.signature.support_level,
                "latency_ratio": item.signature.metadata.get("latency_ratio"),
                "db_hits_ratio": item.signature.metadata.get("db_hits_ratio"),
                "rows_ratio": item.signature.metadata.get("rows_ratio"),
                "pmc_new_units": item.signature.metadata.get("pmc_new_units"),
                "shard_run_id": item.shard_run_id,
            }
            for item in signatures
        ],
    )


def _copy_first_manifest(shard_dirs: tuple[Path, ...], root: Path) -> None:
    for shard_dir in shard_dirs:
        source = shard_dir / "experiment_manifest.json"
        if source.exists():
            (root / "aggregate_experiment_manifest.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return


def _summary_to_mapping(summary: PMCSummary) -> Mapping[str, Any]:
    return {
        "run_id": summary.run_id,
        "total_units_seen": summary.total_units_seen,
        "total_new_units": summary.total_new_units,
        "units_by_kind": dict(summary.units_by_kind),
        "new_units_by_kind": dict(summary.new_units_by_kind),
        "units_by_transform_family": dict(summary.units_by_transform_family),
        "units_by_transform_rule": dict(summary.units_by_transform_rule),
        "candidate_units_by_kind": dict(summary.candidate_units_by_kind),
        "top_units": list(summary.top_units),
        "metadata": dict(summary.metadata),
    }


def _pmc_from_mapping(raw: Mapping[str, Any]) -> PMCEvidence:
    from graph_sym_perf.coverage.model import CoverageUnit

    return PMCEvidence(
        case_id=str(raw.get("case_id") or ""),
        transform_family=str(raw.get("transform_family") or ""),
        transform_rule=str(raw.get("transform_rule") or ""),
        safety_tier=str(raw.get("safety_tier") or "strict"),
        fast_variant_id=raw.get("fast_variant_id"),
        slow_variant_id=raw.get("slow_variant_id"),
        structural_hash_changed=bool(raw.get("structural_hash_changed")),
        units=tuple(
            CoverageUnit(
                kind=str(item.get("kind") or ""),
                key=str(item.get("key") or ""),
                label=str(item.get("label") or ""),
                weight=float(item.get("weight", 1.0)),
                metadata=item.get("metadata") or {},
            )
            for item in raw.get("units") or ()
        ),
        new_units=tuple(),
        unit_count=int(raw.get("unit_count") or 0),
        new_unit_count=int(raw.get("new_unit_count") or 0),
        novelty_score=float(raw.get("novelty_score") or 0.0),
        metadata=raw.get("metadata") or {},
    )


def _signature_from_mapping(raw: Mapping[str, Any]) -> CandidateSignature:
    return CandidateSignature(
        case_id=str(raw.get("case_id") or ""),
        system=str(raw.get("system") or ""),
        transform_family=str(raw.get("transform_family") or ""),
        transform_rule=str(raw.get("transform_rule") or ""),
        safety_tier=str(raw.get("safety_tier") or "strict"),
        candidate_types=tuple(str(item) for item in raw.get("candidate_types") or ()),
        root_cause_labels=tuple(str(item) for item in raw.get("root_cause_labels") or ()),
        support_level=str(raw.get("support_level") or "none"),
        claim_level=str(raw.get("claim_level") or "candidate"),
        plan_signature=str(raw.get("plan_signature") or ""),
        metric_signature=str(raw.get("metric_signature") or ""),
        pmc_signature=str(raw.get("pmc_signature") or ""),
        issue_family_key=str(raw.get("issue_family_key") or ""),
        unique_issue_key=str(raw.get("unique_issue_key") or ""),
        duplicate_key=str(raw.get("duplicate_key") or ""),
        metadata=raw.get("metadata") or {},
    )


def _triage_signature_sort_key(item: LoadedSignature) -> tuple[int, int, int, float, float, float, float, int, str]:
    sig = item.signature
    return (
        -_developer_rank(str(sig.metadata.get("developer_status") or sig.claim_level)),
        -_support_rank(sig.support_level),
        -_root_cause_rank(sig.root_cause_labels),
        -float(sig.metadata.get("db_hits_ratio") or 0.0),
        -float(sig.metadata.get("rows_ratio") or 0.0),
        -float(sig.metadata.get("latency_ratio") or 0.0),
        -float(sig.metadata.get("pmc_new_units") or 0.0),
        int(sig.metadata.get("query_length") or 10**9),
        sig.case_id,
    )


def _triage_payload_sort_key(item: Mapping[str, Any]) -> tuple[int, int, int, float, float, float, float, int, str]:
    return (
        -_developer_rank(str(item.get("claim_level") or "")),
        -_support_rank(str(item.get("support_level") or "")),
        -_root_cause_rank(tuple(item.get("root_cause_labels") or ())),
        -float(item.get("db_hits_ratio") or 0.0),
        -float(item.get("rows_ratio") or 0.0),
        -float(item.get("latency_ratio") or 0.0),
        -float(item.get("pmc_new_units") or 0.0),
        int(item.get("query_length") or 10**9),
        str(item.get("representative_case_id") or ""),
    )


def _support_rank(value: str) -> int:
    return {
        "profile_supported": 4,
        "profile_supported_without_db_hits": 3,
        "latency_supported": 2,
        "plan_only": 1,
        "none": 0,
    }.get(value, 0)


def _developer_rank(value: str) -> int:
    return {
        "fixed": 5,
        "developer_confirmed": 4,
        "confirmed": 3,
        "reported": 2,
        "strong_candidate": 1,
    }.get(value, 0)


def _root_cause_rank(labels: Iterable[str]) -> int:
    preferred = {"PredicatePushdownBlocked", "AccessPathRegression", "WorkInflation"}
    items = tuple(labels)
    if not items:
        return 0
    if items[0] in preferred:
        return 3
    if items[0] != "UnknownPlanAnomaly":
        return 2
    return 1


def _render_aggregate_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        f"# Aggregate Summary: {Path(str(summary.get('run_root'))).name}",
        "",
        f"- Shards: {summary.get('shard_count')}",
        f"- Records: {summary.get('record_count')}",
        f"- Unique candidate signatures: {summary.get('candidate_count')}",
        f"- Unique issues: {summary.get('unique_issue_count')}",
        f"- Issue families: {summary.get('issue_family_count')}",
        "",
        "## Systems",
        "",
        "| System | Records | Strict Candidates |",
        "|---|---:|---:|",
    ]
    for system, payload in (summary.get("systems") or {}).items():
        lines.append(
            f"| {system} | {payload.get('records', 0)} | {payload.get('strict_candidates', 0)} |"
        )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_signatures(path: Path, items: Iterable[LoadedSignature]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(stable_json_dumps(item.signature) + "\n")
