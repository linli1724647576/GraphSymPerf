#!/usr/bin/env python3
"""Strictly replay and reduce the v6 f989 candidate on a live Neo4j endpoint."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from graph_sym_perf.core import TargetSystem, to_jsonable
from graph_sym_perf.experiment.candidate_pipeline import ProductionCandidatePipeline, _rule_witness
from graph_sym_perf.experiment.plan_models import CampaignPhase, RulePlanEntry, RuleStream
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.experiment.worker import _EntryValidationAdapterFactory
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, PipelineStage, ValidationPolicy
from graph_sym_perf.validation.sealed_replay import replay_issue_validation_report


PAIR_HASH = "f9891d29092edfaa5bd39b26cf3552f32194026c4b6ed919f74b08c5a3dec994"
ENTRY_ID = "p0:neo4j:discovery:pattern_order_symmetry:1280:1"
RUN_NAME = "neo4j-pattern-order-1280-10x1000-20260717-v6"


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


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    run_dir = root / "runs" / RUN_NAME
    entry_dir = run_dir / "entries/p0-neo4j-discovery-pattern_order_symmetry-1280-1-9abbaaa0150b"
    candidate_payload = _load_json(entry_dir / f"candidates/{PAIR_HASH}/candidate.json")
    corpus = _load_json(entry_dir / "generation_corpus.json")
    validation_payload = _load_json(entry_dir / f"validation/{PAIR_HASH}.json")
    statements = corpus.get("graph_statements")
    if not isinstance(statements, list) or not all(isinstance(item, str) for item in statements):
        raise TypeError("generation corpus graph statements are unavailable")
    snapshot = GraphSnapshot.from_statements(tuple(statements))
    manifest = validation_payload.get("report", {}).get("manifest")
    policy_payload = validation_payload.get("report", {}).get("validation_policy")
    if not isinstance(manifest, dict) or not isinstance(policy_payload, dict):
        raise TypeError("sealed candidate manifest or validation policy is unavailable")

    certificate = _certificate(candidate_payload.get("safety_certificate"))
    query_a = candidate_payload.get("query_a")
    query_b = candidate_payload.get("query_b")
    parameters = candidate_payload.get("parameters")
    pair_payload = manifest.get("pair")
    if (
        not isinstance(query_a, str)
        or not isinstance(query_b, str)
        or not isinstance(parameters, dict)
        or not isinstance(pair_payload, dict)
    ):
        raise TypeError("candidate pair is unavailable")
    pair = EquivalentQueryPair(
        query_a,
        query_b,
        parameters,
        snapshot.content_hash,
        str(pair_payload["dbms_fingerprint"]),
        str(pair_payload["rule_id"]),
        certificate,
    )
    spec = load_experiment_spec(root / "config/neo4j_pattern_order_1280_10x1000.yaml")
    entry = _entry(run_dir / "rule_plan.json")
    candidate = CandidateBundle.create(
        pair,
        graph_snapshot=snapshot,
        dbms_version=str(manifest["dbms_version"]),
        resolved_config={
            "entry_id": entry.entry_id,
            "stream": entry.stream.value,
            "target": entry.target.value,
            "validation": dict(spec.validation),
        },
        discovery_slow_query=str(manifest["discovery_slow_query"]),
        discovery_ratio=float(manifest["discovery_ratio"]),
        certificate=certificate,
        validation_policy=ValidationPolicy(**policy_payload),
    )
    if candidate.candidate_hash != validation_payload.get("candidate_hash"):
        raise ValueError("reconstructed candidate hash differs from sealed validation")
    report_payload = validation_payload.get("report")
    report = replay_issue_validation_report(candidate, report_payload)
    if report.stage is not PipelineStage.VALIDATION_PASSED:
        raise ValueError("sealed original validation is not passing")

    env = _load_env(root / ".env")
    env["GRAPHSYMPERF_NEO4J_URI"] = args.uri
    factory = _EntryValidationAdapterFactory(spec, env, entry)
    probe = factory(candidate)
    try:
        connected = probe.connectivity_check()
        if not connected or connected[0] is not True:
            raise RuntimeError(f"Neo4j is unavailable: {connected}")
        fingerprint = probe.dbms_fingerprint()
        if not fingerprint.available or str(fingerprint.value) != pair.dbms_fingerprint:
            raise RuntimeError(
                "live Neo4j fingerprint differs from sealed candidate: "
                f"live={fingerprint.value!s} sealed={pair.dbms_fingerprint}"
            )
    finally:
        probe.close()

    pipeline = ProductionCandidatePipeline(spec, environment=env, run_dir=run_dir)
    reduction = pipeline._reduce(
        candidate,
        report,
        task=entry,
        adapter_factory=factory,
        checkpoint_path=args.checkpoint.resolve(),
        rule_witness=_rule_witness(candidate),
    )
    payload = {
        "mode": "strict_live_reduction",
        "source_run": RUN_NAME,
        "source_pair_hash": PAIR_HASH,
        "source_candidate_hash": candidate.candidate_hash,
        "source_validation_stage": report.stage.value,
        "live_uri": args.uri,
        "passed": reduction.status.value == "reduced_valid",
        "reduction": asdict(reduction),
    }
    _write(args.output.resolve(), payload)
    print(
        json.dumps(
            {
                "passed": payload["passed"],
                "status": reduction.status.value,
                "stop_reason": reduction.stop_reason,
                "accepted_steps": len(reduction.accepted_steps),
                "rejected_steps": len(reduction.rejected_steps),
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0 if payload["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
