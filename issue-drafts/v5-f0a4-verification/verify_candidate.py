#!/usr/bin/env python3
"""Rebind and strictly verify the v5 f0a4 candidate on an isolated Neo4j endpoint."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from graph_sym_perf.adapters.lifecycle import reset_graph_state
from graph_sym_perf.core import TargetSystem, to_jsonable
from graph_sym_perf.equivalence.checker import compare_execution_results
from graph_sym_perf.experiment.plan_models import CampaignPhase, RulePlanEntry, RuleStream
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import _EntryValidationAdapterFactory
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import EquivalentQueryPair, RuntimePolicy
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot, verify_graph_snapshot
from graph_sym_perf.validation.models import CandidateBundle, PipelineStage, ValidationPolicy
from graph_sym_perf.validation.session_runner import (
    SpawnedValidationSessionRunner,
    validation_session_timeout_seconds,
)
from graph_sym_perf.validation.validator import IssueValidator


PAIR_HASH = "f0a4e119b9b4cdef2a12d7d94fcfc988f2ef55d1c28ed565c220b661b257bc26"
ENTRY_ID = "p0:neo4j:discovery:pattern_order_symmetry:1280:1"


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _load_env(path: Path) -> dict[str, str]:
    values = dict(os.environ)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), value.strip().strip("\"'"))
    return values


def _entry(rule_plan_path: Path) -> RulePlanEntry:
    plan = _load_json(rule_plan_path)
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise TypeError("rule plan entries are unavailable")
    raw = next(
        (item for item in entries if isinstance(item, dict) and item.get("entry_id") == ENTRY_ID),
        None,
    )
    if raw is None:
        raise ValueError(f"entry not found: {ENTRY_ID}")
    return RulePlanEntry(
        entry_id=str(raw["entry_id"]),
        phase=CampaignPhase(str(raw["phase"])),
        target=TargetSystem(str(raw["target"])),
        rule_id=str(raw["rule_id"]),
        family=str(raw["family"]),
        tier=str(raw["tier"]),
        graph_scale=int(raw["graph_scale"]),
        shard_index=int(raw["shard_index"]),
        shard_seed=int(raw["shard_seed"]),
        accepted_pair_quota=int(raw["accepted_pair_quota"]),
        max_generated_seed_budget=int(raw["max_generated_seed_budget"]),
        max_variants_per_seed=int(raw["max_variants_per_seed"]),
        proof_version=str(raw["proof_version"]),
        query_timeout_seconds=int(raw["query_timeout_seconds"]),
        max_entry_runtime_seconds=int(raw["max_entry_runtime_seconds"]),
        measurement_protocol_id=str(raw["measurement_protocol_id"]),
        stream=RuleStream(str(raw["stream"])),
        regression_case_id=(
            str(raw["regression_case_id"])
            if raw.get("regression_case_id") is not None
            else None
        ),
    )


def _certificate(raw: object) -> SafetyCertificate:
    if not isinstance(raw, dict):
        raise TypeError("candidate safety certificate is unavailable")
    values = dict(raw)
    for name in ("variables_in_scope", "preconditions"):
        if name in values:
            values[name] = tuple(values[name])
    return SafetyCertificate(**values)


def _candidate(
    *,
    root: Path,
    uri: str,
) -> tuple[CandidateBundle, _EntryValidationAdapterFactory, dict[str, object]]:
    run_dir = root / "runs/neo4j-pattern-order-1280-10x1000-20260717-v5"
    entry_dir = run_dir / "entries/p0-neo4j-discovery-pattern_order_symmetry-1280-1-9abbaaa0150b"
    candidate_payload = _load_json(
        entry_dir / f"candidates/{PAIR_HASH}/candidate.json"
    )
    corpus = _load_json(entry_dir / "generation_corpus.json")
    noise = _load_json(entry_dir / "noise_calibration.json")
    graph_statements = corpus.get("graph_statements")
    if not isinstance(graph_statements, list) or not all(
        isinstance(statement, str) for statement in graph_statements
    ):
        raise TypeError("generation corpus graph statements are unavailable")
    snapshot = GraphSnapshot.from_statements(tuple(graph_statements))
    graph_manifest = candidate_payload.get("graph_manifest")
    if not isinstance(graph_manifest, dict):
        raise TypeError("candidate graph manifest is unavailable")
    if snapshot.content_hash != graph_manifest.get("graph_fingerprint"):
        raise ValueError("candidate graph snapshot hash mismatch")

    spec = load_experiment_spec(root / "config/neo4j_pattern_order_1280_10x1000.yaml")
    entry = _entry(run_dir / "rule_plan.json")
    env = _load_env(root / ".env")
    env["GRAPHSYMPERF_NEO4J_URI"] = uri
    env.pop("GRAPHSYMPERF_NEO4J_DATABASE", None)
    factory = _EntryValidationAdapterFactory(spec, env, entry)
    probe = factory(None)  # the production factory does not inspect the candidate argument
    try:
        connected = probe.connectivity_check()
        if not connected or connected[0] is not True:
            raise RuntimeError(f"isolated Neo4j is unavailable: {connected}")
        fingerprint = probe.dbms_fingerprint()
        if not fingerprint.available:
            raise RuntimeError(f"isolated DBMS fingerprint unavailable: {fingerprint.reason}")
        dbms_fingerprint = str(fingerprint.value)
        dbms_version = str(fingerprint.server_version)
    finally:
        probe.close()

    certificate = _certificate(candidate_payload.get("safety_certificate"))
    query_a = candidate_payload.get("query_a")
    query_b = candidate_payload.get("query_b")
    parameters = candidate_payload.get("parameters")
    if not isinstance(query_a, str) or not isinstance(query_b, str) or not isinstance(parameters, dict):
        raise TypeError("candidate query pair is unavailable")
    pair = EquivalentQueryPair(
        query_a,
        query_b,
        parameters,
        snapshot.content_hash,
        dbms_fingerprint,
        str(candidate_payload.get("entry_identity", {}).get("rule_id")),
        certificate,
    )
    stage2 = candidate_payload.get("stage2_evidence")
    if not isinstance(stage2, dict):
        raise TypeError("Stage-2 evidence is unavailable")
    calibration = noise.get("calibration")
    if not isinstance(calibration, dict) or calibration.get("valid") is not True:
        raise ValueError("historical A/A calibration did not pass")
    noise_p99_ms = float(calibration["p99_delta_ms"])
    session_timeout = validation_session_timeout_seconds(
        query_timeout_seconds=float(entry.query_timeout_seconds),
        warmups_per_session=int(spec.validation["warmups_per_session"]),
        pairs_per_session=int(spec.validation["pairs_per_session"]),
    )
    candidate = CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version=dbms_version,
        resolved_config={
            "entry_id": entry.entry_id,
            "stream": entry.stream.value,
            "target": entry.target.value,
            "validation": dict(spec.validation),
        },
        discovery_slow_query=str(stage2["slow_query"]),
        discovery_ratio=float(stage2["median_ratio"]),
        certificate=certificate,
        validation_policy=ValidationPolicy(
            noise_floor_ms=noise_p99_ms,
            timer_resolution_ms=0.001,
            max_result_rows=int(spec.validation["max_result_rows"]),
            query_timeout_seconds=float(entry.query_timeout_seconds),
            session_timeout_seconds=session_timeout,
        ),
    )
    integrity_reasons = candidate.verify_integrity()
    if integrity_reasons:
        raise ValueError(f"rebound candidate integrity failure: {integrity_reasons}")
    provenance = {
        "historical_run": run_dir.name,
        "historical_pair_hash": PAIR_HASH,
        "historical_graph_fingerprint": snapshot.content_hash,
        "historical_stage2": stage2,
        "isolated_uri": uri,
        "isolated_dbms_fingerprint": dbms_fingerprint,
        "isolated_dbms_version": dbms_version,
        "rebound_candidate_hash": candidate.candidate_hash,
        "noise_floor_ms": noise_p99_ms,
        "session_timeout_seconds": session_timeout,
    }
    return candidate, factory, provenance


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _screen(
    candidate: CandidateBundle,
    factory: _EntryValidationAdapterFactory,
    *,
    pairs: int,
) -> dict[str, object]:
    adapter = factory(candidate)
    try:
        connected = adapter.connectivity_check()
        if not connected or connected[0] is not True:
            raise RuntimeError(f"isolated Neo4j is unavailable: {connected}")
        reset_graph_state(
            adapter,
            query_kind="v5_candidate_screen_reset",
            timeout_seconds=candidate.validation_policy.query_timeout_seconds,
        )
        adapter.load_gdsmith_graph(candidate.graph_snapshot.statements)
        verification = verify_graph_snapshot(
            adapter,
            candidate.graph_snapshot,
            max_rows=candidate.validation_policy.max_result_rows,
            timeout_seconds=candidate.validation_policy.query_timeout_seconds,
        )
        if not verification.verified:
            raise RuntimeError(f"graph verification failed: {verification.reason_codes}")
        left = adapter.execute(
            candidate.pair.query_a,
            candidate.pair.parameters,
            "v5_candidate_screen_equivalence_a",
            max_rows=candidate.validation_policy.max_result_rows,
            timeout_seconds=candidate.validation_policy.query_timeout_seconds,
        )
        right = adapter.execute(
            candidate.pair.query_b,
            candidate.pair.parameters,
            "v5_candidate_screen_equivalence_b",
            max_rows=candidate.validation_policy.max_result_rows,
            timeout_seconds=candidate.validation_policy.query_timeout_seconds,
        )
        equivalence = compare_execution_results(
            left,
            right,
            ordered=False,
            max_result_rows=candidate.validation_policy.max_result_rows,
        )
        evidence = RuntimeHarness().measure_pair(
            adapter,
            candidate.pair.query_a,
            candidate.pair.query_b,
            parameters=candidate.pair.parameters,
            policy=RuntimePolicy(
                warmups=0,
                pairs=pairs,
                seed=2026071701,
                require_exact_balance=pairs % 2 == 0,
                timeout_ms=candidate.validation_policy.query_timeout_seconds * 1000,
            ),
            expected_slow_query="a",
        )
    finally:
        adapter.close()
    passed = bool(
        equivalence.equivalent
        and equivalence.complete_result
        and evidence.slow_query == "a"
        and evidence.median_ratio is not None
        and evidence.median_ratio >= 1.5
        and evidence.ci_ratio_low is not None
        and evidence.ci_ratio_low >= 1.2
        and evidence.median_delta_ms is not None
        and evidence.median_delta_ms >= max(
            5.0 * candidate.validation_policy.noise_floor_ms,
            50.0,
        )
        and evidence.ci_delta_low_ms is not None
        and evidence.ci_delta_low_ms > candidate.validation_policy.noise_floor_ms
        and evidence.direction_consistency >= 0.75
    )
    return {
        "passed": passed,
        "gate": "strict_absolute_precheck",
        "pairs": pairs,
        "equivalence": asdict(equivalence),
        "runtime_evidence": asdict(evidence),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--uri", default="bolt://127.0.0.1:7701")
    parser.add_argument("--mode", choices=("offline", "screen", "validate"), required=True)
    parser.add_argument("--screen-pairs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    candidate, factory, provenance = _candidate(root=args.root.resolve(), uri=args.uri)
    if args.mode == "offline":
        payload = {"mode": "offline", "passed": True, "provenance": provenance}
        exit_code = 0
    elif args.mode == "screen":
        if args.screen_pairs <= 0:
            parser.error("--screen-pairs must be positive")
        screen = _screen(candidate, factory, pairs=args.screen_pairs)
        payload = {"mode": "screen", "provenance": provenance, **screen}
        exit_code = 0 if screen["passed"] else 2
    else:
        report = IssueValidator().validate(
            candidate,
            SpawnedValidationSessionRunner(factory),
        )
        passed = report.stage is PipelineStage.VALIDATION_PASSED
        payload = {
            "mode": "validate",
            "passed": passed,
            "provenance": provenance,
            "report": asdict(report),
        }
        exit_code = 0 if passed else 2
    _write(args.output, payload)
    print(
        json.dumps(
            {
                "mode": args.mode,
                "passed": payload["passed"],
                "output": str(args.output),
                "candidate_hash": candidate.candidate_hash,
            },
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
