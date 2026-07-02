"""Calibration run health checks."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def run_calibration_health(run_dir: str | Path) -> Mapping[str, Any]:
    path = Path(run_dir)
    summary = _load_json(path / "summary.json")
    pmc_summary = _load_json(path / "pmc_summary.json")
    rule_stats = _load_json(path / "rule_stats.json", default=[])
    clustered = _load_json(path / "clustered_summary.json")
    cases = _load_jsonl(path / "cases.jsonl")
    pmc_evidence = _load_jsonl(path / "pmc_evidence.jsonl")
    pmc_units = _load_json(path / "pmc_units.json", default=[])
    signatures = _load_jsonl(path / "candidate_signatures.jsonl")
    issues: list[dict[str, Any]] = []

    _check_pmc(path, pmc_summary, pmc_evidence, pmc_units, cases, issues)
    _check_rule_stats(rule_stats if isinstance(rule_stats, list) else [], issues)
    _check_runtime_confirmed(summary, cases, issues)
    _check_enabled_rules(path, summary, rule_stats if isinstance(rule_stats, list) else [], cases, issues)
    _check_candidate_counts(clustered, signatures, issues)
    _check_artifact_paths(path, cases, signatures, issues)
    _check_skip_rates(summary, cases, issues)
    context_pollution_level = _check_context_pollution(summary, pmc_summary, pmc_evidence, issues)

    severities = Counter(str(issue["severity"]) for issue in issues)
    pmc_valid = _category_valid(issues, ("pmc_", "case_pmc_"))
    rule_stats_valid = _category_valid(issues, ("rule_",))
    oracle_valid = _category_valid(issues, ("runtime_", "oracle_"))
    artifact_completeness = _category_valid(
        issues,
        ("artifact_", "pmc_evidence_empty", "pmc_units_empty", "case_pmc_", "per_case_pmc_"),
    )
    overall_status = _overall_status(severities, context_pollution_level)
    payload = {
        "run_dir": str(path),
        "pmc_valid": pmc_valid,
        "rule_stats_valid": rule_stats_valid,
        "oracle_valid": oracle_valid,
        "context_pollution_level": context_pollution_level,
        "artifact_completeness": artifact_completeness,
        "overall_status": overall_status,
        "ok": overall_status != "FAIL",
        "severity_counts": dict(sorted(severities.items())),
        "checked": {
            "case_count": len(cases),
            "pmc_evidence_count": len(pmc_evidence),
            "pmc_unit_count": len(pmc_units) if isinstance(pmc_units, list) else 0,
            "rule_stat_count": len(rule_stats) if isinstance(rule_stats, list) else 0,
            "candidate_signature_count": len(signatures),
        },
        "issues": issues,
    }
    (path / "calibration_health_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (path / "calibration_health_report.md").write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def calibration_health_check(run_dir: str | Path) -> Mapping[str, Any]:
    return run_calibration_health(run_dir)


def _check_pmc(
    run_dir: Path,
    pmc_summary: Mapping[str, Any],
    pmc_evidence: list[Mapping[str, Any]],
    pmc_units: Any,
    cases: list[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    total_all = _int(
        pmc_summary.get("total_units_seen_all")
        or pmc_summary.get("pmc_total_units_seen_all")
        or pmc_summary.get("total_units_seen")
        or 0
    )
    evidence_units = sum(len(item.get("units") or ()) for item in pmc_evidence)
    evidence_unit_counts = _evidence_unit_counts(pmc_evidence)
    if total_all > 0 and not pmc_evidence:
        _issue(issues, "severe", "pmc_evidence_empty", "pmc_summary reports units but pmc_evidence.jsonl is empty")
    if total_all > 0 and isinstance(pmc_units, list) and not pmc_units:
        _issue(issues, "severe", "pmc_units_empty", "pmc_summary reports units but pmc_units.json is empty")
    if total_all and evidence_units and total_all != evidence_units:
        _issue(
            issues,
            "severe",
            "pmc_total_mismatch",
            f"pmc_summary total_units_seen_all={total_all} but pmc_evidence has {evidence_units} unit observations",
        )
    if isinstance(pmc_units, list) and pmc_units and evidence_unit_counts:
        unit_count_total = sum(_int(item.get("count")) for item in pmc_units if isinstance(item, Mapping))
        if unit_count_total != evidence_units:
            _issue(
                issues,
                "severe",
                "pmc_units_mismatch",
                f"pmc_units.json has {unit_count_total} unit observations but pmc_evidence has {evidence_units}",
            )
    context_count = _context_unit_count(pmc_summary, pmc_evidence)
    core_count = _int(pmc_summary.get("total_units_seen_core") or pmc_summary.get("total_units_seen") or 0)
    if total_all and core_count + context_count != total_all:
        _issue(
            issues,
            "severe",
            "pmc_core_context_total_mismatch",
            f"core_units + context_units = {core_count + context_count}, but total_units_seen_all={total_all}",
        )
    evidence_case_ids = {str(item.get("case_id")) for item in pmc_evidence if item.get("case_id")}
    for case_id in _result_equivalent_case_ids(cases):
        if case_id not in evidence_case_ids:
            _issue(issues, "severe", "case_pmc_evidence_missing", f"{case_id}: result-equivalent case has no PMCEvidence")
    case_pmc_case_ids = _case_ids_with_case_pmc(run_dir)
    if pmc_evidence and not case_pmc_case_ids:
        _issue(issues, "severe", "per_case_pmc_missing", "no per-case pmc_evidence.json files were found")
    for case_id in sorted(evidence_case_ids - case_pmc_case_ids):
        _issue(issues, "severe", "per_case_pmc_missing", f"{case_id}: PMCEvidence has no case-level pmc evidence file")


def _check_rule_stats(rule_stats: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> None:
    for row in rule_stats:
        for issue in _rule_stats_consistency_issues(row):
            issues.append(issue)
        rule = str(row.get("rule") or "unknown")
        attempted = _int(row.get("attempted"))
        if attempted == 0 and not row.get("inactive_reason"):
            _issue(issues, "warning", "rule_zero_attempt_no_reason", f"{rule}: enabled rule has 0 attempts without inactive_reason")


def rule_stats_consistency_check(row: Mapping[str, Any]) -> bool:
    return not _rule_stats_consistency_issues(row)


def _rule_stats_consistency_issues(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rule = str(row.get("rule") or "unknown")
    attempted = _int(row.get("attempted"))
    generated = _int(row.get("generated"))
    skipped = _int(row.get("skipped"))
    outcomes = (
        _int(row.get("equivalence_passed"))
        + _int(row.get("equivalence_failed"))
        + _int(row.get("errors"))
        + _int(row.get("timeouts"))
        + skipped
    )
    if generated > attempted:
        _issue(issues, "severe", "rule_generated_gt_attempted", f"{rule}: generated={generated} > attempted={attempted}")
    if skipped > attempted:
        _issue(issues, "severe", "rule_skipped_gt_attempted", f"{rule}: skipped={skipped} > attempted={attempted}")
    if outcomes > generated:
        _issue(
            issues,
            "severe",
            "rule_outcomes_gt_generated",
            f"{rule}: passed+failed+errors+timeouts+skipped={outcomes} > generated={generated}",
        )
    return issues


def _check_runtime_confirmed(summary: Mapping[str, Any], cases: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> None:
    expected = sum(1 for case in cases if _runtime_confirmed_case(case))
    observed = _int(summary.get("runtime_confirmed"))
    if observed != expected:
        _issue(
            issues,
            "severe",
            "runtime_confirmed_mismatch",
            f"summary.runtime_confirmed={observed} but profile-gated runtime-confirmed cases={expected}",
        )
    for case in cases:
        support_level = str(((case.get("confirmation") or {}).get("support_level")) or "")
        if support_level == "latency_supported":
            _issue(
                issues,
                "severe",
                "runtime_without_profile",
                f"{case.get('case_id')}: support_level=latency_supported is not allowed by profile gating",
            )
            continue
        if _runtime_without_profile(case):
            _issue(
                issues,
                "severe",
                "runtime_without_profile",
                f"{case.get('case_id')}: runtime layer passed or latency worsened without profile support",
            )


def _check_enabled_rules(
    run_dir: Path,
    summary: Mapping[str, Any],
    rule_stats: list[Mapping[str, Any]],
    cases: list[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    enabled = set(_enabled_rules(run_dir))
    if not enabled:
        return
    actual = set(str(rule) for rule in (summary.get("transform_rules") or {}).keys())
    actual.update(
        str(row.get("rule"))
        for row in rule_stats
        if _int(row.get("attempted")) > 0 or _int(row.get("generated")) > 0 or _int(row.get("skipped")) > 0
    )
    for case in cases:
        family = case.get("family") or {}
        for variant in tuple(family.get("variants") or ()) + tuple(family.get("skipped_variants") or ()):
            if variant.get("transform_rule"):
                actual.add(str(variant["transform_rule"]))
    unexpected = sorted(rule for rule in actual if rule and rule != "None" and rule not in enabled)
    if unexpected:
        _issue(
            issues,
            "severe",
            "actual_rule_not_enabled",
            "actual transform rules are not enabled or aliased: " + ", ".join(unexpected),
        )


def _check_candidate_counts(
    clustered: Mapping[str, Any],
    signatures: list[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    if not clustered:
        return
    expected = _int(clustered.get("candidate_count"))
    actual = len(signatures)
    if expected != actual:
        _issue(
            issues,
            "severe",
            "candidate_signature_count_mismatch",
            f"clustered candidate_count={expected} but candidate_signatures.jsonl has {actual}",
        )


def _check_artifact_paths(
    run_dir: Path,
    cases: list[Mapping[str, Any]],
    signatures: list[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    missing: list[str] = []
    for case in cases:
        if not ((case.get("oracle") or {}).get("is_candidate")):
            continue
        for rel in (case.get("artifacts") or {}).values():
            if _looks_like_path(rel) and not (run_dir / str(rel)).exists():
                missing.append(str(rel))
    for signature in signatures:
        paths = ((signature.get("metadata") or {}).get("artifact_paths") or {})
        for rel in paths.values():
            if _looks_like_path(rel) and not (run_dir / str(rel)).exists():
                missing.append(str(rel))
    if missing:
        examples = ", ".join(sorted(set(missing))[:8])
        _issue(issues, "severe", "artifact_path_missing", f"referenced artifact paths are missing: {examples}")


def _check_skip_rates(summary: Mapping[str, Any], cases: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> None:
    total = _int(summary.get("generated_cases")) or len(cases)
    skipped = _int((summary.get("states") or {}).get("skipped"))
    if total and skipped / total > 0.5:
        _issue(issues, "warning", "skip_rate_high", f"skip rate is {skipped}/{total} ({skipped / total:.1%})")
    by_system: dict[str, Counter[str]] = {}
    for case in cases:
        system = str(case.get("system") or "unknown")
        bucket = by_system.setdefault(system, Counter())
        bucket["total"] += 1
        if case.get("state") == "skipped":
            bucket["skipped"] += 1
    for system, counter in sorted(by_system.items()):
        if counter["total"] and counter["skipped"] / counter["total"] > 0.8:
            _issue(
                issues,
                "warning",
                "system_skip_rate_high",
                f"{system} skip rate is {counter['skipped']}/{counter['total']} ({counter['skipped'] / counter['total']:.1%}); consider target-specific generation or common Cypher subset filtering",
            )


def _check_context_pollution(
    summary: Mapping[str, Any],
    pmc_summary: Mapping[str, Any],
    pmc_evidence: list[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> str:
    top_units = tuple(pmc_summary.get("top_units") or ())
    if top_units and str(top_units[0].get("kind")) == "transformation_context":
        _issue(issues, "warning", "pmc_top_units_context_polluted", "PMC top_units are led by transformation_context")
    context_count = _context_unit_count(pmc_summary, pmc_evidence) or _int(
        (summary.get("context_units") or {}).get("transformation_context")
    )
    total_all = (
        _int(pmc_summary.get("total_units_seen_all"))
        or _int(summary.get("pmc_total_units_seen_all"))
        or _int(pmc_summary.get("total_units_seen"))
        or _int(summary.get("pmc_total_units_seen"))
    )
    ratio = context_count / total_all if total_all else 0.0
    if ratio > 0.7:
        _issue(issues, "severe", "context_pollution_high", f"context PMC ratio is {context_count}/{total_all} ({ratio:.1%})")
        return "FAIL"
    if ratio > 0.3:
        _issue(issues, "warning", "context_pollution_warn", f"context PMC ratio is {context_count}/{total_all} ({ratio:.1%})")
        return "WARN"
    total_core = _int(summary.get("pmc_total_units_seen"))
    if context_count and total_all and total_core == total_all:
        _issue(issues, "warning", "pmc_core_total_not_separated", "summary PMC total appears to include context units")
        return "WARN"
    return "PASS"


def _runtime_confirmed_case(case: Mapping[str, Any]) -> bool:
    oracle = case.get("oracle") or {}
    if not oracle.get("is_candidate"):
        return False
    confirmation = case.get("confirmation") or {}
    if str(confirmation.get("support_level") or "") != "profile_supported":
        return False
    return _runtime_layer_passed(case)


def _runtime_without_profile(case: Mapping[str, Any]) -> bool:
    oracle = case.get("oracle") or {}
    if not isinstance(oracle, Mapping):
        return False
    if _profile_layer_passed(case):
        return False
    if _runtime_layer_passed(case):
        return True
    latency = ((oracle.get("evidence") or {}).get("latency") or {})
    return bool(isinstance(latency, Mapping) and latency.get("worsened") and oracle.get("is_candidate"))


def _profile_layer_passed(case: Mapping[str, Any]) -> bool:
    confirmation = case.get("confirmation") or {}
    if str(confirmation.get("support_level") or "") == "profile_supported":
        return True
    oracle = case.get("oracle") or {}
    evidence = (oracle.get("evidence") or {}) if isinstance(oracle, Mapping) else {}
    layer_status = (evidence.get("oracle_layer_status") or {}) if isinstance(evidence, Mapping) else {}
    return isinstance(layer_status, Mapping) and str(layer_status.get("profile") or "") == "pass"


def _runtime_layer_passed(case: Mapping[str, Any]) -> bool:
    oracle = case.get("oracle") or {}
    evidence = (oracle.get("evidence") or {}) if isinstance(oracle, Mapping) else {}
    layer_status = (evidence.get("oracle_layer_status") or {}) if isinstance(evidence, Mapping) else {}
    if isinstance(layer_status, Mapping) and str(layer_status.get("runtime") or ""):
        return str(layer_status.get("runtime")) == "pass"
    latency = (evidence.get("latency") or {}) if isinstance(evidence, Mapping) else {}
    return bool(isinstance(latency, Mapping) and latency.get("worsened") and _profile_layer_passed(case))


def _evidence_unit_counts(pmc_evidence: list[Mapping[str, Any]]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for evidence in pmc_evidence:
        for unit in evidence.get("units") or ():
            if not isinstance(unit, Mapping):
                continue
            counts[(str(unit.get("kind") or ""), str(unit.get("key") or ""))] += 1
    return counts


def _context_unit_count(pmc_summary: Mapping[str, Any], pmc_evidence: list[Mapping[str, Any]]) -> int:
    context_units = pmc_summary.get("context_units") or {}
    if isinstance(context_units, Mapping):
        count = _int(context_units.get("transformation_context"))
        if count:
            return count
    return sum(
        1
        for evidence in pmc_evidence
        for unit in evidence.get("units") or ()
        if isinstance(unit, Mapping) and str(unit.get("kind") or "") == "transformation_context"
    )


def _result_equivalent_case_ids(cases: list[Mapping[str, Any]]) -> tuple[str, ...]:
    case_ids: list[str] = []
    for case in cases:
        if any(bool(item.get("passed")) for item in case.get("equivalences") or () if isinstance(item, Mapping)):
            case_id = str(case.get("case_id") or "")
            if case_id:
                case_ids.append(case_id)
    return tuple(case_ids)


def _category_valid(issues: list[dict[str, Any]], prefixes: tuple[str, ...]) -> bool:
    for issue in issues:
        if issue.get("severity") != "severe":
            continue
        code = str(issue.get("code") or "")
        if any(code.startswith(prefix) for prefix in prefixes):
            return False
    return True


def _overall_status(severities: Counter[str], context_pollution_level: str) -> str:
    if severities.get("severe", 0) or context_pollution_level == "FAIL":
        return "FAIL"
    if severities.get("warning", 0) or context_pollution_level == "WARN":
        return "WARN"
    return "PASS"


def _enabled_rules(run_dir: Path) -> tuple[str, ...]:
    for name in ("run_config_snapshot.json", "experiment_manifest.json"):
        payload = _load_json(run_dir / name)
        transforms = payload.get("transforms") if isinstance(payload, Mapping) else None
        rules = transforms.get("enabled_rules") if isinstance(transforms, Mapping) else None
        if rules:
            return tuple(str(rule) for rule in rules)
    return ()


def _case_pmc_paths(run_dir: Path) -> tuple[str, ...]:
    paths = []
    for case_path in run_dir.glob("*/*/*pmc_evidence.json"):
        try:
            paths.append(str(case_path.relative_to(run_dir)))
        except ValueError:
            continue
    return tuple(paths)


def _case_ids_with_case_pmc(run_dir: Path) -> set[str]:
    case_ids: set[str] = set()
    for rel in _case_pmc_paths(run_dir):
        parts = Path(rel).parts
        if len(parts) >= 2:
            case_ids.add(parts[1])
    return case_ids


def _looks_like_path(value: object) -> bool:
    text = str(value)
    return "/" in text and not text.startswith(("http://", "https://"))


def _load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    rows: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(payload)
    return rows


def _issue(issues: list[dict[str, Any]], severity: str, code: str, message: str) -> None:
    issues.append({"severity": severity, "code": code, "message": message})


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Calibration Health Report: {Path(str(payload.get('run_dir'))).name}",
        "",
        f"- Overall status: {payload.get('overall_status')}",
        f"- PMC valid: {payload.get('pmc_valid')}",
        f"- Rule stats valid: {payload.get('rule_stats_valid')}",
        f"- Oracle valid: {payload.get('oracle_valid')}",
        f"- Context pollution: {payload.get('context_pollution_level')}",
        f"- Artifact completeness: {payload.get('artifact_completeness')}",
        f"- Severe: {(payload.get('severity_counts') or {}).get('severe', 0)}",
        f"- Warnings: {(payload.get('severity_counts') or {}).get('warning', 0)}",
        "",
        "## Issues",
        "",
        "| Severity | Code | Message |",
        "|---|---|---|",
    ]
    for issue in payload.get("issues") or ():
        lines.append(f"| {issue.get('severity')} | {issue.get('code')} | {issue.get('message')} |")
    return "\n".join(lines) + "\n"
