"""End-to-end symmetry mining assembly."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Mapping

from graph_sym_perf.adapters import GraphAdapter, create_adapter
from graph_sym_perf.config import environment_with_dotenv, load_run_config
from graph_sym_perf.core import (
    CaseRecord,
    ConfigError,
    ConfirmationRecord,
    EquivalenceRecord,
    MetricDelta,
    OracleRecord,
    PlanDiff,
    QueryCase,
    QueryFamily,
    RunConfig,
    SystemConfig,
    SystemStatus,
    SymmetryFamily,
    SymmetryVariant,
    TargetSystem,
    VariantProfileSummary,
    to_jsonable,
)
from graph_sym_perf.coverage import PMCCoverageState, PMCSummary, make_pmc_evidence, write_pmc_artifacts
from graph_sym_perf.datasets import generate_mini_ldbc
from graph_sym_perf.diagnostics import summarize_skip_errors
from graph_sym_perf.equivalence import compare_results
from graph_sym_perf.feedback import TransformScheduler, initialize_feedback_state, summarize_feedback_state
from graph_sym_perf.oracle import detect_symmetry_violation
from graph_sym_perf.plan import diff_plan_records, profile_record_from_execution, unavailable_plan
from graph_sym_perf.querygen import generate_query_cases
from graph_sym_perf.reporter import ArtifactWriter
from graph_sym_perf.transforms import generate_transform_family
from graph_sym_perf.cluster import write_clustered_summary, write_exploratory_leads, write_review_candidates


EXIT_OK = 0
EXIT_NO_READY_SYSTEMS = 2
EXIT_CONFIG_ERROR = 3


def run_ldbc_mining(
    *,
    config_path: str | Path,
    systems: str = "auto",
    load: bool = False,
    max_cases: int | None = None,
    run_id: str | None = None,
    feedback_enabled: bool | None = None,
    max_rules_per_case: int | None = None,
    transform_family: str = "auto",
    env: Mapping[str, str] | None = None,
) -> int:
    env = environment_with_dotenv(env)
    config = load_run_config(config_path)
    if max_cases is not None:
        config = _with_max_cases(config, max_cases)
    config = _with_runtime_overrides(
        config,
        feedback_enabled=feedback_enabled,
        max_rules_per_case=max_rules_per_case,
        transform_family=transform_family,
    )
    if load and env.get(config.dataset.allow_load_env) != "1":
        raise ConfigError(
            f"--load requires {config.dataset.allow_load_env}=1 to avoid writing into a database by accident"
        )

    selected_systems = _select_systems(config, systems)
    run_id = run_id or _default_run_id()
    writer = ArtifactWriter(config, run_id)
    writer.write_json("config_snapshot.json", config)

    adapters: list[GraphAdapter] = []
    statuses: list[SystemStatus] = []
    try:
        for system_config in selected_systems:
            adapter = create_adapter(system_config, config, env, allow_create=load)
            status = _preflight(adapter)
            statuses.append(status)
            if status.ready:
                adapters.append(adapter)
        writer.write_json("preflight.json", {"run_id": run_id, "systems": statuses})
        if not adapters:
            writer.write_json("summary.json", _summary(run_id, statuses, [], no_ready=True))
            return EXIT_NO_READY_SYSTEMS

        graph = generate_mini_ldbc(config.dataset)
        if load:
            for adapter in adapters:
                adapter.ensure_schema()
                adapter.load_mini_ldbc(graph)
        writer.write_json("synthetic_graph_spec.json", graph)

        query_cases = generate_query_cases(config.dataset, config.query_generation)
        records, run_outputs = _run_query_cases(config, writer, adapters, query_cases)
        writer.write_json("confirmation.json", [record.confirmation for record in records if record.confirmation])
        writer.write_json("symmetry_violations.json", _violation_payload(records))
        writer.write_json("skip_reason_summary.json", summarize_skip_errors(records))
        write_review_candidates(writer.run_dir, update_summary=False)
        write_exploratory_leads(writer.run_dir, update_summary=False)
        cluster_summary = (
            write_clustered_summary(writer.run_dir, update_summary=False, clustering_config=config.clustering)
            if config.clustering.enabled
            else None
        )
        writer.write_json(
            "summary.json",
            _summary(
                run_id,
                statuses,
                records,
                no_ready=False,
                pmc_summary=run_outputs.get("pmc_summary"),
                feedback_summary=run_outputs.get("feedback_summary"),
                cluster_summary=cluster_summary,
            ),
        )
        return EXIT_OK
    finally:
        for adapter in adapters:
            adapter.close()


def run_gdsmith_cases(
    *,
    config_path: str | Path,
    systems: str = "auto",
    query_cases: tuple[QueryCase, ...],
    run_id: str | None = None,
    feedback_enabled: bool | None = None,
    max_rules_per_case: int | None = None,
    transform_family: str = "auto",
    env: Mapping[str, str] | None = None,
) -> int:
    env = dict(os.environ if env is None else env)
    config = load_run_config(config_path)
    config = _with_runtime_overrides(
        config,
        feedback_enabled=feedback_enabled,
        max_rules_per_case=max_rules_per_case,
        transform_family=transform_family,
    )
    selected_systems = _select_systems(config, systems)
    run_id = run_id or _default_run_id()
    writer = ArtifactWriter(config, run_id)
    writer.write_json("config_snapshot.json", config)

    adapters: list[GraphAdapter] = []
    statuses: list[SystemStatus] = []
    try:
        for system_config in selected_systems:
            adapter = create_adapter(system_config, config, env, allow_create=False)
            status = _preflight(adapter)
            statuses.append(status)
            if status.ready:
                adapters.append(adapter)
        writer.write_json("preflight.json", {"run_id": run_id, "systems": statuses})
        if not adapters:
            writer.write_json("summary.json", _summary(run_id, statuses, [], no_ready=True))
            return EXIT_NO_READY_SYSTEMS
        records, run_outputs = _run_query_cases(config, writer, adapters, query_cases)
        writer.write_json("symmetry_violations.json", _violation_payload(records))
        writer.write_json("skip_reason_summary.json", summarize_skip_errors(records))
        write_review_candidates(writer.run_dir, update_summary=False)
        write_exploratory_leads(writer.run_dir, update_summary=False)
        cluster_summary = (
            write_clustered_summary(writer.run_dir, update_summary=False, clustering_config=config.clustering)
            if config.clustering.enabled
            else None
        )
        writer.write_json(
            "summary.json",
            _summary(
                run_id,
                statuses,
                records,
                no_ready=False,
                pmc_summary=run_outputs.get("pmc_summary"),
                feedback_summary=run_outputs.get("feedback_summary"),
                cluster_summary=cluster_summary,
            ),
        )
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
) -> tuple[list[CaseRecord], dict[str, object]]:
    pmc_state = PMCCoverageState(novelty_weights=config.coverage.novelty_weights)
    feedback_state = initialize_feedback_state(config.transforms, config.feedback)
    scheduler = TransformScheduler(config.transforms, config.feedback, feedback_state)
    return _run_query_cases_with_state(config, writer, adapters, query_cases, pmc_state, feedback_state, scheduler)


def _new_runtime_state(config: RunConfig) -> tuple[PMCCoverageState, object, TransformScheduler]:
    pmc_state = PMCCoverageState(novelty_weights=config.coverage.novelty_weights)
    feedback_state = initialize_feedback_state(config.transforms, config.feedback)
    scheduler = TransformScheduler(config.transforms, config.feedback, feedback_state)
    return pmc_state, feedback_state, scheduler


def _run_query_cases_with_state(
    config: RunConfig,
    writer: ArtifactWriter,
    adapters: list[GraphAdapter],
    query_cases: tuple[QueryCase, ...],
    pmc_state: PMCCoverageState,
    feedback_state,
    scheduler: TransformScheduler,
) -> tuple[list[CaseRecord], dict[str, object]]:
    records: list[CaseRecord] = []
    pmc_evidences = []
    for adapter in adapters:
        system_name = adapter.target.value
        for query_case in query_cases:
            replayable = getattr(adapter, "gdsmith_query_is_replayable", None)
            if query_case.source == "gdsmith" and callable(replayable) and not replayable(query_case.query):
                family = QueryFamily(
                    case_id=query_case.case_id,
                    seed_query=query_case.query,
                    parameters=query_case.parameters,
                    variants=(),
                    source=query_case.source,
                    generator_case_id=query_case.generator_case_id,
                    raw_query=query_case.raw_query,
                )
                artifacts = writer.write_query_family(system_name, family)
                _write_transform_artifacts(writer, system_name, family, artifacts)
                record = _case_record(
                    adapter.target,
                    family,
                    (),
                    None,
                    None,
                    artifacts,
                    "skipped",
                    "GDSMITH_QUERY_NOT_REPLAYABLE_ON_TARGET",
                )
                writer.write_case_record(record)
                records.append(record)
                continue
            decision = scheduler.choose_for_case(query_case)
            case_transform_config = replace(
                config.transforms,
                enabled_families=(decision.family,),
                enabled_rules=decision.enabled_rules,
                max_variants_per_case=len(decision.enabled_rules) if decision.enabled_rules else config.transforms.max_variants_per_case,
            )
            family = generate_transform_family(
                query_case,
                case_transform_config,
                target=adapter.target,
                family_name=decision.family,
            )
            artifacts = writer.write_query_family(system_name, family)
            _write_transform_artifacts(writer, system_name, family, artifacts)
            record = _run_family(config, writer, adapter, family, artifacts)
            record, observed = _attach_pmc(writer, config, pmc_state, record)
            pmc_evidences.extend(observed)
            scheduler.update_from_case_record(record, record.pmc)
            writer.write_case_record(record)
            records.append(record)
    pmc_summary = pmc_state.snapshot(writer.run_id)
    if config.coverage.enabled:
        write_pmc_artifacts(writer.run_dir, pmc_summary, pmc_evidences)
    feedback_summary = summarize_feedback_state(feedback_state, config.feedback)
    writer.write_json("feedback_summary.json", feedback_summary)
    writer.write_json("rule_stats.json", tuple(feedback_state.rule_stats.values()))
    return records, {"pmc_summary": pmc_summary, "feedback_summary": feedback_summary}


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
    _write_profile_alias_artifacts(writer, base, profiles, equivalent_variants)

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
    exploratory_pmc = None
    if len(exploratory_comparable) >= 2:
        exploratory_oracle, exploratory_confirmation, exploratory_plan_diff, exploratory_fast, exploratory_slow = _oracle_from_profiles(
            config,
            writer,
            adapter,
            family,
            exploratory_comparable,
            artifacts,
            base,
            prefix="exploratory_",
        )
        exploratory_pmc = _build_pmc_evidence(
            config,
            family,
            exploratory_fast,
            exploratory_slow,
            exploratory_plan_diff,
            exploratory_oracle,
            exploratory_confirmation,
            safety_tier="exploratory",
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
            None,
            exploratory_pmc,
        )

    oracle, confirmation, plan_diff, fast, slow = _oracle_from_profiles(
        config,
        writer,
        adapter,
        family,
        strict_comparable,
        artifacts,
        base,
        prefix="",
    )
    pmc = _build_pmc_evidence(
        config,
        family,
        fast,
        slow,
        plan_diff,
        oracle,
        confirmation,
        safety_tier="strict",
    )
    state = _candidate_state(oracle, confirmation)
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
        pmc,
        exploratory_pmc,
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
) -> tuple[OracleRecord, ConfirmationRecord, PlanDiff, VariantProfileSummary, VariantProfileSummary]:
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
    return oracle, confirmation, plan_diff, fast, slow


def _candidate_state(oracle: OracleRecord, confirmation: ConfirmationRecord) -> str:
    if not oracle.is_candidate:
        return "equivalent_no_candidate"
    latency = oracle.evidence.get("latency", {}) if isinstance(oracle.evidence, Mapping) else {}
    latency_supported = bool(isinstance(latency, Mapping) and latency.get("worsened"))
    support_level = str(confirmation.support_level)
    if support_level == "profile_supported" and latency_supported:
        return "profile_and_latency_supported_candidate"
    if support_level == "profile_supported":
        return "profile_supported_candidate"
    if support_level == "latency_supported":
        return "latency_supported_candidate"
    return "plan_only_candidate"


def _write_profile_alias_artifacts(
    writer: ArtifactWriter,
    base: str,
    profiles: list[VariantProfileSummary],
    equivalent_variants: list[SymmetryVariant],
) -> None:
    profile_by_id = {profile.variant_id: profile for profile in profiles}
    base_profile = profile_by_id.get("seed")
    transformed_profile = None
    for variant in equivalent_variants:
        transformed_profile = profile_by_id.get(variant.variant_id)
        if transformed_profile is not None:
            break
    if base_profile is not None:
        writer.write_text(f"{base}/profile_base.txt", _render_profile_alias_text(base_profile, label="base"))
    if transformed_profile is not None:
        writer.write_text(
            f"{base}/profile_transformed.txt",
            _render_profile_alias_text(transformed_profile, label="transformed"),
        )


def _render_profile_alias_text(profile: VariantProfileSummary, *, label: str) -> str:
    lines = [
        f"# GraphSymPerf Replay Profile: {label}",
        "",
        f"- Query kind: {profile.query_kind}",
        f"- Available: {profile.available}",
        f"- Median elapsed ms: {profile.median_ms}",
        f"- Samples ms: {', '.join(str(item) for item in profile.samples_ms)}",
        "",
        "## Metrics",
        "",
    ]
    if profile.metrics:
        for key in sorted(profile.metrics):
            lines.append(f"- {key}: {profile.metrics[key]}")
    else:
        lines.append("- None")
    lines.extend(["", "## Plan Text", ""])
    if profile.plan is not None and getattr(profile.plan, "raw_text", ""):
        lines.append(str(profile.plan.raw_text))
    else:
        lines.append("No raw plan text available.")
    if profile.profile_plan is not None:
        lines.extend(
            [
                "",
                "## Normalized Profile",
                "",
                f"- Structural hash: {profile.profile_plan.structural_hash}",
                f"- Operator skeleton: {', '.join(profile.profile_plan.operator_skeleton)}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


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
        confirmed=oracle.is_candidate and latency_supported,
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


def _build_pmc_evidence(
    config: RunConfig,
    family: SymmetryFamily,
    fast: VariantProfileSummary,
    slow: VariantProfileSummary,
    plan_diff,
    oracle: OracleRecord,
    confirmation: ConfirmationRecord,
    *,
    safety_tier: str,
):
    if not config.coverage.enabled:
        return None
    variants = {variant.variant_id: variant for variant in family.variants}
    fast_variant = variants.get(fast.variant_id)
    slow_variant = variants.get(slow.variant_id)
    selected_variant = slow_variant if slow.variant_id != "seed" else fast_variant
    transform_rule = selected_variant.transform_rule if selected_variant is not None else "seed_pair"
    transform_family = _transform_family_name(selected_variant, family)
    return make_pmc_evidence(
        case_id=family.case_id,
        transform_family=transform_family,
        transform_rule=transform_rule,
        safety_tier=safety_tier,
        fast_variant_id=fast.variant_id,
        slow_variant_id=slow.variant_id,
        plan_diff=plan_diff,
        coverage_config=config.coverage,
        metadata={
            "is_candidate": bool(oracle.is_candidate),
            "support_level": confirmation.support_level,
            "candidate_types": tuple(oracle.candidate_types),
            "fast_transform_rule": fast_variant.transform_rule if fast_variant is not None else None,
            "slow_transform_rule": slow_variant.transform_rule if slow_variant is not None else None,
        },
    )


def _attach_pmc(
    writer: ArtifactWriter,
    config: RunConfig,
    pmc_state: PMCCoverageState,
    record: CaseRecord,
) -> tuple[CaseRecord, list]:
    if not config.coverage.enabled:
        return record, []
    base = f"{record.system.value}/{record.case_id}"
    artifacts = dict(record.artifacts)
    updated = record
    observed = []
    if record.pmc is not None:
        pmc = pmc_state.observe(record.pmc)
        observed.append(pmc)
        if config.coverage.write_per_case_evidence:
            path = writer.write_json(f"{base}/pmc_evidence.json", pmc)
            artifacts["pmc_evidence"] = str(path.relative_to(writer.run_dir))
        updated = replace(updated, pmc=pmc, artifacts=artifacts)
    if record.exploratory_pmc is not None:
        exploratory_pmc = pmc_state.observe(record.exploratory_pmc)
        observed.append(exploratory_pmc)
        if config.coverage.write_per_case_evidence:
            path = writer.write_json(f"{base}/exploratory_pmc_evidence.json", exploratory_pmc)
            artifacts["exploratory_pmc_evidence"] = str(path.relative_to(writer.run_dir))
        updated = replace(updated, exploratory_pmc=exploratory_pmc, artifacts=artifacts)
    return updated, observed


def _transform_family_name(selected_variant: SymmetryVariant | None, family: SymmetryFamily) -> str:
    if selected_variant is not None:
        family_name = selected_variant.metadata.get("transform_family")
        if family_name:
            return str(family_name)
        if "symmetry" in selected_variant.transform_rule:
            return "symmetry"
    if any("symmetry" in variant.transform_rule for variant in family.variants):
        return "symmetry"
    return "bool_redundancy"


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
    pmc=None,
    exploratory_pmc=None,
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
        pmc=pmc,
        exploratory_pmc=exploratory_pmc,
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


def _summary(
    run_id: str,
    statuses: list[SystemStatus],
    records: list[CaseRecord],
    *,
    no_ready: bool,
    pmc_summary: PMCSummary | None = None,
    feedback_summary: Mapping[str, object] | None = None,
    cluster_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    summary = {
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
        "runtime_confirmed": sum(1 for record in records if _latency_supported(record)),
        "profile_supported_candidates": sum(
            1
            for record in records
            if record.oracle
            and record.oracle.is_candidate
            and record.confirmation
            and record.confirmation.support_level == "profile_supported"
        ),
        "candidate_types": _candidate_type_counts(records),
        "transform_rules": _transform_rule_counts(records),
        "states": _state_counts(records),
        **summarize_skip_errors(records),
        "symmetry_violations_artifact": "symmetry_violations.json",
        "review_candidates_markdown": "review_candidates.md",
        "skip_reason_summary_artifact": "skip_reason_summary.json",
    }
    if pmc_summary is not None:
        summary.update(
            {
                "pmc_total_units_seen": pmc_summary.total_units_seen,
                "pmc_new_units": pmc_summary.total_new_units,
                "pmc_total_units_seen_all": pmc_summary.total_units_seen_all or pmc_summary.total_units_seen,
                "pmc_total_units_seen_core": pmc_summary.total_units_seen_core or pmc_summary.total_units_seen,
                "pmc_new_units_all": pmc_summary.total_new_units_all or pmc_summary.total_new_units,
                "pmc_new_units_core": pmc_summary.total_new_units_core or pmc_summary.total_new_units,
                "context_units": dict(pmc_summary.context_units),
                "pmc_units_by_kind": dict(pmc_summary.units_by_kind),
                "pmc_summary_artifact": "pmc_summary.json",
            }
        )
    if feedback_summary is not None:
        summary.update(
            {
                "feedback_enabled": bool(feedback_summary.get("enabled")),
                "feedback_summary_artifact": "feedback_summary.json",
                "rule_stats_artifact": "rule_stats.json",
            }
        )
        if bool(feedback_summary.get("enabled")):
            summary["feedback_top_rules"] = tuple(feedback_summary.get("top_rules") or ())
        else:
            summary["passive_top_rules"] = tuple(feedback_summary.get("top_rules") or ())
    if cluster_summary is not None:
        summary.update(
            {
                "issue_family_count": int(cluster_summary.get("issue_family_count") or 0),
                "unique_issue_count": int(cluster_summary.get("unique_issue_count") or 0),
                "duplicate_group_count": int(cluster_summary.get("duplicate_group_count") or 0),
                "clustered_summary_artifact": "clustered_summary.json",
            }
        )
        if bool(((cluster_summary.get("generation_metadata") or {}).get("write_signatures"))):
            summary["candidate_signatures_artifact"] = "candidate_signatures.jsonl"
    return summary


def _latency_supported(record: CaseRecord) -> bool:
    if record.oracle is None or not record.oracle.is_candidate:
        return False
    if record.confirmation is not None and record.confirmation.support_level == "latency_supported":
        return True
    latency = {}
    if isinstance(record.oracle.evidence, Mapping):
        latency = record.oracle.evidence.get("latency") or {}
    return bool(isinstance(latency, Mapping) and latency.get("worsened"))


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
        coverage=config.coverage,
        feedback=config.feedback,
        clustering=config.clustering,
        transforms=config.transforms,
    )


def _with_runtime_overrides(
    config: RunConfig,
    *,
    feedback_enabled: bool | None = None,
    max_rules_per_case: int | None = None,
    transform_family: str = "auto",
) -> RunConfig:
    transforms = config.transforms
    feedback = config.feedback
    if feedback_enabled is not None:
        feedback = replace(feedback, enabled=feedback_enabled)
    if max_rules_per_case is not None:
        transforms = replace(transforms, max_variants_per_case=max_rules_per_case)
        feedback = replace(feedback, max_rules_per_case=max_rules_per_case)
    if transform_family != "auto":
        if transform_family not in {"symmetry", "bool_redundancy"}:
            raise ConfigError("--transform-family must be auto, symmetry, or bool_redundancy")
        transforms = replace(transforms, enabled_families=(transform_family,))
    return replace(config, transforms=transforms, feedback=feedback)


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
