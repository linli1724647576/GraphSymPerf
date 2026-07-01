"""Review candidate extraction, clustering, and deduplication."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.cluster.signature import CandidateSignature, build_candidate_signature
from graph_sym_perf.core import (
    CaseRecord,
    ClaimLevel,
    ClusteringConfig,
    ConfirmationRecord,
    DeveloperStatus,
    EquivalenceRecord,
    MetricDelta,
    OracleRecord,
    PlanDiff,
    QueryFamily,
    QueryVariant,
    TargetSystem,
    ToolSupportLevel,
    stable_json_dumps,
    to_jsonable,
)
from graph_sym_perf.coverage import CoverageUnit, PMCEvidence


def write_review_candidates(run_dir: str | Path, *, update_summary: bool = True) -> Mapping[str, Any]:
    path = Path(run_dir)
    records = _load_case_records(path / "cases.jsonl")
    candidates = tuple(
        dict(candidate, rank=index)
        for index, candidate in enumerate(sorted(_candidate_items(records), key=_sort_key), start=1)
    )
    payload: Mapping[str, Any] = {
        "run_id": path.name,
        "candidate_count": len(candidates),
        "selection_policy": "oracle.is_candidate == true",
        "ranking_policy": "sort by support level, latency ratio, latency delta, then case id",
        "candidates": candidates,
    }
    _write_json(path / "review_candidates.json", payload)
    (path / "review_candidates.md").write_text(render_review_candidates_markdown(payload), encoding="utf-8")
    if update_summary:
        _update_summary(path, payload)
    return payload


def write_exploratory_leads(run_dir: str | Path, *, update_summary: bool = True) -> Mapping[str, Any]:
    path = Path(run_dir)
    records = _load_case_records(path / "cases.jsonl")
    leads = tuple(
        dict(candidate, rank=index)
        for index, candidate in enumerate(sorted(_exploratory_items(records), key=_sort_key), start=1)
    )
    payload: Mapping[str, Any] = {
        "run_id": path.name,
        "lead_count": len(leads),
        "selection_policy": "exploratory_oracle.is_candidate == true",
        "ranking_policy": "sort by support level, latency ratio, latency delta, then case id",
        "warning": "Exploratory leads are not issue-ready until promoted to strict semantic proof rules.",
        "leads": leads,
    }
    _write_json(path / "exploratory_leads.json", payload)
    (path / "exploratory_leads.md").write_text(render_exploratory_leads_markdown(payload), encoding="utf-8")
    if update_summary:
        _update_summary(path, {"exploratory_lead_count": len(leads), "exploratory_leads_artifact": "exploratory_leads.json"})
    return payload


def cluster_run_directory(
    run_dir: str | Path,
    *,
    clustering_config: ClusteringConfig | None = None,
) -> Mapping[str, Any]:
    path = Path(run_dir)
    config = clustering_config or ClusteringConfig()
    records = _load_case_records(path / "cases.jsonl")
    bundles = _candidate_bundles(path, records, config)
    signatures = [bundle["signature"] for bundle in bundles]
    family_groups = _group_signatures(signatures, "issue_family_key")
    unique_groups = _group_signatures(signatures, "unique_issue_key")
    duplicate_groups = {
        key: items for key, items in _group_signatures(signatures, "duplicate_key").items() if len(items) > 1
    }

    representatives = [
        _representative_payload(key, items)
        for key, items in sorted(family_groups.items(), key=lambda item: _group_sort_key(item[1]))
    ]
    payload = {
        "run_id": path.name,
        "candidate_count": len(signatures),
        "issue_family_count": len(family_groups),
        "unique_issue_count": len(unique_groups),
        "duplicate_group_count": len(duplicate_groups),
        "families_by_root_cause": dict(sorted(Counter(sig.root_cause_labels[0] for sig in signatures if sig.root_cause_labels).items())),
        "families_by_transform_rule": dict(sorted(Counter(sig.transform_rule for sig in signatures).items())),
        "families_by_support_level": dict(sorted(Counter(sig.support_level for sig in signatures).items())),
        "representatives": representatives,
        "issue_families": [
            _cluster_group_payload(key, items, level="issue_family")
            for key, items in sorted(family_groups.items(), key=lambda item: _group_sort_key(item[1]))
        ],
        "unique_issues": [
            _cluster_group_payload(key, items, level="unique_issue")
            for key, items in sorted(unique_groups.items(), key=lambda item: _group_sort_key(item[1]))
        ],
        "duplicate_groups": [
            _cluster_group_payload(key, items, level="duplicate_group")
            for key, items in sorted(duplicate_groups.items(), key=lambda item: _group_sort_key(item[1]))
        ],
        "generation_metadata": {
            "write_signatures": config.write_signatures,
            "include_plan_only": config.include_plan_only,
            "include_exploratory": config.include_exploratory,
            "representative_policy": config.representative_policy,
        },
    }
    if config.write_signatures:
        _write_signature_artifacts(path, bundles, payload)
    return payload


def write_clustered_summary(
    run_dir: str | Path,
    *,
    update_summary: bool = True,
    clustering_config: ClusteringConfig | None = None,
) -> Mapping[str, Any]:
    path = Path(run_dir)
    payload = cluster_run_directory(path, clustering_config=clustering_config)
    _write_json(path / "clustered_summary.json", payload)
    _write_json(path / "issue_families.json", payload["issue_families"])
    _write_json(path / "unique_issues.json", payload["unique_issues"])
    _write_json(path / "duplicate_groups.json", payload["duplicate_groups"])
    (path / "clustered_summary.md").write_text(render_clustered_summary_markdown(payload), encoding="utf-8")
    if update_summary:
        summary_update = {
            "candidate_count": payload["candidate_count"],
            "issue_family_count": payload["issue_family_count"],
            "unique_issue_count": payload["unique_issue_count"],
            "duplicate_group_count": payload["duplicate_group_count"],
            "clustered_summary_artifact": "clustered_summary.json",
            "issue_families_artifact": "issue_families.json",
            "unique_issues_artifact": "unique_issues.json",
            "duplicate_groups_artifact": "duplicate_groups.json",
        }
        if bool(((payload.get("generation_metadata") or {}).get("write_signatures"))):
            summary_update["candidate_signatures_artifact"] = "candidate_signatures.jsonl"
        _update_summary(path, summary_update)
    return payload


def render_review_candidates_markdown(payload: Mapping[str, Any]) -> str:
    candidates = tuple(payload.get("candidates") or ())
    lines = [
        f"# GraphSymPerf Review Candidates: {payload.get('run_id')}",
        "",
        "## Conclusion",
        "",
        f"This run produced {len(candidates)} symmetry-violation candidates.",
        "",
        "| Rank | Case | Support | Types | Fast | Slow | Latency Ratio | Delta ms | Artifacts |",
        "|---:|---|---|---|---|---|---:|---:|---|",
    ]
    for candidate in candidates:
        lines.append(
            "| {rank} | {case} | {support} | {types} | {fast} | {slow} | {ratio} | {delta} | {artifacts} |".format(
                rank=candidate.get("rank"),
                case=candidate.get("case_id"),
                support=candidate.get("support_level"),
                types=", ".join(candidate.get("candidate_types") or ()),
                fast=candidate.get("fast_variant_id"),
                slow=candidate.get("slow_variant_id"),
                ratio=_fmt(candidate.get("latency_ratio")),
                delta=_fmt(candidate.get("latency_delta_ms")),
                artifacts=_artifact_links(candidate.get("artifacts") or {}),
            )
        )
    lines.extend(
        [
            "",
            "## Review Checklist",
            "",
            "1. Re-run the fast and slow Cypher files in the same dedicated database.",
            "2. Confirm result equivalence before interpreting latency or PROFILE differences.",
            "3. Inspect `plan_diff.json`, `oracle.json`, and `profile_summaries.json` for the causal plan signal.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_exploratory_leads_markdown(payload: Mapping[str, Any]) -> str:
    leads = tuple(payload.get("leads") or ())
    lines = [
        f"# GraphSymPerf Exploratory Leads: {payload.get('run_id')}",
        "",
        "## Conclusion",
        "",
        f"This run produced {len(leads)} exploratory leads.",
        "",
        "These leads are not issue-ready until the responsible transformation is promoted to a strict semantic proof rule.",
        "",
        "| Rank | Case | Support | Types | Fast | Slow | Latency Ratio | Delta ms | Artifacts |",
        "|---:|---|---|---|---|---|---:|---:|---|",
    ]
    for candidate in leads:
        lines.append(
            "| {rank} | {case} | {support} | {types} | {fast} | {slow} | {ratio} | {delta} | {artifacts} |".format(
                rank=candidate.get("rank"),
                case=candidate.get("case_id"),
                support=candidate.get("support_level"),
                types=", ".join(candidate.get("candidate_types") or ()),
                fast=candidate.get("fast_variant_id"),
                slow=candidate.get("slow_variant_id"),
                ratio=_fmt(candidate.get("latency_ratio")),
                delta=_fmt(candidate.get("latency_delta_ms")),
                artifacts=_artifact_links(candidate.get("artifacts") or {}, prefix="exploratory_"),
            )
        )
    return "\n".join(lines) + "\n"


def render_clustered_summary_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Candidate Clustering Summary: {payload.get('run_id')}",
        "",
        f"- Candidate count: {payload.get('candidate_count', 0)}",
        f"- Issue family count: {payload.get('issue_family_count', 0)}",
        f"- Unique issue count: {payload.get('unique_issue_count', 0)}",
        f"- Duplicate group count: {payload.get('duplicate_group_count', 0)}",
        "",
        "## Representative Cases",
        "",
        "| Rank | Family Key | Representative | Root Cause | Transform Rule | Support | Metrics | PMC New Units | Artifacts |",
        "|---:|---|---|---|---|---|---|---:|---|",
    ]
    for index, item in enumerate(tuple(payload.get("representatives") or ()), start=1):
        lines.append(
            "| {rank} | {family_key} | {case_id} | {root_cause} | {transform_rule} | {support} | {metrics} | {pmc_new_units} | {artifacts} |".format(
                rank=index,
                family_key=item.get("family_key"),
                case_id=item.get("representative_case_id"),
                root_cause=item.get("root_cause"),
                transform_rule=item.get("transform_rule"),
                support=item.get("support_level"),
                metrics=item.get("metric_signature"),
                pmc_new_units=item.get("pmc_new_units", 0),
                artifacts=item.get("artifacts", ""),
            )
        )
    return "\n".join(lines) + "\n"


def _candidate_items(records: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    items: list[Mapping[str, Any]] = []
    for record in records:
        oracle = record.get("oracle") or {}
        confirmation = record.get("confirmation") or {}
        if not oracle.get("is_candidate"):
            continue
        evidence = oracle.get("evidence") or {}
        latency = evidence.get("latency") or {}
        items.append(
            {
                "case_id": record.get("case_id"),
                "system": record.get("system"),
                "candidate_types": tuple(oracle.get("candidate_types") or ()),
                "support_level": confirmation.get("support_level", "none"),
                "fast_variant_id": confirmation.get("fast_variant_id") or evidence.get("fast_variant_id"),
                "slow_variant_id": confirmation.get("slow_variant_id") or evidence.get("slow_variant_id"),
                "latency_ratio": confirmation.get("latency_ratio") or latency.get("ratio"),
                "latency_delta_ms": confirmation.get("latency_delta_ms") or latency.get("delta_ms"),
                "artifacts": record.get("artifacts") or {},
            }
        )
    return tuple(items)


def _exploratory_items(records: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    items: list[Mapping[str, Any]] = []
    for record in records:
        oracle = record.get("exploratory_oracle") or {}
        confirmation = record.get("exploratory_confirmation") or {}
        if not oracle.get("is_candidate"):
            continue
        evidence = oracle.get("evidence") or {}
        latency = evidence.get("latency") or {}
        items.append(
            {
                "case_id": record.get("case_id"),
                "system": record.get("system"),
                "candidate_types": tuple(oracle.get("candidate_types") or ()),
                "support_level": confirmation.get("support_level", "none"),
                "fast_variant_id": confirmation.get("fast_variant_id") or evidence.get("fast_variant_id"),
                "slow_variant_id": confirmation.get("slow_variant_id") or evidence.get("slow_variant_id"),
                "latency_ratio": confirmation.get("latency_ratio") or latency.get("ratio"),
                "latency_delta_ms": confirmation.get("latency_delta_ms") or latency.get("delta_ms"),
                "artifacts": record.get("artifacts") or {},
            }
        )
    return tuple(items)


def _candidate_bundles(
    run_dir: Path,
    records: tuple[Mapping[str, Any], ...],
    config: ClusteringConfig,
) -> list[Mapping[str, Any]]:
    bundles: list[Mapping[str, Any]] = []
    for raw_record in records:
        record = _case_record_from_mapping(raw_record)
        strict_bundle = _candidate_bundle(run_dir, record, raw_record, exploratory=False)
        if strict_bundle is not None and _include_bundle(strict_bundle, config):
            bundles.append(strict_bundle)
        if config.include_exploratory:
            exploratory_bundle = _candidate_bundle(run_dir, record, raw_record, exploratory=True)
            if exploratory_bundle is not None and _include_bundle(exploratory_bundle, config):
                bundles.append(exploratory_bundle)
    return bundles


def _candidate_bundle(
    run_dir: Path,
    record: CaseRecord,
    raw_record: Mapping[str, Any],
    *,
    exploratory: bool,
) -> Mapping[str, Any] | None:
    oracle = record.exploratory_oracle if exploratory else record.oracle
    confirmation = record.exploratory_confirmation if exploratory else record.confirmation
    pmc = record.exploratory_pmc if exploratory else record.pmc
    if oracle is None or not oracle.is_candidate:
        return None
    case_dir = run_dir / record.system.value / record.case_id
    prefix = "exploratory_" if exploratory else ""
    plan_diff = _load_plan_diff(case_dir, oracle, prefix=prefix)
    variant = _candidate_variant(record, confirmation, exploratory=exploratory)
    transform_rule = variant.transform_rule if variant is not None else (pmc.transform_rule if pmc is not None else "unknown_rule")
    transform_family = (
        str(variant.metadata.get("transform_family"))
        if variant is not None and variant.metadata.get("transform_family")
        else (pmc.transform_family if pmc is not None else _guess_family(transform_rule))
    )
    safety_tier = "exploratory" if exploratory else (variant.safety_tier if variant is not None else "strict")
    signature = build_candidate_signature(
        record=record,
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        oracle=oracle,
        confirmation=confirmation,
        plan_diff=plan_diff,
        pmc_evidence=pmc,
        extra_metadata={
            "artifacts": _artifact_links(record.artifacts, prefix=prefix),
            "artifact_paths": dict(record.artifacts),
            "query_length": _query_length(record, variant),
            "case_dir": str(case_dir),
            "run_id": run_dir.name,
        },
    )
    return {
        "record": record,
        "raw_record": raw_record,
        "oracle": oracle,
        "confirmation": confirmation,
        "pmc": pmc,
        "plan_diff": plan_diff,
        "transform_family": transform_family,
        "transform_rule": transform_rule,
        "safety_tier": safety_tier,
        "signature": signature,
        "artifacts": record.artifacts,
        "query_length": _query_length(record, variant),
    }


def _include_bundle(bundle: Mapping[str, Any], config: ClusteringConfig) -> bool:
    confirmation = bundle.get("confirmation")
    support_level = str(confirmation.support_level) if confirmation is not None else "none"
    if not config.include_plan_only and support_level == "plan_only":
        return False
    return True


def _candidate_variant(record: CaseRecord, confirmation: ConfirmationRecord | None, *, exploratory: bool) -> QueryVariant | None:
    target_variant_id = confirmation.slow_variant_id if confirmation is not None else None
    variants = record.family.variants
    if exploratory:
        variants = tuple(variant for variant in record.family.variants if variant.safety_tier == "exploratory")
    if target_variant_id is not None:
        for variant in variants:
            if variant.variant_id == target_variant_id:
                return variant
    return variants[0] if variants else None


def _load_plan_diff(case_dir: Path, oracle: OracleRecord, *, prefix: str) -> PlanDiff | None:
    plan_diff_path = case_dir / f"{prefix}plan_diff.json"
    if plan_diff_path.exists():
        return _plan_diff_from_mapping(json.loads(plan_diff_path.read_text(encoding="utf-8")))
    operator_tree_diff = oracle.evidence.get("operator_tree_diff")
    if isinstance(operator_tree_diff, Mapping):
        return _plan_diff_from_mapping(operator_tree_diff)
    return None


def _write_signature_artifacts(run_dir: Path, bundles: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> None:
    with (run_dir / "candidate_signatures.jsonl").open("w", encoding="utf-8") as file:
        for bundle in bundles:
            file.write(stable_json_dumps(bundle["signature"]) + "\n")
    for bundle in bundles:
        signature = bundle["signature"]
        case_dir = run_dir / signature.system / signature.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        suffix = "exploratory_candidate_signature.json" if signature.safety_tier == "exploratory" else "candidate_signature.json"
        _write_json(case_dir / suffix, signature)


def _group_signatures(signatures: list[CandidateSignature], field_name: str) -> dict[str, list[CandidateSignature]]:
    groups: dict[str, list[CandidateSignature]] = {}
    for signature in signatures:
        key = str(getattr(signature, field_name))
        groups.setdefault(key, []).append(signature)
    return groups


def _representative_payload(key: str, items: list[CandidateSignature]) -> Mapping[str, Any]:
    representative = _representative_signature(items)
    return {
        "family_key": key,
        "representative_case_id": representative.case_id,
        "root_cause": representative.root_cause_labels[0] if representative.root_cause_labels else "UnknownPlanAnomaly",
        "transform_rule": representative.transform_rule,
        "support_level": representative.support_level,
        "metric_signature": representative.metric_signature,
        "pmc_new_units": int(representative.metadata.get("pmc_new_units") or 0),
        "artifacts": representative.metadata.get("artifacts", ""),
    }


def _cluster_group_payload(key: str, items: list[CandidateSignature], *, level: str) -> Mapping[str, Any]:
    representative = _representative_signature(items)
    return {
        "cluster_key": key,
        "cluster_level": level,
        "size": len(items),
        "representative_case_id": representative.case_id,
        "case_ids": tuple(signature.case_id for signature in sorted(items, key=lambda item: item.case_id)),
        "root_cause_labels": representative.root_cause_labels,
        "candidate_types": representative.candidate_types,
        "transform_rules": dict(sorted(Counter(signature.transform_rule for signature in items).items())),
        "support_levels": dict(sorted(Counter(signature.support_level for signature in items).items())),
        "claim_levels": dict(sorted(Counter(signature.claim_level for signature in items).items())),
        "metric_signatures": dict(sorted(Counter(signature.metric_signature for signature in items).items())),
        "pmc_signatures": dict(sorted(Counter(signature.pmc_signature for signature in items).items())),
    }


def _representative_signature(items: list[CandidateSignature]) -> CandidateSignature:
    return sorted(items, key=_representative_sort_key)[0]


def _representative_sort_key(signature: CandidateSignature) -> tuple[int, int, float, float, int, str]:
    return (
        -_developer_rank(str(signature.metadata.get("developer_status") or signature.claim_level)),
        -_support_rank(signature.support_level),
        -float(signature.metadata.get("max_work_ratio") or 0.0),
        -float(signature.metadata.get("latency_ratio") or 0.0),
        int(signature.metadata.get("query_length") or 10**9),
        signature.case_id,
    )


def _group_sort_key(items: list[CandidateSignature]) -> tuple[int, tuple[int, int, float, float, int, str]]:
    representative = _representative_signature(items)
    return (-len(items), _representative_sort_key(representative))


def _sort_key(candidate: Mapping[str, Any]) -> tuple[int, float, float, str]:
    return (
        -_support_rank(str(candidate.get("support_level"))),
        -float(candidate.get("latency_ratio") or 0.0),
        -float(candidate.get("latency_delta_ms") or 0.0),
        str(candidate.get("case_id")),
    )


def _artifact_links(artifacts: Mapping[str, Any], *, prefix: str = "") -> str:
    wanted = (f"{prefix}fast_query", f"{prefix}slow_query", "symmetry_family", "pmc_evidence")
    values = [str(artifacts[key]) for key in wanted if key in artifacts]
    return ", ".join(values)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _load_case_records(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.exists():
        return ()
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on {path}:{line_number}: {exc}") from exc
        if not isinstance(record, Mapping):
            raise ValueError(f"case record on {path}:{line_number} must be a JSON object")
        records.append(record)
    return tuple(records)


def _case_record_from_mapping(mapping: Mapping[str, Any]) -> CaseRecord:
    return CaseRecord(
        system=TargetSystem.parse(str(mapping.get("system"))),
        case_id=str(mapping.get("case_id")),
        family=_query_family_from_mapping(mapping.get("family") or {}),
        equivalences=tuple(_equivalence_from_mapping(item) for item in tuple(mapping.get("equivalences") or ())),
        oracle=_oracle_from_mapping(mapping.get("oracle")),
        confirmation=_confirmation_from_mapping(mapping.get("confirmation")),
        artifacts={str(key): str(value) for key, value in (mapping.get("artifacts") or {}).items()},
        state=str(mapping.get("state") or "unknown"),
        reason=str(mapping.get("reason") or ""),
        developer_status=str(mapping.get("developer_status") or DeveloperStatus.UNKNOWN.value),
        claim_level=str(mapping.get("claim_level") or ClaimLevel.CANDIDATE.value),
        exploratory_oracle=_oracle_from_mapping(mapping.get("exploratory_oracle")),
        exploratory_confirmation=_confirmation_from_mapping(mapping.get("exploratory_confirmation")),
        pmc=_pmc_from_mapping(mapping.get("pmc")),
        exploratory_pmc=_pmc_from_mapping(mapping.get("exploratory_pmc")),
    )


def _query_family_from_mapping(mapping: Mapping[str, Any]) -> QueryFamily:
    return QueryFamily(
        case_id=str(mapping.get("case_id") or ""),
        seed_query=str(mapping.get("seed_query") or ""),
        parameters=mapping.get("parameters") or {},
        variants=tuple(_variant_from_mapping(item) for item in tuple(mapping.get("variants") or ())),
        skipped_variants=tuple(_variant_from_mapping(item) for item in tuple(mapping.get("skipped_variants") or ())),
        source=str(mapping.get("source") or "gdsmith"),
        generator_case_id=mapping.get("generator_case_id"),
        raw_query=mapping.get("raw_query"),
    )


def _variant_from_mapping(mapping: Mapping[str, Any]) -> QueryVariant:
    return QueryVariant(
        variant_id=str(mapping.get("variant_id") or ""),
        query=str(mapping.get("query") or ""),
        transform_rule=str(mapping.get("transform_rule") or ""),
        proof_obligation=str(mapping.get("proof_obligation") or ""),
        static_safe=bool(mapping.get("static_safe", True)),
        safety_tier=str(mapping.get("safety_tier") or "strict"),
        skip_reason=mapping.get("skip_reason"),
        metadata=mapping.get("metadata") or {},
    )


def _equivalence_from_mapping(mapping: Mapping[str, Any]) -> EquivalenceRecord:
    return EquivalenceRecord(
        case_id=str(mapping.get("case_id") or ""),
        passed=bool(mapping.get("passed")),
        left_hash=mapping.get("left_hash"),
        right_hash=mapping.get("right_hash"),
        left_count=int(mapping.get("left_count") or 0),
        right_count=int(mapping.get("right_count") or 0),
        reason=str(mapping.get("reason") or ""),
        sample_left=tuple(mapping.get("sample_left") or ()),
        sample_right=tuple(mapping.get("sample_right") or ()),
    )


def _oracle_from_mapping(mapping: Mapping[str, Any] | None) -> OracleRecord | None:
    if not isinstance(mapping, Mapping):
        return None
    return OracleRecord(
        case_id=str(mapping.get("case_id") or ""),
        is_candidate=bool(mapping.get("is_candidate")),
        candidate_types=tuple(str(item) for item in tuple(mapping.get("candidate_types") or ())),
        reason=str(mapping.get("reason") or ""),
        evidence=mapping.get("evidence") or {},
    )


def _confirmation_from_mapping(mapping: Mapping[str, Any] | None) -> ConfirmationRecord | None:
    if not isinstance(mapping, Mapping):
        return None
    return ConfirmationRecord(
        case_id=str(mapping.get("case_id") or ""),
        confirmed=bool(mapping.get("confirmed")),
        original_median_ms=_float_or_none(mapping.get("original_median_ms")),
        transformed_median_ms=_float_or_none(mapping.get("transformed_median_ms")),
        latency_ratio=_float_or_none(mapping.get("latency_ratio")),
        latency_delta_ms=_float_or_none(mapping.get("latency_delta_ms")),
        original_samples_ms=tuple(float(item) for item in tuple(mapping.get("original_samples_ms") or ())),
        transformed_samples_ms=tuple(float(item) for item in tuple(mapping.get("transformed_samples_ms") or ())),
        reason=str(mapping.get("reason") or ""),
        level=str(mapping.get("level") or "none"),
        support_level=str(mapping.get("support_level") or ToolSupportLevel.PLAN_ONLY.value),
        developer_status=str(mapping.get("developer_status") or DeveloperStatus.UNKNOWN.value),
        claim_level=str(mapping.get("claim_level") or ClaimLevel.CANDIDATE.value),
        metric_deltas=tuple(_metric_delta_from_mapping(item) for item in tuple(mapping.get("metric_deltas") or ())),
        profile_samples=mapping.get("profile_samples") or {},
        profile_diff_artifact=mapping.get("profile_diff_artifact"),
        fast_variant_id=mapping.get("fast_variant_id"),
        slow_variant_id=mapping.get("slow_variant_id"),
    )


def _pmc_from_mapping(mapping: Mapping[str, Any] | None) -> PMCEvidence | None:
    if not isinstance(mapping, Mapping):
        return None
    return PMCEvidence(
        case_id=str(mapping.get("case_id") or ""),
        transform_family=str(mapping.get("transform_family") or ""),
        transform_rule=str(mapping.get("transform_rule") or ""),
        safety_tier=str(mapping.get("safety_tier") or "strict"),
        fast_variant_id=mapping.get("fast_variant_id"),
        slow_variant_id=mapping.get("slow_variant_id"),
        structural_hash_changed=bool(mapping.get("structural_hash_changed")),
        units=tuple(_coverage_unit_from_mapping(item) for item in tuple(mapping.get("units") or ())),
        new_units=tuple(_coverage_unit_from_mapping(item) for item in tuple(mapping.get("new_units") or ())),
        unit_count=int(mapping.get("unit_count") or len(tuple(mapping.get("units") or ()))),
        new_unit_count=int(mapping.get("new_unit_count") or len(tuple(mapping.get("new_units") or ()))),
        novelty_score=float(mapping.get("novelty_score") or 0.0),
        metadata=mapping.get("metadata") or {},
    )


def _coverage_unit_from_mapping(mapping: Mapping[str, Any]) -> CoverageUnit:
    return CoverageUnit(
        kind=str(mapping.get("kind") or ""),
        key=str(mapping.get("key") or ""),
        label=str(mapping.get("label") or ""),
        weight=float(mapping.get("weight") or 1.0),
        metadata=mapping.get("metadata") or {},
    )


def _plan_diff_from_mapping(mapping: Mapping[str, Any]) -> PlanDiff:
    return PlanDiff(
        structural_hash_changed=bool(mapping.get("structural_hash_changed")),
        changes=tuple(mapping.get("changes") or ()),
        added_nodes=tuple(mapping.get("added_nodes") or ()),
        removed_nodes=tuple(mapping.get("removed_nodes") or ()),
        replaced_nodes=tuple(mapping.get("replaced_nodes") or ()),
        family_delta={str(key): int(value) for key, value in (mapping.get("family_delta") or {}).items()},
        access_point_delta=mapping.get("access_point_delta") or {},
        expand_chain_delta=mapping.get("expand_chain_delta") or {},
        barrier_delta=mapping.get("barrier_delta") or {},
        estimated_rows_delta=mapping.get("estimated_rows_delta") or {},
        metric_deltas=tuple(_metric_delta_from_mapping(item) for item in tuple(mapping.get("metric_deltas") or ())),
    )


def _metric_delta_from_mapping(mapping: Mapping[str, Any]) -> MetricDelta:
    return MetricDelta(
        metric=str(mapping.get("metric") or ""),
        original=_float_or_none(mapping.get("original")),
        transformed=_float_or_none(mapping.get("transformed")),
        ratio=_float_or_none(mapping.get("ratio")),
        delta=_float_or_none(mapping.get("delta")),
        worsened=bool(mapping.get("worsened")),
    )


def _query_length(record: CaseRecord, variant: QueryVariant | None) -> int:
    candidates = [record.family.seed_query]
    if variant is not None:
        candidates.append(variant.query)
    lengths = [len(query) for query in candidates if query]
    return min(lengths) if lengths else 10**9


def _guess_family(transform_rule: str) -> str:
    return "symmetry" if "symmetry" in transform_rule else "bool_redundancy"


def _support_rank(value: str) -> int:
    support_rank = {"profile_supported": 3, "latency_supported": 2, "plan_only": 1, "none": 0}
    return support_rank.get(value, 0)


def _developer_rank(value: str) -> int:
    developer_rank = {
        ClaimLevel.FIXED.value: 6,
        ClaimLevel.DEVELOPER_CONFIRMED.value: 5,
        DeveloperStatus.FIXED.value: 4,
        DeveloperStatus.CONFIRMED.value: 3,
        DeveloperStatus.REPORTED.value: 2,
        ClaimLevel.STRONG_CANDIDATE.value: 1,
    }
    return developer_rank.get(value, 0)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _update_summary(path: Path, payload: Mapping[str, Any]) -> None:
    summary_path = path / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(json.loads(stable_json_dumps(payload)))
    _write_json(summary_path, summary)
