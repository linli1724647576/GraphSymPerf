"""End-to-end GDsmith-backed mining assembly."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlparse

from graph_sym_perf.adapters import GraphAdapter, create_adapter
from graph_sym_perf.app.mining import (
    EXIT_CONFIG_ERROR,
    EXIT_NO_READY_SYSTEMS,
    EXIT_OK,
    _default_run_id,
    _new_runtime_state,
    _preflight,
    _run_query_cases_with_state,
    _select_systems,
    _summary,
    _violation_payload,
    _with_max_cases,
    _with_runtime_overrides,
)
from graph_sym_perf.config import load_run_config
from graph_sym_perf.core import ConfigError, SystemStatus, TargetSystem
from graph_sym_perf.cluster import write_clustered_summary, write_exploratory_leads, write_review_candidates
from graph_sym_perf.gdsmith import check_gdsmith_tooling, query_cases_from_gdsmith, run_gdsmith
from graph_sym_perf.reporter import ArtifactWriter


def run_gdsmith_mining(
    *,
    config_path: str | Path,
    systems: str = "neo4j",
    max_cases: int | None = None,
    run_id: str | None = None,
    seed: int | None = None,
    feedback_enabled: bool | None = None,
    max_rules_per_case: int | None = None,
    transform_family: str = "auto",
    generator_target: str = "neo4j",
    env: Mapping[str, str] | None = None,
) -> int:
    env = dict(os.environ if env is None else env)
    config = load_run_config(config_path)
    if max_cases is not None:
        config = _with_max_cases(config, max_cases)
    config = _with_runtime_overrides(
        config,
        feedback_enabled=feedback_enabled,
        max_rules_per_case=max_rules_per_case,
        transform_family=transform_family,
    )
    selected_systems = _select_systems(config, systems)
    generator = TargetSystem.parse(generator_target)
    backend = _gdsmith_backend(generator)

    run_id = run_id or _default_run_id()
    writer = ArtifactWriter(config, run_id)
    writer.write_json("config_snapshot.json", config)

    adapters: list[GraphAdapter] = []
    statuses: list[SystemStatus] = []
    gdsmith_status = check_gdsmith_tooling(config.query_generation.gdsmith, env)
    try:
        for system_config in selected_systems:
            adapter = create_adapter(system_config, config, env, allow_create=True)
            status = _preflight(adapter)
            statuses.append(status)
            if status.ready:
                adapters.append(adapter)
        writer.write_json("preflight.json", {"run_id": run_id, "systems": statuses, "gdsmith": gdsmith_status})

        if not gdsmith_status.ready:
            writer.write_json(
                "summary.json",
                _summary_with_gdsmith(run_id, statuses, [], no_ready=True, gdsmith_status=gdsmith_status),
            )
            return EXIT_CONFIG_ERROR
        if not adapters:
            writer.write_json(
                "summary.json",
                _summary_with_gdsmith(run_id, statuses, [], no_ready=True, gdsmith_status=gdsmith_status),
            )
            return EXIT_NO_READY_SYSTEMS

        graph_adapter = next((adapter for adapter in adapters if adapter.target is generator), None)
        if graph_adapter is None:
            raise ConfigError(f"GDsmith generator target {generator.value} must be included in --systems")
        gdsmith_config = config.query_generation.gdsmith
        case_budget = config.query_generation.max_cases
        seed_value = seed if seed is not None else gdsmith_config.random_seed
        host, port, username, password = _gdsmith_connection_fields(generator, env)
        result = run_gdsmith(
            config=gdsmith_config,
            env=env,
            raw_dir=writer.run_dir / "gdsmith_raw",
            backend=backend,
            host=host,
            port=port,
            username=username,
            password=password,
            num_queries=case_budget,
            seed=seed_value,
        )
        _write_gdsmith_artifacts(writer, result)
        query_cases = query_cases_from_gdsmith(result.parsed.query_records)
        for adapter in adapters:
            if adapter.target is generator:
                continue
            loader = getattr(adapter, "load_gdsmith_graph", None)
            if callable(loader):
                loader(result.parsed.graph_statements)
        pmc_state, feedback_state, scheduler = _new_runtime_state(config)
        records, run_outputs = _run_query_cases_with_state(
            config,
            writer,
            adapters,
            query_cases,
            pmc_state,
            feedback_state,
            scheduler,
        )
        writer.write_json("confirmation.json", [record.confirmation for record in records if record.confirmation])
        writer.write_json("symmetry_violations.json", _violation_payload(records))
        review = write_review_candidates(writer.run_dir, update_summary=False)
        exploratory = write_exploratory_leads(writer.run_dir, update_summary=False)
        cluster_summary = (
            write_clustered_summary(writer.run_dir, update_summary=False, clustering_config=config.clustering)
            if config.clustering.enabled
            else None
        )
        writer.write_json(
            "summary.json",
            _summary_with_gdsmith(
                run_id,
                statuses,
                records,
                no_ready=False,
                gdsmith_status=gdsmith_status,
                gdsmith_run=result,
                review_candidates=review,
                exploratory_leads=exploratory,
                pmc_summary=run_outputs.get("pmc_summary"),
                feedback_summary=run_outputs.get("feedback_summary"),
                cluster_summary=cluster_summary,
            ),
        )
        return EXIT_OK
    finally:
        for adapter in adapters:
            adapter.close()


def _write_gdsmith_artifacts(writer: ArtifactWriter, result) -> None:
    writer.write_text(
        "gdsmith_graph.cypher",
        "".join(f"{statement};\n" for statement in result.parsed.graph_statements),
    )
    for record in result.parsed.query_records:
        writer.append_jsonl("gdsmith_queries.jsonl", record)
    writer.write_json(
        "gdsmith_manifest.json",
        {
            "command": result.command,
            "returncode": result.returncode,
            "raw_dir": _relative_or_absolute(writer, result.raw_dir),
            "stdout_path": _relative_or_absolute(writer, result.stdout_path),
            "stderr_path": _relative_or_absolute(writer, result.stderr_path),
            "log_path": _relative_or_absolute(writer, result.log_path) if result.log_path else None,
            "graph_statement_count": len(result.parsed.graph_statements),
            "query_record_count": len(result.parsed.query_records),
            "accepted_query_count": len(result.parsed.accepted_queries),
        },
    )


def _summary_with_gdsmith(
    run_id: str,
    statuses: list[SystemStatus],
    records,
    *,
    no_ready: bool,
    gdsmith_status,
    gdsmith_run=None,
    review_candidates=None,
    exploratory_leads=None,
    pmc_summary=None,
    feedback_summary=None,
    cluster_summary=None,
) -> dict[str, object]:
    summary = _summary(
        run_id,
        statuses,
        records,
        no_ready=no_ready,
        pmc_summary=pmc_summary,
        feedback_summary=feedback_summary,
        cluster_summary=cluster_summary,
    )
    if review_candidates is not None:
        summary.update(
            {
                "review_candidate_count": int(review_candidates.get("candidate_count") or 0),
                "review_candidates_artifact": "review_candidates.json",
                "review_candidates_markdown": "review_candidates.md",
            }
        )
    if exploratory_leads is not None:
        summary.update(
            {
                "exploratory_lead_count": int(exploratory_leads.get("lead_count") or 0),
                "exploratory_leads_artifact": "exploratory_leads.json",
                "exploratory_leads_markdown": "exploratory_leads.md",
            }
        )
    summary["gdsmith"] = {
        "tool_status": gdsmith_status,
        "graph_statements": len(gdsmith_run.parsed.graph_statements) if gdsmith_run else 0,
        "query_records": len(gdsmith_run.parsed.query_records) if gdsmith_run else 0,
        "accepted_queries": len(gdsmith_run.parsed.accepted_queries) if gdsmith_run else 0,
        "filtered_queries": (
            sum(1 for record in gdsmith_run.parsed.query_records if not record.accepted) if gdsmith_run else 0
        ),
        "returncode": gdsmith_run.returncode if gdsmith_run else None,
        "log_path": gdsmith_run.log_path if gdsmith_run else None,
    }
    return summary


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
        host, port = _neo4j_host_port(env["GRAPHSYMPERF_NEO4J_URI"])
        return host, port, env["GRAPHSYMPERF_NEO4J_USER"], env["GRAPHSYMPERF_NEO4J_PASSWORD"]
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
    if target is TargetSystem.AGENSGRAPH:
        host, port, username, password = _postgres_uri_to_connection_fields(env["GRAPHSYMPERF_AGENSGRAPH_URI"])
        return (
            host,
            port,
            env.get("GRAPHSYMPERF_AGENSGRAPH_USER") or username or "postgres",
            env.get("GRAPHSYMPERF_AGENSGRAPH_PASSWORD") or password or "",
        )
    raise ConfigError(f"GDsmith connection fields for {target.value} are not configured")


def _neo4j_host_port(uri: str) -> tuple[str, int]:
    from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port

    return neo4j_uri_to_host_port(uri)


def _postgres_uri_to_connection_fields(uri: str) -> tuple[str, int, str | None, str | None]:
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise ConfigError(f"invalid PostgreSQL URI for GDsmith: {uri!r}")
    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None
    return parsed.hostname, parsed.port or 5432, username, password


def _relative_or_absolute(writer: ArtifactWriter, path: str) -> str:
    path_obj = Path(path)
    try:
        return str(path_obj.relative_to(writer.run_dir))
    except ValueError:
        return str(path_obj)
