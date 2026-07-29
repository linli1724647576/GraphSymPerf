"""One shard's runtime-first generation, acceptance, and screening lifecycle.

The worker deliberately owns only a single `(DBMS, shard)` execution context.
It does not use plan or PROFILE APIs, and it stores only minimal accepted-pair
records until a pair earns candidate retention through ordinary runtime screen.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.core import (
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
)
from graph_sym_perf.dialects.agensgraph import AgensGraphDialect
from graph_sym_perf.dialects.apache_age import ApacheAgeDialect
from graph_sym_perf.dialects.base import DecisionKind, PortableQuery
from graph_sym_perf.dialects.native import NativeCypherDialect
from graph_sym_perf.equivalence.checker import compare_typed_results
from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.experiment.plan_models import ExperimentSpec, ShardTask
from graph_sym_perf.gdsmith.replay import gdsmith_read_query_is_replayable
from graph_sym_perf.gdsmith.runner import neo4j_uri_to_host_port, run_gdsmith
from graph_sym_perf.generation.symmetric_state import (
    SymmetryQueryPair,
    build_involutive_symmetry_state,
)
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import RuntimePolicy, RuntimeVerdict


@dataclass(frozen=True)
class GeneratedShard:
    """Symmetric replay graph and certified graph-pattern pairs for one shard."""

    graph_statements: tuple[str, ...]
    query_pairs: tuple[SymmetryQueryPair, ...]
    generator_metadata: Mapping[str, object]


class _AdapterFactory(Protocol):
    def __call__(self, task: ShardTask) -> Any: ...


class _GenerationRunner(Protocol):
    def __call__(self, task: ShardTask, raw_dir: Path) -> GeneratedShard: ...


class RuntimeFirstShardWorker:
    """Build accepted pairs and runtime-screen them with a fresh adapter.

    `GeneratedShard` contains the portable primary-label projection of a
    GDsmith-derived involutive state. AGE and LadybugDB may use a compatible
    GDsmith proxy for base generation, but graph replay and all measurements
    happen on the target adapter. A pair is counted only after both ordinary
    executions return complete, typed-equivalent results.
    """

    def __init__(
        self,
        spec: ExperimentSpec,
        *,
        environment: Mapping[str, str] | None = None,
        generation_runner: _GenerationRunner | None = None,
        adapter_factory: _AdapterFactory | None = None,
    ) -> None:
        self.spec = spec
        self.environment = dict(os.environ if environment is None else environment)
        self._generation_runner = generation_runner or self._generate_with_gdsmith
        self._adapter_factory = adapter_factory or self._create_adapter
        self._run_dir: Path | None = None

    def bind_run_dir(self, run_dir: str | Path) -> None:
        self._run_dir = Path(run_dir)

    def __call__(self, task: ShardTask) -> dict[str, object]:
        if self._run_dir is None:
            raise RuntimeError("shard worker was not bound to an immutable run directory")
        shard_dir = _allocate_shard_attempt_dir(self._run_dir, task)
        generated = self._generation_runner(task, shard_dir / "gdsmith_raw")
        adapter = self._adapter_factory(task)
        try:
            self._reset_and_load(adapter, generated.graph_statements)
            accepted, rejects = self._build_accepted_pairs(adapter, task, generated.query_pairs, shard_dir)
            if len(accepted) < task.accepted_query_pairs:
                raise RuntimeError(
                    f"accepted only {len(accepted)} of "
                    f"{task.accepted_query_pairs} required symmetry pairs"
                )
            candidates = self._screen_pairs(adapter, task, accepted, shard_dir)
        finally:
            adapter.close()
        summary = {
            "target": task.target.value,
            "shard_index": task.shard_index,
            "generation": dict(generated.generator_metadata),
            "generated_query_records": len(generated.query_pairs),
            "accepted_pairs": len(accepted),
            "screened_candidates": candidates,
            "reject_reasons": dict(sorted(rejects.items())),
        }
        (shard_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return summary

    def _reset_and_load(self, adapter: Any, graph_statements: tuple[str, ...]) -> None:
        # A shard can only start from a known empty graph.  Failure to reset is
        # fatal: continuing would combine this shard with stale graph state.
        adapter.execute("MATCH (n) DETACH DELETE n", {}, "shard_reset")
        ensure_schema = getattr(adapter, "ensure_schema", None)
        if callable(ensure_schema):
            ensure_schema()
        adapter.load_gdsmith_graph(graph_statements)

    def _build_accepted_pairs(
        self,
        adapter: Any,
        task: ShardTask,
        query_pairs: tuple[SymmetryQueryPair, ...],
        shard_dir: Path,
    ) -> tuple[list[SymmetryQueryPair], Counter[str]]:
        accepted: list[SymmetryQueryPair] = []
        rejects: Counter[str] = Counter()
        seen: set[str] = set()
        max_rows = int(self.spec.validation["max_result_rows"])
        timeout = self.spec.systems[task.target].default_timeout_seconds
        records_path = shard_dir / "accepted_pairs.jsonl"
        for pair in query_pairs[: task.max_generated_seeds]:
            if len(accepted) >= task.accepted_query_pairs:
                break
            base, transformed = pair.query_a, pair.query_b
            if not assess_query_purity(base).safe or not assess_query_purity(transformed).safe:
                rejects["PURITY_REJECT"] += 1
                continue
            decision = _dialect_for(task.target).check(PortableQuery(base))
            transformed_decision = _dialect_for(task.target).check(PortableQuery(transformed))
            if decision.kind is DecisionKind.REJECT or transformed_decision.kind is DecisionKind.REJECT:
                rejects["DIALECT_REJECT"] += 1
                continue
            replayable = getattr(adapter, "gdsmith_query_is_replayable", gdsmith_read_query_is_replayable)
            if not replayable(base) or not replayable(transformed):
                rejects["DIALECT_REPLAY_REJECT"] += 1
                continue
            pair_hash = _pair_hash(base, transformed)
            if pair_hash in seen:
                rejects["DUPLICATE_AST"] += 1
                continue
            try:
                left = adapter.execute(base, {}, "acceptance_base", max_rows=max_rows, timeout_seconds=timeout)
                right = adapter.execute(transformed, {}, "acceptance_transformed", max_rows=max_rows, timeout_seconds=timeout)
            except Exception:
                rejects["QUERY_EXECUTION_ERROR"] += 1
                continue
            if _is_truncated(left, max_rows) or _is_truncated(right, max_rows):
                rejects["RESULT_TRUNCATED"] += 1
                continue
            equivalence = compare_typed_results(left.rows, right.rows, ordered=_has_order_by(base))
            if not equivalence.equivalent:
                rejects["TYPED_EQUIVALENCE_FAILED"] += 1
                continue
            seen.add(pair_hash)
            accepted.append(pair)
            _append_jsonl(
                records_path,
                {
                    "pair_hash": pair_hash,
                    "query_a_hash": _query_hash(base),
                    "query_b_hash": _query_hash(transformed),
                    "rule_id": pair.rule_id,
                    "symmetry_certificate": dict(pair.certificate),
                    "equivalence": "complete_typed_equivalent",
                },
            )
        return accepted, rejects

    def _screen_pairs(
        self,
        adapter: Any,
        task: ShardTask,
        pairs: list[SymmetryQueryPair],
        shard_dir: Path,
    ) -> int:
        policy_config = self.spec.runtime["screen"]
        policy = RuntimePolicy(
            warmups=int(policy_config["warmups"]),
            pairs=int(policy_config["pairs"]),
            seed=task.seed,
            require_exact_balance=False,
            timeout_ms=float(self.spec.systems[task.target].default_timeout_seconds * 1000),
            bootstrap_resamples=1,
        )
        threshold = float(policy_config["ratio"])
        harness = RuntimeHarness()
        candidates = 0
        for index, pair in enumerate(pairs):
            base, transformed = pair.query_a, pair.query_b
            evidence = harness.measure_pair(adapter, base, transformed, policy=RuntimePolicy(**{**policy.__dict__, "seed": task.seed + index}))
            if evidence.verdict is not RuntimeVerdict.PASS or evidence.median_ratio is None or evidence.median_ratio < threshold:
                continue
            candidates += 1
            _append_jsonl(
                shard_dir / "candidates.jsonl",
                {
                    "pair_hash": _pair_hash(base, transformed),
                    "query_a": base,
                    "query_b": transformed,
                    "rule_id": pair.rule_id,
                    "symmetry_certificate": dict(pair.certificate),
                    "discovery_median_ratio": evidence.median_ratio,
                    "discovery_median_delta_ms": evidence.median_delta_ms,
                    "runtime_protocol": "execute-only-screen-v3",
                },
            )
        return candidates

    def _create_adapter(self, task: ShardTask) -> Any:
        definition = self.spec.systems[task.target]
        system = SystemConfig(task.target, definition.required_env, _optional_env(task.target), {})
        run_config = RunConfig(
            source_path=str(self.spec.source_path),
            paths=RunPaths(),
            systems=(system,),
            dataset=DatasetConfig(),
            query_generation=QueryGenerationConfig(gdsmith=GdsmithConfig()),
            equivalence=EquivalenceConfig(max_result_rows=int(self.spec.validation["max_result_rows"])),
            confirmation=ConfirmationConfig(),
        )
        return create_adapter(system, run_config, self.environment, allow_create=True)

    def _generate_with_gdsmith(self, task: ShardTask, raw_dir: Path) -> GeneratedShard:
        backend, host, port, username, password = _gdsmith_connection(task.target, self.environment)
        result = run_gdsmith(
            config=GdsmithConfig(
                algorithm=self.spec.generation.algorithm,
                backend_extra_args={
                    backend: (
                        "--max-node-num",
                        str(self.spec.generation.max_node_num),
                        "--label-num",
                        str(self.spec.generation.label_num),
                    )
                },
                timeout_seconds=min(float(task.max_runtime_seconds), 3600.0),
            ),
            env=self.environment,
            raw_dir=raw_dir,
            backend=backend,
            host=host,
            port=port,
            username=username,
            password=password,
            num_queries=self.spec.generation.bootstrap_queries,
            seed=task.seed,
        )
        if result.returncode != 0:
            raise RuntimeError(f"GDsmith failed with code {result.returncode}")
        symmetric_state = build_involutive_symmetry_state(
            result.parsed.graph_statements,
            max_pairs=task.max_generated_seeds,
            seed=task.seed,
        )
        return GeneratedShard(
            graph_statements=symmetric_state.graph_statements,
            query_pairs=symmetric_state.query_pairs,
            generator_metadata={
                "backend": backend,
                "algorithm": self.spec.generation.algorithm,
                "bootstrap_queries": self.spec.generation.bootstrap_queries,
                "gdsmith_max_node_num": self.spec.generation.max_node_num,
                "gdsmith_label_num": self.spec.generation.label_num,
                "generation_seed": task.seed,
                "generated_query_record_limit": task.max_generated_seeds,
                "gdsmith_returncode": result.returncode,
                "gdsmith_accepted_query_records": len(result.parsed.accepted_queries),
                **dict(symmetric_state.metadata),
            },
        )


def _dialect_for(target: TargetSystem):
    if target is TargetSystem.AGENSGRAPH:
        return AgensGraphDialect()
    if target is TargetSystem.APACHE_AGE:
        return ApacheAgeDialect()
    return NativeCypherDialect()


def _is_truncated(result: Any, max_rows: int) -> bool:
    return bool(getattr(result, "summary", {}).get("truncated")) or len(result.rows) > max_rows


def _has_order_by(query: str) -> bool:
    return bool(re.search(r"\bORDER\s+BY\b", query, flags=re.IGNORECASE))


def _query_hash(query: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", query).strip().encode("utf-8")).hexdigest()


def _pair_hash(query_a: str, query_b: str) -> str:
    return hashlib.sha256(f"{_query_hash(query_a)}:{_query_hash(query_b)}".encode("ascii")).hexdigest()


def _append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")


def _allocate_shard_attempt_dir(run_dir: Path, task: ShardTask) -> Path:
    shards_dir = run_dir / "shards"
    base_name = f"{task.target.value}-{task.shard_index:03d}"
    for attempt in range(10_000):
        name = base_name if attempt == 0 else f"{base_name}-retry-{attempt:03d}"
        candidate = shards_dir / name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"exhausted artifact directories for shard {base_name}")


def _optional_env(target: TargetSystem) -> Mapping[str, str]:
    return {
        TargetSystem.NEO4J: {"database": "GRAPHSYMPERF_NEO4J_DATABASE"},
        TargetSystem.MEMGRAPH: {"username": "GRAPHSYMPERF_MEMGRAPH_USER", "password": "GRAPHSYMPERF_MEMGRAPH_PASSWORD"},
        TargetSystem.FALKORDB: {"username": "GRAPHSYMPERF_FALKORDB_USER", "password": "GRAPHSYMPERF_FALKORDB_PASSWORD"},
        TargetSystem.AGENSGRAPH: {"database": "GRAPHSYMPERF_AGENSGRAPH_DATABASE"},
        TargetSystem.APACHE_AGE: {"database": "GRAPHSYMPERF_APACHE_AGE_DATABASE"},
    }.get(target, {})


def _gdsmith_connection(target: TargetSystem, env: Mapping[str, str]) -> tuple[str, str, int, str, str]:
    if target is TargetSystem.NEO4J:
        host, port = neo4j_uri_to_host_port(_required(env, "GRAPHSYMPERF_NEO4J_URI"))
        return "neo4j", host, port, _required(env, "GRAPHSYMPERF_NEO4J_USER"), _required(env, "GRAPHSYMPERF_NEO4J_PASSWORD")
    if target is TargetSystem.MEMGRAPH:
        return "memgraph", _required(env, "GRAPHSYMPERF_MEMGRAPH_HOST"), int(env.get("GRAPHSYMPERF_MEMGRAPH_PORT", "7687")), env.get("GRAPHSYMPERF_MEMGRAPH_USER", ""), env.get("GRAPHSYMPERF_MEMGRAPH_PASSWORD", "")
    if target is TargetSystem.FALKORDB:
        return "redisgraph", _required(env, "GRAPHSYMPERF_FALKORDB_HOST"), int(env.get("GRAPHSYMPERF_FALKORDB_PORT", "6379")), env.get("GRAPHSYMPERF_FALKORDB_USER", ""), env.get("GRAPHSYMPERF_FALKORDB_PASSWORD", "")
    if target is TargetSystem.AGENSGRAPH:
        host, port, user, password = _postgres_connection(_required(env, "GRAPHSYMPERF_AGENSGRAPH_URI"))
        return "agensgraph", host, port, env.get("GRAPHSYMPERF_AGENSGRAPH_USER", user), env.get("GRAPHSYMPERF_AGENSGRAPH_PASSWORD", password)
    if target is TargetSystem.ARCADEDB:
        parsed = urlparse(_required(env, "GRAPHSYMPERF_ARCADEDB_HTTP_URL"))
        if not parsed.hostname:
            raise ValueError("GRAPHSYMPERF_ARCADEDB_HTTP_URL must contain a hostname")
        return "arcadedb", parsed.hostname, parsed.port or 2480, env.get("GRAPHSYMPERF_ARCADEDB_USER", "root"), env.get("GRAPHSYMPERF_ARCADEDB_PASSWORD", "")
    if target is TargetSystem.APACHE_AGE:
        host, port, user, password = _proxy_connection(env, target)
        return "agensgraph", host, port, user, password
    if target is TargetSystem.LADYBUGDB:
        host, port, user, password = _proxy_connection(env, target)
        return "neo4j", host, port, user, password
    raise ValueError(f"no GDsmith generation backend for {target.value}")


def _proxy_connection(env: Mapping[str, str], target: TargetSystem) -> tuple[str, int, str, str]:
    prefix = f"GRAPHSYMPERF_GDSMITH_PROXY_{target.value.upper()}"
    uri = _required(env, f"{prefix}_URI")
    if target is TargetSystem.APACHE_AGE:
        host, port, user, password = _postgres_connection(uri)
    else:
        host, port = neo4j_uri_to_host_port(uri)
        user, password = "", ""
    return host, port, env.get(f"{prefix}_USER", user), env.get(f"{prefix}_PASSWORD", password)


def _postgres_connection(uri: str) -> tuple[str, int, str, str]:
    parsed = urlparse(uri)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError(f"invalid PostgreSQL URI for GDsmith: {uri!r}")
    return parsed.hostname, parsed.port or 5432, unquote(parsed.username or "postgres"), unquote(parsed.password or "")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"missing required generation environment variable {name}")
    return value
