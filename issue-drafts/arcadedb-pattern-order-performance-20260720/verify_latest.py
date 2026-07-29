#!/usr/bin/env python3
"""Rebind one ArcadeDB rule candidate and run the production validator."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.config import environment_with_dotenv, migrate_experiment_spec_to_run_config
from graph_sym_perf.core import TargetSystem, to_jsonable
from graph_sym_perf.experiment.candidate_pipeline import _ValidationAdapterFactory
from graph_sym_perf.experiment.spec import load_experiment_spec
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.engine import apply_certified_rule_with_proof
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.transforms.safety_points import enumerate_safety_points
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import CandidateBundle, PipelineStage, ValidationPolicy
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner
from graph_sym_perf.validation.validator import IssueValidator


DEFAULT_CANDIDATE = Path(
    "runs/arcadedb-candidate-hunt-20260719/"
    "main_runtime_first_gate_arcadedb_100-36a7c55569aa/"
    "shards/arcadedb-000/artifacts/objects/04/"
    "04bf107cfd646e12b48c8c80772bf39137aad4a8fc897b08fc14129e9f25f752"
)
DEFAULT_SNAPSHOT = Path(
    "runs/arcadedb-candidate-hunt-20260719/"
    "main_runtime_first_gate_arcadedb_100-36a7c55569aa/"
    "shards/arcadedb-000/artifacts/objects/aa/"
    "aaacabc4196a73f50a696f8a91c4c6dc9d2abc186ad9c19def30791153b8c9c2"
)
DEFAULT_IMAGE_DIGEST = (
    "sha256:0a7935789d65da3ae5e328c33fc56c3144115d324be14d561967afc65c552a86"
)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path}")
    return value


def _certificate(raw: object) -> SafetyCertificate:
    if not isinstance(raw, dict):
        raise TypeError("candidate safety certificate is unavailable")
    values = dict(raw)
    for name in ("variables_in_scope", "preconditions"):
        if name in values:
            values[name] = tuple(values[name])
    return SafetyCertificate(**values)


def _certificate_for_pair(query_a: str, query_b: str) -> tuple[SafetyCertificate, dict[str, object]]:
    rule = RuleRegistry.default().get("pattern_order_symmetry")
    for certificate in enumerate_safety_points(query_a, rule, "arcadedb-latest"):
        result = apply_certified_rule_with_proof(query_a, rule, certificate)
        if result.query == query_b:
            return result.certificate, dict(result.semantic_proof)
    raise ValueError("query B is not a certified pattern_order_symmetry transform of query A")


def _write_json(path: Path, payload: object) -> None:
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
    parser.add_argument("--config", type=Path, default=Path("config/experiment.yaml"))
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--query-a", type=Path)
    parser.add_argument("--query-b", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:2482")
    parser.add_argument("--database", default="graphsymperf_arcadedb_perf_verify_20260720")
    parser.add_argument("--container", default="graphsymperf-arcadedb-verify-20260720")
    parser.add_argument("--image-digest", default=DEFAULT_IMAGE_DIGEST)
    parser.add_argument("--hard-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    candidate_payload = _load_json(root / args.candidate)
    graph_snapshot = GraphSnapshot.from_statements(
        tuple((root / args.snapshot).read_text(encoding="utf-8").splitlines())
    )
    expected_snapshot_hash = candidate_payload.get("graph_snapshot_hash")
    if graph_snapshot.content_hash != expected_snapshot_hash:
        raise ValueError("graph snapshot hash does not match the source candidate")

    spec = load_experiment_spec(root / args.config)
    target = TargetSystem.ARCADEDB
    environment = environment_with_dotenv(None, dotenv_path=root / args.dotenv)
    environment.update(
        {
            "GRAPHSYMPERF_ARCADEDB_HTTP_URL": args.url,
            "GRAPHSYMPERF_ARCADEDB_DATABASE": args.database,
            "GRAPHSYMPERF_ARCADEDB_CONTAINER": args.container,
            "GRAPHSYMPERF_ARCADEDB_IMAGE_DIGEST": args.image_digest,
        }
    )
    factory = _ValidationAdapterFactory(spec, environment, target)

    run_config = migrate_experiment_spec_to_run_config(spec, target)
    probe = create_adapter(
        run_config.systems[0],
        run_config,
        environment,
        allow_create=True,
    )
    try:
        connected = probe.connectivity_check()
        if not connected or connected[0] is not True:
            raise RuntimeError(f"isolated ArcadeDB is unavailable: {connected}")
        fingerprint = probe.dbms_fingerprint()
        if not fingerprint.available:
            raise RuntimeError(f"isolated DBMS fingerprint unavailable: {fingerprint.reason}")
        dbms_fingerprint = str(fingerprint.value)
        dbms_version = str(fingerprint.server_version)
    finally:
        probe.close()

    query_a = (
        (root / args.query_a).read_text(encoding="utf-8").strip().rstrip(";")
        if args.query_a is not None
        else candidate_payload.get("query_a")
    )
    query_b = (
        (root / args.query_b).read_text(encoding="utf-8").strip().rstrip(";")
        if args.query_b is not None
        else candidate_payload.get("query_b")
    )
    parameters = candidate_payload.get("parameters")
    if not isinstance(query_a, str) or not isinstance(query_b, str) or not isinstance(parameters, dict):
        raise TypeError("candidate query pair is unavailable")
    if args.query_a is not None or args.query_b is not None:
        if args.query_a is None or args.query_b is None:
            parser.error("--query-a and --query-b must be provided together")
        certificate, semantic_proof = _certificate_for_pair(query_a, query_b)
    else:
        certificate = _certificate(candidate_payload.get("safety_certificate"))
        semantic_proof = {}
    pair = EquivalentQueryPair(
        query_a=query_a,
        query_b=query_b,
        parameters=parameters,
        graph_snapshot_hash=graph_snapshot.content_hash,
        dbms_fingerprint=dbms_fingerprint,
        rule_id=str(candidate_payload.get("rule_id")),
        safety_certificate=certificate,
    )
    validation = dict(spec.validation)
    query_timeout_seconds = float(spec.systems[target].default_timeout_seconds)
    candidate = CandidateBundle.create(
        pair,
        graph_snapshot=graph_snapshot,
        dbms_version=dbms_version,
        resolved_config={
            "target": target.value,
            "validation": validation,
            "latest_revalidation": True,
            "source_candidate_hash": candidate_payload.get("candidate_hash"),
        },
        discovery_slow_query=str(candidate_payload.get("discovery_slow_query")),
        discovery_ratio=float(candidate_payload["discovery_median_ratio"]),
        certificate=certificate,
        validation_policy=ValidationPolicy(
            noise_floor_ms=0.0,
            timer_resolution_ms=0.001,
            min_ratio=float(validation["min_ratio"]),
            min_ci_low=float(validation["min_ci_lower_ratio"]),
            max_result_rows=int(validation["max_result_rows"]),
            query_timeout_seconds=query_timeout_seconds,
            session_timeout_seconds=args.hard_timeout_seconds,
        ),
    )
    integrity_reasons = candidate.verify_integrity()
    if integrity_reasons:
        raise ValueError(f"rebound candidate integrity failure: {integrity_reasons}")

    report = IssueValidator().validate(
        candidate,
        SpawnedValidationSessionRunner(
            factory,
            hard_timeout_seconds=args.hard_timeout_seconds,
        ),
    )
    passed = report.stage is PipelineStage.VALIDATION_PASSED
    payload = {
        "passed": passed,
        "provenance": {
            "source_candidate_hash": candidate_payload.get("candidate_hash"),
            "source_pair_hash": candidate_payload.get("pair_hash"),
            "source_graph_snapshot_hash": graph_snapshot.content_hash,
            "source_discovery_ratio": candidate_payload.get("discovery_median_ratio"),
            "source_discovery_delta_ms": candidate_payload.get("discovery_median_delta_ms"),
            "rebound_candidate_hash": candidate.candidate_hash,
            "endpoint": args.url,
            "database": args.database,
            "container": args.container,
            "image_digest": args.image_digest,
            "dbms_version": dbms_version,
            "dbms_fingerprint": dbms_fingerprint,
            "protocol": "production IssueValidator / independent-two-session-v1",
            "semantic_proof": semantic_proof,
        },
        "report": asdict(report),
    }
    _write_json(root / args.output, payload)
    print(
        json.dumps(
            {
                "passed": passed,
                "stage": report.stage.value,
                "candidate_hash": candidate.candidate_hash,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
