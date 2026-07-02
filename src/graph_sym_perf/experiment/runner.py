"""Experiment runner with shard/resume support."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
import tempfile
from typing import Any, Mapping
import yaml

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.app.gdsmith_mining import _write_gdsmith_artifacts
from graph_sym_perf.app.mining import (
    EXIT_CONFIG_ERROR,
    EXIT_NO_READY_SYSTEMS,
    EXIT_OK,
    _attach_pmc,
    _case_record,
    _new_runtime_state,
    _preflight,
    _run_family,
    _summary,
    _violation_payload,
    _write_transform_artifacts,
)
from graph_sym_perf.cluster import write_clustered_summary, write_exploratory_leads, write_review_candidates
from graph_sym_perf.config import environment_with_dotenv
from graph_sym_perf.core import CaseRecord, ConfigError, QueryCase, QueryFamily, SystemConfig, SystemStatus, TargetSystem
from graph_sym_perf.coverage import PMCCoverageState, PMCEvidence, write_pmc_artifacts
from graph_sym_perf.datasets import generate_mini_ldbc
from graph_sym_perf.feedback import TransformScheduler, initialize_feedback_state, summarize_feedback_state
from graph_sym_perf.gdsmith import check_gdsmith_tooling, query_cases_from_gdsmith, run_gdsmith
from graph_sym_perf.querygen import generate_query_cases
from graph_sym_perf.reporter import ArtifactWriter
from graph_sym_perf.transforms import generate_transform_family

from graph_sym_perf.experiment.manifest import load_experiment_manifest
from graph_sym_perf.experiment.resume import CheckpointState, append_checkpoint_event, load_checkpoint, save_checkpoint
from graph_sym_perf.experiment.shard import ShardSpec, stable_case_id
from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port

DESTRUCTIVE_REPLAY_ENV = "GRAPHSYMPERF_REPLAY_ALLOW_DESTRUCTIVE"


@dataclass(frozen=True)
class SourcePayload:
    query_cases: tuple[QueryCase, ...]
    graph_statements: tuple[str, ...] = ()
    generator_target: TargetSystem | None = None


@dataclass(frozen=True)
class CaseTask:
    system_config: SystemConfig
    local_index: int
    base_case: QueryCase
    case_id: str


def run_experiment(
    manifest_path: str | Path,
    *,
    shard: int | None = None,
    num_shards: int | None = None,
    systems: str | None = None,
    max_cases: int | None = None,
    feedback_enabled: bool | None = None,
    resume: bool | None = None,
) -> int:
    env = environment_with_dotenv(None)
    manifest = load_experiment_manifest(manifest_path)
    shard_spec = ShardSpec(
        index=int(shard if shard is not None else manifest.execution.shard_index or 0),
        total=int(num_shards if num_shards is not None else manifest.execution.shards),
    )
    run_config = manifest.to_run_config(shard_index=shard_spec.index, num_shards=shard_spec.total)
    run_config = _apply_run_overrides(
        run_config,
        systems=systems,
        max_cases=max_cases,
        feedback_enabled=feedback_enabled,
    )
    effective_resume = bool(manifest.execution.resume if resume is None else resume)
    writer = ArtifactWriter(run_config, shard_spec.run_id)
    writer.write_json("experiment_manifest.json", manifest)
    writer.write_json("run_config_snapshot.json", run_config)
    writer.write_json("shard_spec.json", {"index": shard_spec.index, "total": shard_spec.total, "run_id": shard_spec.run_id})

    selected_systems = run_config.systems
    adapters = []
    statuses: list[SystemStatus] = []
    try:
        for system_config in selected_systems:
            adapter = create_adapter(system_config, run_config, env, allow_create=bool(manifest.source.get("load")))
            status = _preflight(adapter)
            statuses.append(status)
            if status.ready:
                adapters.append(adapter)
        writer.write_json("preflight.json", {"systems": statuses})
        if not adapters:
            summary_payload = _summary(writer.run_id, statuses, [], no_ready=True)
            writer.write_json("summary.json", summary_payload)
            if manifest.source_modes == ("cases_jsonl",):
                _write_known_replay_summary_artifacts(writer, summary_payload, ())
            return EXIT_NO_READY_SYSTEMS
        if manifest.source_kind == "ldbc" and bool(manifest.source.get("load")):
            graph = generate_mini_ldbc(run_config.dataset)
            writer.write_json("synthetic_graph_spec.json", graph)
            for adapter in adapters:
                adapter.ensure_schema()
                adapter.load_mini_ldbc(graph)

        source_payload = _query_cases_from_source(manifest, run_config, writer, env)
        if "ldbc" in manifest.source_modes:
            graph = generate_mini_ldbc(run_config.dataset)
            writer.write_json("synthetic_graph_spec.json", graph)
            for adapter in adapters:
                adapter.ensure_schema()
                adapter.load_mini_ldbc(graph)
        if "gdsmith" in manifest.source_modes and source_payload.graph_statements:
            for adapter in adapters:
                if adapter.target is source_payload.generator_target:
                    continue
                loader = getattr(adapter, "load_gdsmith_graph", None)
                if callable(loader):
                    loader(source_payload.graph_statements)
        if max_cases is not None:
            source_payload = replace(
                source_payload,
                query_cases=source_payload.query_cases[: int(max_cases)],
            )
        shard_cases = shard_spec.slice_cases(source_payload.query_cases)
        checkpoint = load_checkpoint(writer.run_dir) if effective_resume else CheckpointState()
        pmc_state = checkpoint.pmc_state or PMCCoverageState(novelty_weights=run_config.coverage.novelty_weights)
        feedback_state = checkpoint.feedback_state or initialize_feedback_state(run_config.transforms, run_config.feedback)
        scheduler = TransformScheduler(run_config.transforms, run_config.feedback, feedback_state)
        ready_targets = {adapter.target for adapter in adapters}
        ready_systems = tuple(system for system in selected_systems if system.target in ready_targets)
        case_tasks = _build_case_tasks(manifest, shard_spec, shard_cases, ready_systems, checkpoint)
        worker_count = _effective_worker_count(manifest, case_tasks)
        scheduler_lock = Lock()

        def commit_result(task: CaseTask, record: CaseRecord) -> None:
            record_with_pmc, observed = _attach_pmc(writer, run_config, pmc_state, record)
            with scheduler_lock:
                scheduler.update_from_case_record(record_with_pmc, record_with_pmc.pmc)
            writer.write_case_record(record_with_pmc)
            _update_checkpoint(checkpoint, record_with_pmc, task.local_index, feedback_state, pmc_state, manifest, shard_spec)
            if task.local_index % max(1, manifest.execution.write_every_n_cases) == 0:
                save_checkpoint(writer.run_dir, checkpoint)
                append_checkpoint_event(
                    writer.run_dir,
                    {
                        "case_id": record_with_pmc.case_id,
                        "state": record_with_pmc.state,
                        "local_index": task.local_index,
                        "new_pmc_units": sum(item.new_unit_count for item in observed if isinstance(item, PMCEvidence)),
                    },
                )
            if manifest.execution.fail_fast and record_with_pmc.state == "error":
                save_checkpoint(writer.run_dir, checkpoint)
                raise RuntimeError(f"fail-fast triggered by {record_with_pmc.case_id}")

        if worker_count == 1:
            for task in case_tasks:
                record = _execute_case_task(
                    task,
                    run_config=run_config,
                    writer=writer,
                    env=env,
                    manifest=manifest,
                    scheduler=scheduler,
                    scheduler_lock=scheduler_lock,
                )
                commit_result(task, record)
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="graphsymperf-worker") as executor:
                futures = {
                    executor.submit(
                        _execute_case_task,
                        task,
                        run_config=run_config,
                        writer=writer,
                        env=env,
                        manifest=manifest,
                        scheduler=scheduler,
                        scheduler_lock=scheduler_lock,
                    ): task
                    for task in case_tasks
                }
                for future in as_completed(futures):
                    task = futures[future]
                    commit_result(task, future.result())

        save_checkpoint(writer.run_dir, checkpoint)
        records = _load_case_records(writer.run_dir / "cases.jsonl")
        pmc_summary = pmc_state.snapshot(writer.run_id)
        if run_config.coverage.enabled:
            write_pmc_artifacts(writer.run_dir, pmc_summary, _load_pmc_evidences(writer.run_dir))
        feedback_summary = summarize_feedback_state(feedback_state, run_config.feedback)
        writer.write_json("feedback_summary.json", feedback_summary)
        writer.write_json("rule_stats.json", tuple(feedback_state.rule_stats.values()))
        writer.write_json("symmetry_violations.json", _violation_payload(records))
        write_review_candidates(writer.run_dir, update_summary=False)
        write_exploratory_leads(writer.run_dir, update_summary=False)
        cluster_summary = (
            write_clustered_summary(writer.run_dir, update_summary=False, clustering_config=run_config.clustering)
            if run_config.clustering.enabled
            else None
        )
        summary_payload = _summary(
            writer.run_id,
            statuses,
            records,
            no_ready=False,
            pmc_summary=pmc_summary,
            feedback_summary=feedback_summary,
            cluster_summary=cluster_summary,
        ) | {
            "experiment_id": manifest.experiment_id,
            "shard_index": shard_spec.index,
            "num_shards": shard_spec.total,
            "cases_assigned": len(shard_cases),
            "system_case_tasks": len(case_tasks),
            "resume_enabled": effective_resume,
            "execution_mode": "serial" if worker_count == 1 else "parallel",
            "workers_per_shard": worker_count,
        }
        writer.write_json("summary.json", summary_payload)
        if manifest.source_modes == ("cases_jsonl",):
            _write_known_replay_summary_artifacts(writer, summary_payload, records)
        return EXIT_OK
    except ConfigError:
        return EXIT_CONFIG_ERROR
    finally:
        for adapter in adapters:
            adapter.close()


def _build_case_tasks(
    manifest,
    shard_spec: ShardSpec,
    shard_cases: tuple[tuple[int, QueryCase], ...],
    ready_systems: tuple[SystemConfig, ...],
    checkpoint: CheckpointState,
) -> tuple[CaseTask, ...]:
    tasks: list[CaseTask] = []
    for system_config in ready_systems:
        system_name = system_config.target.value
        for local_index, base_case in shard_cases:
            if base_case.target_system and base_case.target_system != system_name:
                continue
            case_id = stable_case_id(manifest.experiment_id, system_config.target, shard_spec, local_index, manifest.seed)
            if case_id in checkpoint.completed_case_ids:
                continue
            tasks.append(CaseTask(system_config=system_config, local_index=local_index, base_case=base_case, case_id=case_id))
    return tuple(tasks)


def _effective_worker_count(manifest, tasks: tuple[CaseTask, ...]) -> int:
    requested = max(1, int(manifest.execution.workers_per_shard))
    if requested == 1:
        return 1
    if any(task.base_case.setup_path for task in tasks):
        return 1
    return min(requested, max(1, len(tasks)))


def _execute_case_task(
    task: CaseTask,
    *,
    run_config,
    writer: ArtifactWriter,
    env: Mapping[str, str],
    manifest,
    scheduler: TransformScheduler,
    scheduler_lock: Lock,
) -> CaseRecord:
    adapter = create_adapter(task.system_config, run_config, env, allow_create=False)
    try:
        system_case = replace(task.base_case, case_id=task.case_id)
        system_name = adapter.target.value
        if system_case.setup_path:
            _prepare_known_issue_setup(adapter, manifest, system_case)
        replayable = getattr(adapter, "gdsmith_query_is_replayable", None)
        if system_case.source == "gdsmith" and callable(replayable) and not replayable(system_case.query):
            family = QueryFamily(
                case_id=system_case.case_id,
                seed_query=system_case.query,
                parameters=system_case.parameters,
                variants=(),
                source=system_case.source,
                generator_case_id=system_case.generator_case_id,
                raw_query=system_case.raw_query,
            )
            artifacts = writer.write_query_family(system_name, family)
            _write_transform_artifacts(writer, system_name, family, artifacts)
            return _annotate_record_with_case_metadata(
                _case_record(
                    adapter.target,
                    family,
                    (),
                    None,
                    None,
                    artifacts,
                    "skipped",
                    "GDSMITH_QUERY_NOT_REPLAYABLE_ON_TARGET",
                ),
                system_case,
            )
        with scheduler_lock:
            decision = scheduler.choose_for_case(system_case)
        transform_config = replace(
            run_config.transforms,
            enabled_families=(decision.family,),
            enabled_rules=decision.enabled_rules,
            max_variants_per_case=(
                len(decision.enabled_rules) if decision.enabled_rules else run_config.transforms.max_variants_per_case
            ),
        )
        family = generate_transform_family(
            system_case,
            transform_config,
            target=adapter.target,
            family_name=decision.family,
        )
        artifacts = writer.write_query_family(system_name, family)
        _write_transform_artifacts(writer, system_name, family, artifacts)
        record = _run_family(run_config, writer, adapter, family, artifacts)
        return _annotate_record_with_case_metadata(record, system_case)
    finally:
        adapter.close()


def _apply_run_overrides(
    run_config,
    *,
    systems: str | None,
    max_cases: int | None,
    feedback_enabled: bool | None,
):
    updated = run_config
    if systems:
        try:
            requested = tuple(TargetSystem.parse(item.strip()) for item in systems.split(",") if item.strip())
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if not requested:
            raise ConfigError("--systems must contain at least one target")
        by_target = {system.target: system for system in updated.systems}
        missing = tuple(target.value for target in requested if target not in by_target)
        if missing:
            raise ConfigError(f"--systems references targets not present in manifest: {', '.join(missing)}")
        updated = replace(updated, systems=tuple(by_target[target] for target in requested))
    if max_cases is not None:
        if max_cases <= 0:
            raise ConfigError("--max-cases must be greater than 0")
        updated = replace(
            updated,
            query_generation=replace(
                updated.query_generation,
                max_cases=max_cases,
                gdsmith=replace(updated.query_generation.gdsmith, num_queries=max_cases),
            ),
        )
    if feedback_enabled is not None:
        updated = replace(updated, feedback=replace(updated.feedback, enabled=feedback_enabled))
    return updated


def _query_cases_from_source(manifest, run_config, writer: ArtifactWriter, env: Mapping[str, str]) -> SourcePayload:
    modes = manifest.source_modes
    if modes == ("ldbc",):
        return SourcePayload(query_cases=generate_query_cases(run_config.dataset, _ldbc_query_generation(run_config, manifest)))
    if modes == ("cases_jsonl",):
        source_path = _resolve_source_path(manifest, str(manifest.source.get("cases_path") or ""))
        return SourcePayload(query_cases=_load_query_cases_jsonl(source_path))
    payloads: list[QueryCase] = []
    graph_statements: tuple[str, ...] = ()
    generator: TargetSystem | None = None
    if "gdsmith" in modes:
        generator = TargetSystem.parse(str(manifest.source.get("generator_target", "neo4j")))
        gdsmith_payload = _gdsmith_source_payload(manifest, run_config, writer, env, generator)
        payloads.extend(gdsmith_payload.query_cases)
        graph_statements = gdsmith_payload.graph_statements
    if "ldbc" in modes:
        payloads.extend(generate_query_cases(run_config.dataset, _ldbc_query_generation(run_config, manifest)))
    if not payloads:
        raise ConfigError(f"unsupported source.kind/source.enabled: {modes}")
    return SourcePayload(
        query_cases=tuple(payloads),
        graph_statements=graph_statements,
        generator_target=generator,
    )


def _gdsmith_source_payload(
    manifest,
    run_config,
    writer: ArtifactWriter,
    env: Mapping[str, str],
    generator: TargetSystem,
) -> SourcePayload:
    gdsmith_status = check_gdsmith_tooling(run_config.query_generation.gdsmith, env)
    writer.write_json("gdsmith_tool_status.json", gdsmith_status)
    if not gdsmith_status.ready:
        raise ConfigError("GDsmith tooling is not ready; see gdsmith_tool_status.json")
    backend = _gdsmith_backend(generator)
    host, port, username, password = _gdsmith_connection_fields(generator, env)
    result = run_gdsmith(
        config=run_config.query_generation.gdsmith,
        env=env,
        raw_dir=writer.run_dir / "gdsmith_raw",
        backend=backend,
        host=host,
        port=port,
        username=username,
        password=password,
        num_queries=run_config.query_generation.max_cases,
        seed=manifest.seed,
    )
    _write_gdsmith_artifacts(writer, result)
    return SourcePayload(
        query_cases=query_cases_from_gdsmith(result.parsed.query_records),
        graph_statements=tuple(result.parsed.graph_statements),
        generator_target=generator,
    )


def _ldbc_query_generation(run_config, manifest) -> object:
    ldbc_mapping = manifest.source.get("ldbc_templates") or {}
    if not isinstance(ldbc_mapping, Mapping):
        ldbc_mapping = {}
    max_cases = int(ldbc_mapping.get("max_seed_queries_per_shard", run_config.query_generation.max_cases))
    result_limit = int(ldbc_mapping.get("result_limit", run_config.query_generation.result_limit))
    return replace(run_config.query_generation, max_cases=max_cases, result_limit=result_limit)


def _resolve_source_path(manifest, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if path.exists():
        return path.resolve()
    manifest_path = Path(str(manifest.metadata.get("manifest_path") or "."))
    return manifest_path.parent / path


def _load_query_cases_jsonl(path: Path) -> tuple[QueryCase, ...]:
    if not path.exists():
        raise ConfigError(f"cases_jsonl source does not exist: {path}")
    cases: list[QueryCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        try:
            cases.append(
                QueryCase(
                    case_id=str(raw["case_id"]),
                    template_name=str(raw.get("template_name", "replay_case")),
                    query=str(raw["query"]),
                    parameters=raw.get("parameters") or {},
                    base_predicate=str(raw.get("base_predicate", "")),
                    safe_expression=str(raw.get("safe_expression", "")),
                    source=str(raw.get("source", "cases_jsonl")),
                    generator_case_id=raw.get("generator_case_id"),
                    raw_query=raw.get("raw_query"),
                    skip_reason=raw.get("skip_reason"),
                    transform_family=(str(raw["transform_family"]) if raw.get("transform_family") is not None else None),
                    transform_rule=(str(raw["transform_rule"]) if raw.get("transform_rule") is not None else None),
                    target_system=(str(raw["target_system"]) if raw.get("target_system") is not None else None),
                    setup_path=(str(raw["setup_path"]) if raw.get("setup_path") is not None else None),
                    setup_attachment_url=(str(raw["setup_attachment_url"]) if raw.get("setup_attachment_url") is not None else None),
                    setup_attachment_name=(str(raw["setup_attachment_name"]) if raw.get("setup_attachment_name") is not None else None),
                    expected_nodes=(int(raw["expected_nodes"]) if raw.get("expected_nodes") is not None else None),
                    expected_relationships=(
                        int(raw["expected_relationships"]) if raw.get("expected_relationships") is not None else None
                    ),
                    developer_status=(str(raw["developer_status"]) if raw.get("developer_status") is not None else None),
                    issue_url=(str(raw["issue_url"]) if raw.get("issue_url") is not None else None),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"invalid cases_jsonl record at {path}:{line_number}: missing {exc}") from exc
    return tuple(cases)


def load_known_replay_cases(
    manifest_path: str | Path,
    *,
    case_ids: tuple[str, ...] = (),
) -> tuple[QueryCase, ...]:
    manifest = load_experiment_manifest(manifest_path)
    if "cases_jsonl" not in manifest.source_modes:
        raise ConfigError("replay manifest must use source.kind = cases_jsonl")
    source_path = _resolve_source_path(manifest, str(manifest.source.get("cases_path") or ""))
    cases = _load_query_cases_jsonl(source_path)
    if not case_ids:
        return cases
    wanted = set(case_ids)
    return tuple(case for case in cases if case.case_id in wanted)


def run_known_replay(
    manifest_path: str | Path,
    *,
    case_ids: tuple[str, ...] = (),
    dry_run: bool = False,
) -> tuple[int, Mapping[str, Any]]:
    cases = load_known_replay_cases(manifest_path, case_ids=case_ids)
    payload = {
        "manifest": str(Path(manifest_path)),
        "case_count": len(cases),
        "case_ids": [case.case_id for case in cases],
        "dry_run": dry_run,
    }
    if dry_run:
        return 0, payload

    manifest = load_experiment_manifest(manifest_path)
    source_path = _resolve_source_path(manifest, str(manifest.source.get("cases_path") or ""))
    with tempfile.TemporaryDirectory(prefix="graphsymperf-replay-") as tmp_dir:
        tmp_cases = Path(tmp_dir) / source_path.name
        tmp_cases.write_text(
            "".join(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "template_name": case.template_name,
                        "query": case.query,
                        "parameters": case.parameters,
                        "base_predicate": case.base_predicate,
                        "safe_expression": case.safe_expression,
                        "source": case.source,
                        "generator_case_id": case.generator_case_id,
                        "raw_query": case.raw_query,
                        "skip_reason": case.skip_reason,
                        "transform_family": case.transform_family,
                        "transform_rule": case.transform_rule,
                        "target_system": case.target_system,
                        "setup_path": case.setup_path,
                        "setup_attachment_url": case.setup_attachment_url,
                        "setup_attachment_name": case.setup_attachment_name,
                        "expected_nodes": case.expected_nodes,
                        "expected_relationships": case.expected_relationships,
                        "developer_status": case.developer_status,
                        "issue_url": case.issue_url,
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for case in cases
            ),
            encoding="utf-8",
        )
        patched = dict(manifest.source)
        patched["cases_path"] = str(tmp_cases)
        temp_manifest = Path(tmp_dir) / "replay_manifest.yaml"
        temp_manifest.write_text(
            yaml.safe_dump(
                {
                    "experiment_id": manifest.experiment_id,
                    "description": manifest.description,
                    "seed": manifest.seed,
                    "output_root": str(manifest.output_root),
                    "systems": {"targets": [system.target.value for system in manifest.systems]},
                    "source": patched,
                    "transforms": dict(manifest.transforms),
                    "coverage": {
                        "enabled": manifest.coverage.enabled,
                        "include_transformation_context": manifest.coverage.include_transformation_context,
                        "write_per_case_evidence": manifest.coverage.write_per_case_evidence,
                        "ratio_buckets": list(manifest.coverage.ratio_buckets),
                        "novelty_weights": dict(manifest.coverage.novelty_weights),
                    },
                    "feedback": {
                        "enabled": manifest.feedback.enabled,
                        "exploration_rate": manifest.feedback.exploration_rate,
                        "seed": manifest.feedback.seed,
                        "min_rules_per_case": manifest.feedback.min_rules_per_case,
                        "max_rules_per_case": manifest.feedback.max_rules_per_case,
                        "reward_weights": dict(manifest.feedback.reward_weights),
                    },
                    "oracle": dict(manifest.oracle),
                    "clustering": {
                        "enabled": manifest.clustering.enabled,
                        "write_signatures": manifest.clustering.write_signatures,
                        "representative_policy": manifest.clustering.representative_policy,
                        "include_plan_only": manifest.clustering.include_plan_only,
                        "include_exploratory": manifest.clustering.include_exploratory,
                    },
                    "reducer": dict(manifest.reducer),
                    "execution": {
                        "shards": manifest.execution.shards,
                        "workers_per_shard": manifest.execution.workers_per_shard,
                        "cases_per_shard": manifest.execution.cases_per_shard,
                        "per_query_timeout_seconds": manifest.execution.per_query_timeout_seconds,
                        "per_case_timeout_seconds": manifest.execution.per_case_timeout_seconds,
                        "resume": manifest.execution.resume,
                        "fail_fast": manifest.execution.fail_fast,
                        "isolate_system_per_worker": manifest.execution.isolate_system_per_worker,
                        "write_every_n_cases": manifest.execution.write_every_n_cases,
                    },
                    "artifacts": dict(manifest.artifacts),
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        exit_code = run_experiment(temp_manifest, shard=0, num_shards=1)
    return exit_code, payload


def _annotate_record_with_case_metadata(record, query_case: QueryCase):
    confirmation = record.confirmation
    if confirmation is not None and query_case.developer_status:
        confirmation = replace(confirmation, developer_status=query_case.developer_status)
    artifacts = dict(record.artifacts)
    for field_name in (
        "setup_path",
        "setup_attachment_url",
        "setup_attachment_name",
        "issue_url",
    ):
        value = getattr(query_case, field_name, None)
        if value is not None:
            artifacts[field_name] = str(value)
    for field_name in ("expected_nodes", "expected_relationships"):
        value = getattr(query_case, field_name, None)
        if value is not None:
            artifacts[field_name] = str(value)
    return replace(
        record,
        artifacts=artifacts,
        developer_status=(query_case.developer_status or record.developer_status),
        confirmation=confirmation,
    )


def _prepare_known_issue_setup(adapter, manifest, query_case: QueryCase) -> None:
    _require_destructive_replay_guard()
    setup_path = _resolve_source_path(manifest, query_case.setup_path or "")
    if not setup_path.exists():
        raise ConfigError(f"known issue setup does not exist: {setup_path}")
    _reset_adapter_for_known_issue_setup(adapter)
    for statement in _split_setup_statements(setup_path.read_text(encoding="utf-8")):
        adapter.execute(
            statement,
            {},
            "known_issue_setup",
            timeout_seconds=float(manifest.execution.per_query_timeout_seconds),
        )
    if adapter.target.value == "neo4j":
        adapter.execute(
            "CALL db.awaitIndexes()",
            {},
            "known_issue_setup_await_indexes",
            timeout_seconds=float(manifest.execution.per_query_timeout_seconds),
        )
    _verify_loaded_graph_counts(adapter, query_case)


def _require_destructive_replay_guard() -> None:
    if os.environ.get(DESTRUCTIVE_REPLAY_ENV) != "1":
        raise ConfigError(
            f"known issue replay resets target data; set {DESTRUCTIVE_REPLAY_ENV}=1 only for disposable databases"
        )


def _split_setup_statements(text: str) -> tuple[str, ...]:
    statements: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        current.append(stripped)
        if stripped.endswith(";"):
            statement = " ".join(current).rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    if current:
        statement = " ".join(current).strip()
        if statement:
            statements.append(statement)
    return tuple(statements)


def _reset_adapter_for_known_issue_setup(adapter) -> None:
    target = adapter.target.value
    if target == "neo4j":
        adapter._connect()
        with adapter._session() as session:
            indexes = list(session.run("SHOW INDEXES YIELD name, type RETURN name, type"))
            for record in indexes:
                if str(record["type"]).upper() == "LOOKUP":
                    continue
                name = str(record["name"]).replace("`", "``")
                session.run(f"DROP INDEX `{name}` IF EXISTS").consume()
            session.run("MATCH (n) DETACH DELETE n").consume()
        return
    if target == "memgraph":
        adapter.execute("MATCH (n) DETACH DELETE n", {}, "known_issue_reset")
        return
    if target == "falkordb":
        adapter._connect()
        graph_name = adapter._graph.name
        adapter._graph.delete()
        adapter._graph = adapter._db.select_graph(graph_name)
        return
    if target == "arcadedb":
        adapter._connect()
        response = adapter._request(
            "POST",
            "/api/v1/server",
            json={"command": f"drop database {adapter._database}"},
        )
        if response.status_code not in (200, 201, 204, 404, 409):
            raise ConfigError(f"cannot drop ArcadeDB database {adapter._database!r}: {response.text}")
        adapter.ensure_schema()
        return
    if target == "apache_age":
        adapter._connect()
        with adapter._conn.cursor() as cursor:
            adapter._prepare_session(cursor)
            cursor.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (adapter._graph_name,))
            exists = int(cursor.fetchone()[0]) > 0
            if exists:
                cursor.execute("SELECT * FROM ag_catalog.drop_graph(%s, true)", (adapter._graph_name,))
        adapter._conn.commit()
        adapter.ensure_schema()
        return
    raise ConfigError(f"known issue replay setup reset is not implemented for target: {target}")


def _verify_loaded_graph_counts(adapter, query_case: QueryCase) -> None:
    if query_case.expected_nodes is not None:
        rows = adapter.execute("MATCH (n) RETURN count(n) AS nodes", {}, "known_issue_setup_count_nodes").rows
        actual_nodes = int((rows[0] or {}).get("nodes", 0)) if rows else 0
        if actual_nodes != query_case.expected_nodes:
            raise ConfigError(
                f"known issue setup node count mismatch for {query_case.case_id}: "
                f"expected {query_case.expected_nodes}, got {actual_nodes}"
            )
    if query_case.expected_relationships is not None:
        rows = adapter.execute(
            "MATCH ()-[r]->() RETURN count(r) AS relationships",
            {},
            "known_issue_setup_count_relationships",
        ).rows
        actual_relationships = int((rows[0] or {}).get("relationships", 0)) if rows else 0
        if actual_relationships != query_case.expected_relationships:
            raise ConfigError(
                f"known issue setup relationship count mismatch for {query_case.case_id}: "
                f"expected {query_case.expected_relationships}, got {actual_relationships}"
            )


def _write_known_replay_summary_artifacts(
    writer: ArtifactWriter,
    summary_payload: Mapping[str, Any],
    records: tuple[CaseRecord, ...] | list[CaseRecord],
) -> None:
    replay_summary = {
        "run_id": writer.run_id,
        "summary": dict(summary_payload),
        "cases": [_known_replay_case_payload(record) for record in records],
    }
    writer.write_json("replay_summary.json", replay_summary)
    writer.write_text("replay_summary.md", _render_known_replay_summary_markdown(replay_summary))


def _known_replay_case_payload(record: CaseRecord) -> Mapping[str, Any]:
    return {
        "system": record.system.value,
        "case_id": record.case_id,
        "state": record.state,
        "reason": record.reason,
        "developer_status": str(record.developer_status),
        "equivalence_passed": bool(record.equivalences) and all(item.passed for item in record.equivalences),
        "equivalence_count": len(record.equivalences),
        "is_candidate": bool(record.oracle.is_candidate) if record.oracle is not None else False,
        "support_level": (
            str(record.confirmation.support_level)
            if record.confirmation is not None and record.confirmation.support_level is not None
            else None
        ),
        "artifacts": dict(record.artifacts),
    }


def _render_known_replay_summary_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload.get("summary") or {}
    cases = tuple(payload.get("cases") or ())
    lines = [
        f"# Known Issue Replay Summary: {payload.get('run_id')}",
        "",
        f"- Cases assigned: {summary.get('cases_assigned', 0)}",
        f"- Equivalence passed: {summary.get('equivalence_passed', 0)}",
        f"- Runtime confirmed: {summary.get('runtime_confirmed', 0)}",
        f"- Profile supported candidates: {summary.get('profile_supported_candidates', 0)}",
        f"- Unique issue count: {summary.get('unique_issue_count', 0)}",
        "",
        "| Case | System | State | Developer Status | Equivalence | Candidate | Support | Reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in cases:
        lines.append(
            "| {case_id} | {system} | {state} | {developer_status} | {equivalence} | {candidate} | {support} | {reason} |".format(
                case_id=item.get("case_id"),
                system=item.get("system"),
                state=item.get("state"),
                developer_status=item.get("developer_status"),
                equivalence=item.get("equivalence_passed"),
                candidate=item.get("is_candidate"),
                support=item.get("support_level"),
                reason=item.get("reason"),
            )
        )
    return "\n".join(lines) + "\n"


def _update_checkpoint(checkpoint: CheckpointState, record, local_index: int, feedback_state, pmc_state, manifest, shard_spec) -> None:
    if record.state == "error":
        checkpoint.failed_case_ids.add(record.case_id)
    elif record.state == "skipped":
        checkpoint.skipped_case_ids.add(record.case_id)
    else:
        checkpoint.completed_case_ids.add(record.case_id)
    checkpoint.last_seed_index = local_index
    checkpoint.feedback_state = feedback_state
    checkpoint.pmc_state = pmc_state
    checkpoint.deterministic_seed_info = {
        "experiment_seed": manifest.seed,
        "shard_index": shard_spec.index,
        "num_shards": shard_spec.total,
        "derived_seed": manifest.seed + shard_spec.index,
    }


def _load_case_records(path: Path):
    from graph_sym_perf.cluster.candidates import _case_record_from_mapping  # type: ignore[attr-defined]

    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(_case_record_from_mapping(json.loads(line)))
    return records


def _load_pmc_evidences(run_dir: Path) -> list[PMCEvidence]:
    path = run_dir / "pmc_evidence.jsonl"
    if not path.exists():
        return []
    from graph_sym_perf.experiment.aggregate import _pmc_from_mapping

    evidences: list[PMCEvidence] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            evidences.append(_pmc_from_mapping(json.loads(line)))
    return evidences


def _gdsmith_backend(target: TargetSystem) -> str:
    mapping = {
        TargetSystem.NEO4J: "neo4j",
        TargetSystem.MEMGRAPH: "memgraph",
        TargetSystem.FALKORDB: "redisgraph",
        TargetSystem.ARCADEDB: "arcadedb",
        TargetSystem.AGENSGRAPH: "agensgraph",
    }
    backend = mapping.get(target)
    if backend is None:
        raise ConfigError(f"no direct GDsmith backend for {target.value}")
    return backend


def _gdsmith_connection_fields(target: TargetSystem, env: Mapping[str, str]) -> tuple[str, int, str, str]:
    if target is TargetSystem.NEO4J:
        return neo4j_uri_to_host_port(env["GRAPHSYMPERF_NEO4J_URI"]) + (
            env["GRAPHSYMPERF_NEO4J_USER"],
            env["GRAPHSYMPERF_NEO4J_PASSWORD"],
        )
    if target is TargetSystem.MEMGRAPH:
        return (
            env["GRAPHSYMPERF_MEMGRAPH_HOST"],
            int(env["GRAPHSYMPERF_MEMGRAPH_PORT"]),
            env.get("GRAPHSYMPERF_MEMGRAPH_USER", "sqlancer"),
            env.get("GRAPHSYMPERF_MEMGRAPH_PASSWORD", "sqlancer"),
        )
    if target is TargetSystem.FALKORDB:
        return (
            env["GRAPHSYMPERF_FALKORDB_HOST"],
            int(env["GRAPHSYMPERF_FALKORDB_PORT"]),
            env.get("GRAPHSYMPERF_FALKORDB_USER", "neo4j"),
            env.get("GRAPHSYMPERF_FALKORDB_PASSWORD", "sqlancer"),
        )
    raise ConfigError(f"GDsmith connection fields for {target.value} are not configured")
