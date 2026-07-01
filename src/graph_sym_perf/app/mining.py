"""End-to-end symmetry mining assembly."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping

from graph_sym_perf.adapters import GraphAdapter, create_adapter
from graph_sym_perf.config import load_run_config
from graph_sym_perf.core import (
    CaseRecord,
    ConfigError,
    ConfirmationRecord,
    EquivalenceRecord,
    MetricDelta,
    OracleRecord,
    QueryCase,
    RunConfig,
    SystemConfig,
    SystemStatus,
    SymmetryFamily,
    SymmetryVariant,
    TargetSystem,
    VariantProfileSummary,
    to_jsonable,
)
from graph_sym_perf.equivalence import compare_results
from graph_sym_perf.oracle import detect_symmetry_violation
from graph_sym_perf.plan import diff_plan_records, profile_record_from_execution, unavailable_plan
from graph_sym_perf.reporter import ArtifactWriter
from graph_sym_perf.transforms import generate_transform_family
from graph_sym_perf.cluster import write_exploratory_leads, write_review_candidates


EXIT_OK = 0
EXIT_NO_READY_SYSTEMS = 2
EXIT_CONFIG_ERROR = 3


def run_gdsmith_cases(
    *,
    config_path: str | Path,
    systems: str = "auto",
    query_cases: tuple[QueryCase, ...],
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    config = load_run_config(config_path)
    selected_systems = _select_systems(config, systems)
    run_id = run_id or _default_run_id()
    writer = ArtifactWriter(config, run_id)
    writer.write_json("config_snapshot.json", config)

    adapters: list[GraphAdapter] = []
    statuses: list[SystemStatus] = []
    try:
        for system_config in selected_systems:
            adapter = create_adapter(system_config, config, env or {}, allow_create=False)
            status = _preflight(adapter)
            statuses.append(status)
            if status.ready:
                adapters.append(adapter)
        writer.write_json("preflight.json", {"run_id": run_id, "systems": statuses})
        if not adapters:
            writer.write_json("summary.json", _summary(run_id, statuses, [], no_ready=True))
            return EXIT_NO_READY_SYSTEMS
        records = _run_query_cases(config, writer, adapters, query_cases)
        writer.write_json("symmetry_violations.json", _violation_payload(records))
        write_review_candidates(writer.run_dir, update_summary=False)
        write_exploratory_leads(writer.run_dir, update_summary=False)
        writer.write_json("summary.json", _summary(run_id, statuses, records, no_ready=False))
        return EXIT_OK
    finally:
        for adapter in adapters:
            adapter.close()


def _preflight(adapter: GraphAdapter) -> SystemStatus:
    dependency_ok, dependency_reason = adapter.check_dependency()
    environment_ok, missing_env = adapter.check_environment()
    if not dependency_ok:
        return SystemStatus(
            system=adapter.target,
            state="skipped",
            dependency_ok=False,
            environment_ok=environment_ok,
            connectivity_ok=False,
            reason=dependency_reason,
            missing_env=missing_env,
        )
    if not environment_ok:
        return SystemStatus(
            system=adapter.target,
            state="skipped",
            dependency_ok=True,
            environment_ok=False,
            connectivity_ok=False,
            reason="missing required environment variables",
            missing_env=missing_env,
        )
    connectivity_ok, connectivity_reason = adapter.connectivity_check()
    return SystemStatus(
        system=adapter.target,
        state="ready" if connectivity_ok else "skipped",
        dependency_ok=True,
        environment_ok=True,
        connectivity_ok=connectivity_ok,
        reason=connectivity_reason,
    )


def _run_query_cases(
    config: RunConfig,
    writer: ArtifactWriter,
    adapters: list[GraphAdapter],
    query_cases: tuple[QueryCase, ...],
) -> list[CaseRecord]:
    records: list[CaseRecord] = []
    for adapter in adapters:
        system_name = adapter.target.value
        for query_case in query_cases:
            family = generate_transform_family(query_case, config.transforms, target=adapter.target)
            artifacts = writer.write_query_family(system_name, family)
            _write_transform_artifacts(writer, system_name, family, artifacts)
            record = _run_family(config, writer, adapter, family, artifacts)
            writer.write_case_record(record)
            records.append(record)
    return records


def _run_family(
    config: RunConfig,
    writer: ArtifactWriter,
    adapter: GraphAdapter,
    family: SymmetryFamily,
    artifacts: dict[str, str],
) -> CaseRecord:
    system_name = adapter.target.value
    base = f"{system_name}/{family.case_id}"
    if not family.variants:
        return _case_record(
            adapter.target,
            family,
            (),
            None,
            None,
            artifacts,
            "skipped",
            "NO_STATICALLY_SAFE_VARIANT",
        )
    try:
        seed_result = adapter.execute(
            family.seed_query,
            family.parameters,
            "seed",
            max_rows=config.equivalence.max_result_rows,
            timeout_seconds=config.equivalence.timeout_seconds,
        )
        writer.write_json(f"{base}/seed_result_sample.json", seed_result.rows[:10])
    except Exception as exc:
        writer.write_json(f"{base}/error.json", {"error": str(exc), "stage": "seed_execute"})
        return _case_record(adapter.target, family, (), None, None, artifacts, "error", str(exc))

    equivalences: list[EquivalenceRecord] = []
    equivalent_variants: list[SymmetryVariant] = []
    for variant in family.variants:
        try:
            variant_result = adapter.execute(
                variant.query,
                family.parameters,
                variant.variant_id,
                max_rows=config.equivalence.max_result_rows,
                timeout_seconds=config.equivalence.timeout_seconds,
            )
            writer.write_json(f"{base}/{variant.variant_id}_result_sample.json", variant_result.rows[:10])
            equivalence = compare_results(
                variant.variant_id,
                seed_result.rows,
                variant_result.rows,
                config.equivalence.max_result_rows,
            )
        except Exception as exc:
            equivalence = EquivalenceRecord(
                case_id=variant.variant_id,
                passed=False,
                left_hash=None,
                right_hash=None,
                left_count=len(seed_result.rows),
                right_count=0,
                reason=f"QUERY_ERROR: {exc}",
            )
        writer.write_json(f"{base}/{variant.variant_id}_equivalence.json", equivalence)
        equivalences.append(equivalence)
        if equivalence.passed:
            equivalent_variants.append(variant)

    if not equivalent_variants:
        return _case_record(
            adapter.target,
            family,
            tuple(equivalences),
            None,
            None,
            artifacts,
            "equivalence_failed",
            "NO_DYNAMICALLY_EQUIVALENT_VARIANT",
        )

    profiles = [_profile_variant(config, adapter, "seed", family.seed_query, family.parameters)]
    for variant in equivalent_variants:
        profiles.append(_profile_variant(config, adapter, variant.variant_id, variant.query, family.parameters))
    writer.write_json(f"{base}/profile_summaries.json", profiles)

    comparable = [profile for profile in profiles if profile.available and profile.median_ms is not None]
    profile_by_id = {profile.variant_id: profile for profile in comparable}
    strict_ids = {variant.variant_id for variant in equivalent_variants if variant.safety_tier == "strict"}
    exploratory_ids = {variant.variant_id for variant in equivalent_variants if variant.safety_tier == "exploratory"}
    strict_comparable = [profile for profile in comparable if profile.variant_id == "seed" or profile.variant_id in strict_ids]
    exploratory_comparable = [
        profile for profile in comparable if profile.variant_id == "seed" or profile.variant_id in exploratory_ids
    ]

    exploratory_oracle = None
    exploratory_confirmation = None
    if len(exploratory_comparable) >= 2:
        exploratory_oracle, exploratory_confirmation = _oracle_from_profiles(
            config,
            writer,
            adapter,
            family,
            exploratory_comparable,
            artifacts,
            base,
            prefix="exploratory_",
        )

    if len(strict_comparable) < 2:
        confirmation = ConfirmationRecord(
            case_id=family.case_id,
            confirmed=False,
            original_median_ms=None,
            transformed_median_ms=None,
            latency_ratio=None,
            latency_delta_ms=None,
            original_samples_ms=(),
            transformed_samples_ms=(),
            reason="NO_STRICT_COMPARABLE_PROFILE_SAMPLES",
        )
        writer.write_json(f"{base}/confirmation.json", confirmation)
        return _case_record(
            adapter.target,
            family,
            tuple(equivalences),
            None,
            confirmation,
            artifacts,
            "exploratory_equivalent_no_candidate" if exploratory_ids else "equivalent_no_profile",
            confirmation.reason,
            exploratory_oracle,
            exploratory_confirmation,
        )

    oracle, confirmation = _oracle_from_profiles(
        config,
        writer,
        adapter,
        family,
        strict_comparable,
        artifacts,
        base,
        prefix="",
    )
    state = "confirmed" if confirmation.confirmed else "equivalent_no_candidate"
    reason = confirmation.reason
    return _case_record(
        adapter.target,
        family,
        tuple(equivalences),
        oracle,
        confirmation,
        artifacts,
        state,
        reason,
        exploratory_oracle,
        exploratory_confirmation,
    )


def _oracle_from_profiles(
    config: RunConfig,
    writer: ArtifactWriter,
    adapter: GraphAdapter,
    family: SymmetryFamily,
    comparable: list[VariantProfileSummary],
    artifacts: dict[str, str],
    base: str,
    *,
    prefix: str,
) -> tuple[OracleRecord, ConfirmationRecord]:
    fast = min(comparable, key=lambda item: item.median_ms or float("inf"))
    slow = max(comparable, key=lambda item: item.median_ms or 0.0)
    fast_plan = fast.plan or unavailable_plan(adapter.target, "fast", "missing fast plan")
    slow_plan = slow.plan or unavailable_plan(adapter.target, "slow", "missing slow plan")
    plan_diff = diff_plan_records(fast_plan, slow_plan)
    fast_query_path = writer.write_text(f"{base}/{prefix}fast_query.cypher", fast.query + "\n")
    slow_query_path = writer.write_text(f"{base}/{prefix}slow_query.cypher", slow.query + "\n")
    artifacts[f"{prefix}fast_query"] = str(fast_query_path.relative_to(writer.run_dir))
    artifacts[f"{prefix}slow_query"] = str(slow_query_path.relative_to(writer.run_dir))
    writer.write_json(f"{base}/{prefix}plan_diff.json", plan_diff)
    oracle = detect_symmetry_violation(family.case_id, fast, slow, plan_diff, config.confirmation)
    writer.write_json(f"{base}/{prefix}oracle.json", oracle)
    confirmation = _confirmation_from_oracle(family.case_id, fast, slow, oracle)
    writer.write_json(f"{base}/{prefix}confirmation.json", confirmation)
    return oracle, confirmation


def _profile_variant(
    config: RunConfig,
    adapter: GraphAdapter,
    variant_id: str,
    query: str,
    parameters: Mapping[str, object],
) -> VariantProfileSummary:
    samples: list[float] = []
    metrics_samples: list[Mapping[str, float]] = []
    profile_plan = None
    plan_record = adapter.explain(query, parameters, f"{variant_id}_explain")
    try:
        for _ in range(max(0, config.confirmation.warmup)):
            adapter.profile(query, parameters, f"{variant_id}_warmup", timeout_seconds=config.confirmation.timeout_seconds)
        for _ in range(max(1, config.confirmation.repeats)):
            result = adapter.profile(
                query,
                parameters,
                f"{variant_id}_profile",
                max_rows=config.query_generation.result_limit,
                timeout_seconds=config.confirmation.timeout_seconds,
            )
            samples.append(result.elapsed_ms)
            profile = profile_record_from_execution(result)
            metrics_samples.append(profile.metrics)
            profile_plan = profile.plan or profile_plan
    except Exception as exc:
        return VariantProfileSummary(
            variant_id=variant_id,
            query=query,
            query_kind=variant_id,
            median_ms=float(median(samples)) if samples else None,
            samples_ms=tuple(samples),
            plan=plan_record,
            profile_plan=profile_plan,
            metrics=_median_metrics(tuple(metrics_samples)),
            available=False,
            reason=str(exc),
        )
    return VariantProfileSummary(
        variant_id=variant_id,
        query=query,
        query_kind=variant_id,
        median_ms=float(median(samples)) if samples else None,
        samples_ms=tuple(samples),
        plan=plan_record,
        profile_plan=profile_plan,
        metrics=_median_metrics(tuple(metrics_samples)),
    )


def _median_metrics(samples: tuple[Mapping[str, float], ...]) -> Mapping[str, float]:
    metrics: dict[str, float] = {}
    keys = sorted({key for sample in samples for key in sample})
    for key in keys:
        values = [float(sample[key]) for sample in samples if key in sample]
        if values:
            metrics[key] = float(median(values))
    return metrics


def _confirmation_from_oracle(
    case_id: str,
    fast: VariantProfileSummary,
    slow: VariantProfileSummary,
    oracle: OracleRecord,
) -> ConfirmationRecord:
    latency = oracle.evidence.get("latency", {}) if isinstance(oracle.evidence, Mapping) else {}
    metric_deltas = tuple(
        item for item in (oracle.evidence.get("metric_deltas", ()) if isinstance(oracle.evidence, Mapping) else ())
        if isinstance(item, MetricDelta)
    )
    profile_supported = any(delta.worsened for delta in metric_deltas if delta.metric in {"db_hits", "rows", "memory_bytes"})
    latency_supported = bool(latency.get("worsened"))
    support_level = "profile_supported" if profile_supported else "latency_supported" if latency_supported else "plan_only"
    level = "strong" if profile_supported and latency_supported else "moderate" if profile_supported else "weak" if latency_supported else "none"
    return ConfirmationRecord(
        case_id=case_id,
        confirmed=oracle.is_candidate and support_level != "plan_only",
        original_median_ms=fast.median_ms,
        transformed_median_ms=slow.median_ms,
        latency_ratio=latency.get("ratio"),
        latency_delta_ms=latency.get("delta_ms"),
        original_samples_ms=fast.samples_ms,
        transformed_samples_ms=slow.samples_ms,
        reason=oracle.reason,
        level=level,
        support_level=support_level,
        metric_deltas=metric_deltas,
        profile_samples={"fast": to_jsonable(fast), "slow": to_jsonable(slow)},
        fast_variant_id=fast.variant_id,
        slow_variant_id=slow.variant_id,
    )


def _case_record(
    target: TargetSystem,
    family: SymmetryFamily,
    equivalences: tuple[EquivalenceRecord, ...],
    oracle: OracleRecord | None,
    confirmation: ConfirmationRecord | None,
    artifacts: Mapping[str, str],
    state: str,
    reason: str,
    exploratory_oracle: OracleRecord | None = None,
    exploratory_confirmation: ConfirmationRecord | None = None,
) -> CaseRecord:
    return CaseRecord(
        system=target,
        case_id=family.case_id,
        family=family,
        equivalences=equivalences,
        oracle=oracle,
        confirmation=confirmation,
        artifacts=artifacts,
        state=state,
        reason=reason,
        exploratory_oracle=exploratory_oracle,
        exploratory_confirmation=exploratory_confirmation,
    )


def _write_transform_artifacts(
    writer: ArtifactWriter,
    system_name: str,
    family: SymmetryFamily,
    artifacts: dict[str, str],
) -> None:
    base = f"{system_name}/{family.case_id}"
    transform_path = writer.write_json(
        f"{base}/symmetry_family.json",
        {
            "case_id": family.case_id,
            "variant_count": len(family.variants),
            "skipped_variant_count": len(family.skipped_variants),
            "variants": family.variants,
            "skipped_variants": family.skipped_variants,
        },
    )
    artifacts["symmetry_family"] = str(transform_path.relative_to(writer.run_dir))


def _summary(run_id: str, statuses: list[SystemStatus], records: list[CaseRecord], *, no_ready: bool) -> dict[str, object]:
    return {
        "run_id": run_id,
        "no_ready_systems": no_ready,
        "systems": to_jsonable(statuses),
        "generated_cases": len(records),
        "families_with_static_variants": sum(1 for record in records if record.family.variants),
        "strict_static_families": sum(
            1 for record in records if any(variant.safety_tier == "strict" for variant in record.family.variants)
        ),
        "exploratory_static_families": sum(
            1 for record in records if any(variant.safety_tier == "exploratory" for variant in record.family.variants)
        ),
        "equivalence_passed": sum(1 for record in records if any(item.passed for item in record.equivalences)),
        "strict_equivalence_passed": sum(1 for record in records if _tier_equivalence_passed(record, "strict")),
        "exploratory_equivalence_passed": sum(1 for record in records if _tier_equivalence_passed(record, "exploratory")),
        "symmetry_violation_candidates": sum(1 for record in records if record.oracle and record.oracle.is_candidate),
        "exploratory_leads": sum(
            1 for record in records if record.exploratory_oracle and record.exploratory_oracle.is_candidate
        ),
        "runtime_confirmed": sum(1 for record in records if record.confirmation and record.confirmation.confirmed),
        "profile_supported_candidates": sum(1 for record in records if record.confirmation and record.confirmation.support_level == "profile_supported"),
        "candidate_types": _candidate_type_counts(records),
        "transform_rules": _transform_rule_counts(records),
        "states": _state_counts(records),
        "symmetry_violations_artifact": "symmetry_violations.json",
        "review_candidates_markdown": "review_candidates.md",
    }


def _tier_equivalence_passed(record: CaseRecord, tier: str) -> bool:
    variant_tiers = {variant.variant_id: variant.safety_tier for variant in record.family.variants}
    return any(item.passed and variant_tiers.get(item.case_id) == tier for item in record.equivalences)


def _violation_payload(records: list[CaseRecord]) -> Mapping[str, object]:
    candidates = [record for record in records if record.oracle and record.oracle.is_candidate]
    return {
        "candidate_count": len(candidates),
        "candidates": tuple(
            {
                "case_id": record.case_id,
                "system": record.system.value,
                "candidate_types": record.oracle.candidate_types if record.oracle else (),
                "support_level": record.confirmation.support_level if record.confirmation else "none",
                "fast_variant_id": record.confirmation.fast_variant_id if record.confirmation else None,
                "slow_variant_id": record.confirmation.slow_variant_id if record.confirmation else None,
                "latency_ratio": record.confirmation.latency_ratio if record.confirmation else None,
                "latency_delta_ms": record.confirmation.latency_delta_ms if record.confirmation else None,
                "artifacts": record.artifacts,
                "evidence": record.oracle.evidence if record.oracle else {},
            }
            for record in sorted(candidates, key=_candidate_sort_key)
        ),
    }


def _candidate_sort_key(record: CaseRecord) -> tuple[float, float, str]:
    ratio = record.confirmation.latency_ratio if record.confirmation and record.confirmation.latency_ratio else 0.0
    delta = record.confirmation.latency_delta_ms if record.confirmation and record.confirmation.latency_delta_ms else 0.0
    return (-ratio, -delta, record.case_id)


def _select_systems(config: RunConfig, systems: str) -> tuple[SystemConfig, ...]:
    if systems == "auto":
        return config.systems
    selected_targets = tuple(TargetSystem.parse(item) for item in systems.split(",") if item.strip())
    if not selected_targets:
        raise ConfigError("--systems must be auto or a comma-separated system list")
    selected: list[SystemConfig] = []
    for target in selected_targets:
        system_config = config.system_config(target)
        if system_config is None:
            raise ConfigError(f"system {target.value} is not configured")
        selected.append(system_config)
    return tuple(selected)


def _with_max_cases(config: RunConfig, max_cases: int) -> RunConfig:
    return RunConfig(
        source_path=config.source_path,
        paths=config.paths,
        systems=config.systems,
        dataset=config.dataset,
        query_generation=replace(
            config.query_generation,
            max_cases=max_cases,
            gdsmith=replace(config.query_generation.gdsmith, num_queries=max_cases),
        ),
        equivalence=config.equivalence,
        confirmation=config.confirmation,
        oracle=config.oracle,
        transforms=config.transforms,
    )


def _state_counts(records: list[CaseRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.state] = counts.get(record.state, 0) + 1
    return counts


def _candidate_type_counts(records: list[CaseRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if not record.oracle:
            continue
        for candidate_type in record.oracle.candidate_types:
            counts[candidate_type] = counts.get(candidate_type, 0) + 1
    return counts


def _transform_rule_counts(records: list[CaseRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for variant in record.family.variants:
            counts[variant.transform_rule] = counts.get(variant.transform_rule, 0) + 1
    return counts


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
