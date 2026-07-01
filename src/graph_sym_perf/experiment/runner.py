"""Experiment runner with shard/resume support."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.app.gdsmith_mining import _write_gdsmith_artifacts
from graph_sym_perf.app.mining import (
    EXIT_CONFIG_ERROR,
    EXIT_NO_READY_SYSTEMS,
    EXIT_OK,
    _attach_pmc,
    _preflight,
    _run_family,
    _summary,
    _violation_payload,
    _write_transform_artifacts,
)
from graph_sym_perf.cluster import write_clustered_summary, write_exploratory_leads, write_review_candidates
from graph_sym_perf.config import environment_with_dotenv
from graph_sym_perf.core import ConfigError, QueryCase, SystemStatus
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


def run_experiment(
    manifest_path: str | Path,
    *,
    shard: int | None = None,
    num_shards: int | None = None,
) -> int:
    env = environment_with_dotenv()
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

        query_cases = _query_cases_from_source(manifest, run_config, writer, env)
        shard_cases = shard_spec.slice_cases(query_cases)
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


def _query_cases_from_source(manifest, run_config, writer: ArtifactWriter, env: Mapping[str, str]) -> tuple[QueryCase, ...]:
    kind = manifest.source_kind
    if kind == "ldbc":
        return generate_query_cases(run_config.dataset, run_config.query_generation)
    if kind == "cases_jsonl":
        source_path = _resolve_source_path(manifest, str(manifest.source.get("cases_path") or ""))
        return _load_query_cases_jsonl(source_path)
    if kind != "gdsmith":
        raise ConfigError(f"unsupported source.kind: {kind}")
    if any(system.target.value != "neo4j" for system in run_config.systems):
        raise ConfigError("GDsmith experiment manifests currently support only neo4j targets")
    gdsmith_status = check_gdsmith_tooling(run_config.query_generation.gdsmith, env)
    writer.write_json("gdsmith_tool_status.json", gdsmith_status)
    if not gdsmith_status.ready:
        raise ConfigError("GDsmith tooling is not ready; see gdsmith_tool_status.json")
    result = run_gdsmith(
        config=run_config.query_generation.gdsmith,
        env=env,
        raw_dir=writer.run_dir / "gdsmith_raw",
        neo4j_uri=env["GRAPHSYMPERF_NEO4J_URI"],
        username=env["GRAPHSYMPERF_NEO4J_USER"],
        password=env["GRAPHSYMPERF_NEO4J_PASSWORD"],
        num_queries=run_config.query_generation.max_cases,
        seed=manifest.seed,
    )
    _write_gdsmith_artifacts(writer, result)
    return query_cases_from_gdsmith(result.parsed.query_records)


def _resolve_source_path(manifest, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
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
