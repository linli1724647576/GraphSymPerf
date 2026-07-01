"""Experiment runner with shard/resume support."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
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
from graph_sym_perf.core import ConfigError, QueryCase, QueryFamily, SystemStatus, TargetSystem
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


@dataclass(frozen=True)
class SourcePayload:
    query_cases: tuple[QueryCase, ...]
    graph_statements: tuple[str, ...] = ()
    generator_target: TargetSystem | None = None


def run_experiment(
    manifest_path: str | Path,
    *,
    shard: int | None = None,
    num_shards: int | None = None,
) -> int:
    env = environment_with_dotenv(None)
    manifest = load_experiment_manifest(manifest_path)
    shard_spec = ShardSpec(
        index=int(shard if shard is not None else manifest.execution.shard_index or 0),
        total=int(num_shards if num_shards is not None else manifest.execution.shards),
    )
    run_config = manifest.to_run_config(shard_index=shard_spec.index, num_shards=shard_spec.total)
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
            writer.write_json("summary.json", _summary(writer.run_id, statuses, [], no_ready=True))
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
        shard_cases = shard_spec.slice_cases(source_payload.query_cases)
        checkpoint = load_checkpoint(writer.run_dir) if manifest.execution.resume else CheckpointState()
        pmc_state = checkpoint.pmc_state or PMCCoverageState(novelty_weights=run_config.coverage.novelty_weights)
        feedback_state = checkpoint.feedback_state or initialize_feedback_state(run_config.transforms, run_config.feedback)
        scheduler = TransformScheduler(run_config.transforms, run_config.feedback, feedback_state)

        for adapter in adapters:
            system_name = adapter.target.value
            for local_index, base_case in shard_cases:
                system_case = replace(
                    base_case,
                    case_id=stable_case_id(manifest.experiment_id, adapter.target, shard_spec, local_index, manifest.seed),
                )
                if system_case.case_id in checkpoint.completed_case_ids:
                    continue
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
                    _update_checkpoint(checkpoint, record, local_index, feedback_state, pmc_state, manifest, shard_spec)
                    continue
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
                record, observed = _attach_pmc(writer, run_config, pmc_state, record)
                scheduler.update_from_case_record(record, record.pmc)
                writer.write_case_record(record)
                _update_checkpoint(checkpoint, record, local_index, feedback_state, pmc_state, manifest, shard_spec)
                if local_index % max(1, manifest.execution.write_every_n_cases) == 0:
                    save_checkpoint(writer.run_dir, checkpoint)
                    append_checkpoint_event(
                        writer.run_dir,
                        {
                            "case_id": record.case_id,
                            "state": record.state,
                            "local_index": local_index,
                            "new_pmc_units": sum(item.new_unit_count for item in observed if isinstance(item, PMCEvidence)),
                        },
                    )
                if manifest.execution.fail_fast and record.state == "error":
                    save_checkpoint(writer.run_dir, checkpoint)
                    raise RuntimeError(f"fail-fast triggered by {record.case_id}")

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
        writer.write_json(
            "summary.json",
            _summary(
                writer.run_id,
                statuses,
                records,
                no_ready=False,
                pmc_summary=pmc_summary,
                feedback_summary=feedback_summary,
                cluster_summary=cluster_summary,
            )
            | {
                "experiment_id": manifest.experiment_id,
                "shard_index": shard_spec.index,
                "num_shards": shard_spec.total,
                "cases_assigned": len(shard_cases),
                "resume_enabled": manifest.execution.resume,
                "execution_mode": "serial",
            },
        )
        return EXIT_OK
    except ConfigError:
        return EXIT_CONFIG_ERROR
    finally:
        for adapter in adapters:
            adapter.close()


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
