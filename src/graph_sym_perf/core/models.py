"""JSON-friendly immutable data contracts for GraphSymPerf."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from graph_sym_perf.core.capabilities import EngineCapabilities
from graph_sym_perf.core.status import ClaimLevel, DeveloperStatus, ToolSupportLevel


class TypedValueKind(str, Enum):
    NODE = "node"
    RELATIONSHIP = "relationship"
    PATH = "path"
    DATE = "date"
    DATETIME = "datetime"
    BYTES = "bytes"


class MissingValue(str, Enum):
    VALUE = "missing"


@dataclass(frozen=True)
class TypedNode:
    element_id: str
    labels: tuple[str, ...]
    properties: tuple[tuple[str, object], ...]
    kind: TypedValueKind = field(default=TypedValueKind.NODE, init=False)


@dataclass(frozen=True)
class TypedRelationship:
    element_id: str
    relationship_type: str
    start_element_id: str
    end_element_id: str
    properties: tuple[tuple[str, object], ...]
    kind: TypedValueKind = field(default=TypedValueKind.RELATIONSHIP, init=False)


@dataclass(frozen=True)
class TypedPath:
    nodes: tuple[TypedNode, ...]
    relationships: tuple[TypedRelationship, ...]
    kind: TypedValueKind = field(default=TypedValueKind.PATH, init=False)


@dataclass(frozen=True)
class TypedTemporal:
    kind: TypedValueKind
    iso_value: str

    def __post_init__(self) -> None:
        if self.kind not in {TypedValueKind.DATE, TypedValueKind.DATETIME}:
            raise ValueError("TypedTemporal kind must be date or datetime")


@dataclass(frozen=True)
class TypedBytes:
    value: bytes
    kind: TypedValueKind = field(default=TypedValueKind.BYTES, init=False)

class TargetSystem(str, Enum):
    """Supported graph database targets."""

    NEO4J = "neo4j"
    MEMGRAPH = "memgraph"
    FALKORDB = "falkordb"
    AGENSGRAPH = "agensgraph"
    APACHE_AGE = "apache_age"
    ARCADEDB = "arcadedb"
    LADYBUGDB = "ladybugdb"

    @classmethod
    def parse(cls, value: str) -> "TargetSystem":
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        allowed = ", ".join(member.value for member in cls)
        raise ValueError(f"unknown target system {value!r}; expected one of: {allowed}")


@dataclass(frozen=True)
class RunPaths:
    artifact_dir: str = "logs/{run_id}"

    def artifact_path(self, run_id: str) -> Path:
        return Path(self.artifact_dir.format(run_id=run_id))


@dataclass(frozen=True)
class SystemConfig:
    target: TargetSystem
    required_env: tuple[str, ...]
    optional_env: Mapping[str, str]
    settings: Mapping[str, Any]
    capabilities: EngineCapabilities = field(default_factory=EngineCapabilities)


@dataclass(frozen=True)
class DatasetConfig:
    kind: str = "neutral_gdsmith_graph"
    dataset_id: str = "graph_sym_perf_neutral_graph"
    seed: int = 20260513
    person_count: int = 8
    post_count: int = 6
    forum_count: int = 2
    tag_count: int = 4
    allow_load_env: str = "GRAPHSYMPERF_ALLOW_LOAD"


@dataclass(frozen=True)
class GdsmithConfig:
    jar_env: str = "GRAPHSYMPERF_GDSMITH_JAR"
    jar_sha256_env: str = "GRAPHSYMPERF_GDSMITH_JAR_SHA256"
    expected_source_commit: str | None = None
    algorithm: str = "compared3"
    num_tries: int = 1
    num_queries: int = 20
    random_seed: int = 20260513
    allow_destructive_env: str = "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE"
    no_proxy_launcher_env: str = "GRAPHSYMPERF_GDSMITH_NO_PROXY_LAUNCHER"
    backend_extra_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    timeout_seconds: float = 180.0
    stall_timeout_seconds: float = 120.0
    max_raw_artifact_bytes: int | None = None


@dataclass(frozen=True)
class QueryGenerationConfig:
    max_cases: int = 500
    result_limit: int = 100
    include_variable_length: bool = False
    source: str = "gdsmith"
    gdsmith: GdsmithConfig = field(default_factory=GdsmithConfig)


@dataclass(frozen=True)
class TransformConfig:
    enabled_families: tuple[str, ...] = ("symmetry", "bool_redundancy")
    enabled_rules: tuple[str, ...] = (
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
        "node_order_symmetry",
        "predicate_order_symmetry",
        "pattern_order_symmetry",
        "label_order_symmetry",
    )
    max_expr_complexity: str = "medium"
    gdsmith_safe_only: bool = True
    max_variants_per_case: int | None = None


@dataclass(frozen=True)
class EquivalenceConfig:
    max_result_rows: int = 1000
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ConfirmationConfig:
    warmup: int = 1
    repeats: int = 5
    timeout_seconds: float = 30.0
    min_latency_ratio: float = 10.0
    min_latency_delta_ms: float = 50.0
    min_db_hits_ratio: float = 5.0
    min_rows_ratio: float = 5.0


@dataclass(frozen=True)
class RunConfig:
    source_path: str
    paths: RunPaths
    systems: tuple[SystemConfig, ...]
    dataset: DatasetConfig
    query_generation: QueryGenerationConfig
    equivalence: EquivalenceConfig
    confirmation: ConfirmationConfig
    transforms: TransformConfig = field(default_factory=TransformConfig)

    def system_config(self, target: TargetSystem) -> SystemConfig | None:
        for system in self.systems:
            if system.target == target:
                return system
        return None


@dataclass(frozen=True)
class SystemStatus:
    system: TargetSystem
    state: str
    dependency_ok: bool
    environment_ok: bool
    connectivity_ok: bool
    reason: str
    missing_env: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class QueryCase:
    case_id: str
    template_name: str
    query: str
    parameters: Mapping[str, Any]
    base_predicate: str
    safe_expression: str
    source: str = "gdsmith"
    generator_case_id: str | None = None
    raw_query: str | None = None
    skip_reason: str | None = None
    transform_family: str | None = None
    transform_rule: str | None = None
    target_system: str | None = None
    setup_path: str | None = None
    setup_attachment_url: str | None = None
    setup_attachment_name: str | None = None
    expected_nodes: int | None = None
    expected_relationships: int | None = None
    developer_status: str | None = None
    issue_url: str | None = None


@dataclass(frozen=True)
class ExprNode:
    kind: str
    rendered_cypher: str
    referenced_variables: tuple[str, ...] = ()
    children: tuple[Any, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PropertyRef:
    variable: str
    property_name: str
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class FunctionCall:
    function_name: str
    arguments: tuple[Any, ...]
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class Comparison:
    left: Any
    operator: str
    right: Any
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class NullCheck:
    expression: Any
    is_null: bool
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class InList:
    expression: Any
    values: tuple[Any, ...]
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class LogicalExpr:
    operator: str
    children: tuple[Any, ...]
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class CaseExpr:
    condition: Any
    when_true: str
    when_false: str
    rendered_cypher: str
    referenced_variables: tuple[str, ...]


@dataclass(frozen=True)
class ExpressionRecord:
    expr_id: str
    family: str
    complexity: str
    risk_level: str
    referenced_variables: tuple[str, ...]
    rendered_cypher: str
    proof_note: str
    ast: Any | None = None


@dataclass(frozen=True)
class InjectionSite:
    clause_kind: str
    start: int
    end: int
    has_where: bool
    bound_variables: tuple[str, ...]
    selected_variable: str | None
    base_predicate: str
    skip_reason: str | None = None


@dataclass(frozen=True)
class QueryPair:
    case_id: str
    original_query: str
    transformed_query: str
    parameters: Mapping[str, Any]
    transform_rule: str
    proof_obligation: str
    expression_record: ExpressionRecord | None = None
    injection_site: InjectionSite | None = None
    transform_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryVariant:
    variant_id: str
    query: str
    transform_rule: str
    proof_obligation: str
    static_safe: bool = True
    safety_tier: str = "strict"
    skip_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryFamily:
    case_id: str
    seed_query: str
    parameters: Mapping[str, Any]
    variants: tuple[QueryVariant, ...]
    skipped_variants: tuple[QueryVariant, ...] = ()
    source: str = "gdsmith"
    generator_case_id: str | None = None
    raw_query: str | None = None


SymmetryVariant = QueryVariant
SymmetryFamily = QueryFamily


@dataclass(frozen=True)
class VariantProfileSummary:
    variant_id: str
    query: str
    query_kind: str
    median_ms: float | None
    samples_ms: tuple[float, ...]
    plan: "PlanRecord | None"
    profile_plan: "NormalizedPlan | None"
    metrics: Mapping[str, float]
    available: bool = True
    reason: str = "OK"


@dataclass(frozen=True)
class ExecutionResult:
    system: TargetSystem
    query_kind: str
    rows: tuple[Mapping[str, Any], ...]
    elapsed_ms: float
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class EquivalenceRecord:
    case_id: str
    passed: bool
    left_hash: str | None
    right_hash: str | None
    left_count: int
    right_count: int
    reason: str
    sample_left: tuple[Mapping[str, Any], ...] = ()
    sample_right: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class PlanNode:
    path: str
    operator_type: str
    operator_family: str
    identifiers: tuple[str, ...] = ()
    details: str = ""
    estimated_rows: float | None = None
    actual_rows: float | None = None
    db_hits: float | None = None
    page_cache_hits: float | None = None
    page_cache_misses: float | None = None
    memory_bytes: float | None = None
    access_kind: str | None = None
    access_variable: str | None = None
    access_label: str | None = None
    relationship_type: str | None = None
    expand_direction: str | None = None
    predicate: str | None = None
    barrier_kind: str | None = None
    children: tuple["PlanNode", ...] = ()


@dataclass(frozen=True)
class NormalizedPlan:
    root: PlanNode | None
    nodes: tuple[PlanNode, ...]
    structural_hash: str
    operator_skeleton: tuple[str, ...]
    access_points: tuple[Mapping[str, Any], ...] = ()
    predicate_attachments: tuple[Mapping[str, Any], ...] = ()
    expand_chains: tuple[Mapping[str, Any], ...] = ()
    barriers: tuple[Mapping[str, Any], ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricDelta:
    metric: str
    original: float | None
    transformed: float | None
    ratio: float | None
    delta: float | None
    worsened: bool


@dataclass(frozen=True)
class PlanDiff:
    structural_hash_changed: bool
    changes: tuple[Mapping[str, Any], ...]
    added_nodes: tuple[Mapping[str, Any], ...]
    removed_nodes: tuple[Mapping[str, Any], ...]
    replaced_nodes: tuple[Mapping[str, Any], ...]
    family_delta: Mapping[str, int]
    access_point_delta: Mapping[str, Any]
    expand_chain_delta: Mapping[str, Any]
    barrier_delta: Mapping[str, Any]
    estimated_rows_delta: Mapping[str, Any]
    metric_deltas: tuple[MetricDelta, ...] = ()


@dataclass(frozen=True)
class ProfileRecord:
    system: TargetSystem
    query_kind: str
    elapsed_ms: float
    plan: NormalizedPlan | None
    metrics: Mapping[str, float]
    available: bool = True


@dataclass(frozen=True)
class PlanRecord:
    system: TargetSystem
    query_kind: str
    raw_plan: Any
    raw_text: str
    operator_families: tuple[str, ...]
    available: bool = True
    normalized_plan: NormalizedPlan | None = None
    structural_hash: str | None = None
    operator_skeleton: tuple[str, ...] = ()
    access_points: tuple[Mapping[str, Any], ...] = ()
    predicate_attachments: tuple[Mapping[str, Any], ...] = ()
    expand_chains: tuple[Mapping[str, Any], ...] = ()
    barriers: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class OracleRecord:
    case_id: str
    is_candidate: bool
    candidate_types: tuple[str, ...]
    reason: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ConfirmationRecord:
    case_id: str
    confirmed: bool
    original_median_ms: float | None
    transformed_median_ms: float | None
    latency_ratio: float | None
    latency_delta_ms: float | None
    original_samples_ms: tuple[float, ...]
    transformed_samples_ms: tuple[float, ...]
    reason: str
    level: str = "none"
    support_level: ToolSupportLevel | str = ToolSupportLevel.NONE
    developer_status: DeveloperStatus | str = DeveloperStatus.UNKNOWN
    claim_level: ClaimLevel | str = ClaimLevel.CANDIDATE
    metric_deltas: tuple[MetricDelta, ...] = ()
    profile_samples: Mapping[str, Any] = field(default_factory=dict)
    profile_diff_artifact: str | None = None
    fast_variant_id: str | None = None
    slow_variant_id: str | None = None


@dataclass(frozen=True)
class CaseRecord:
    system: TargetSystem
    case_id: str
    family: SymmetryFamily
    equivalences: tuple[EquivalenceRecord, ...]
    oracle: OracleRecord | None
    confirmation: ConfirmationRecord | None
    artifacts: Mapping[str, str]
    state: str
    reason: str
    developer_status: DeveloperStatus | str = DeveloperStatus.UNKNOWN
    claim_level: ClaimLevel | str = ClaimLevel.CANDIDATE
    exploratory_oracle: OracleRecord | None = None
    exploratory_confirmation: ConfirmationRecord | None = None

    @property
    def equivalence(self) -> EquivalenceRecord:
        for record in self.equivalences:
            if record.passed:
                return record
        return self.equivalences[0] if self.equivalences else EquivalenceRecord(
            case_id=self.case_id,
            passed=False,
            left_hash=None,
            right_hash=None,
            left_count=0,
            right_count=0,
            reason="NO_VARIANTS",
        )


@dataclass(frozen=True)
class ClusterRecord:
    cluster_id: str
    cluster_level: str
    key: Mapping[str, Any]
    candidate_types: tuple[str, ...]
    representative_case_id: str
    case_ids: tuple[str, ...]
    size: int
    claim_level: ClaimLevel | str
    evidence_summary: Mapping[str, Any]
    expression_family_counts: Mapping[str, int]
    transform_rule_counts: Mapping[str, int]
    confirmation_level_counts: Mapping[str, int]
    support_level: ToolSupportLevel | str = ToolSupportLevel.NONE
    support_level_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusteredSummary:
    run_id: str
    candidate_count: int
    issue_family_count: int
    unique_issue_count: int
    issue_families: tuple[ClusterRecord, ...]
    unique_issues: tuple[ClusterRecord, ...]
    generation_metadata: Mapping[str, Any]


def to_typed_value(value: Any) -> Any:
    """Preserve driver value types for in-memory equivalence checks."""

    if isinstance(value, TypedNode | TypedRelationship | TypedPath | TypedTemporal | TypedBytes | MissingValue):
        return value
    if isinstance(value, datetime):
        return TypedTemporal(TypedValueKind.DATETIME, value.isoformat())
    if isinstance(value, date):
        return TypedTemporal(TypedValueKind.DATE, value.isoformat())
    if isinstance(value, bytes):
        return TypedBytes(value)
    if hasattr(value, "type") and hasattr(value, "items") and (
        hasattr(value, "start_node") or hasattr(value, "start_node_element_id")
    ):
        return TypedRelationship(
            element_id=str(getattr(value, "element_id", getattr(value, "id", ""))),
            relationship_type=str(getattr(value, "type", "")),
            start_element_id=str(getattr(value, "start_node_element_id", getattr(getattr(value, "start_node", None), "element_id", ""))),
            end_element_id=str(getattr(value, "end_node_element_id", getattr(getattr(value, "end_node", None), "element_id", ""))),
            properties=tuple(sorted((str(key), to_typed_value(item)) for key, item in dict(value.items()).items())),
        )
    if hasattr(value, "labels") and hasattr(value, "items"):
        return TypedNode(
            element_id=str(getattr(value, "element_id", getattr(value, "id", ""))),
            labels=tuple(sorted(str(label) for label in getattr(value, "labels", ()))),
            properties=tuple(sorted((str(key), to_typed_value(item)) for key, item in dict(value.items()).items())),
        )
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        nodes = tuple(to_typed_value(node) for node in getattr(value, "nodes", ()))
        relationships = tuple(to_typed_value(rel) for rel in getattr(value, "relationships", ()))
        if not all(isinstance(node, TypedNode) for node in nodes) or not all(isinstance(rel, TypedRelationship) for rel in relationships):
            raise TypeError("path contains unsupported node or relationship values")
        return TypedPath(nodes=nodes, relationships=relationships)
    if isinstance(value, Mapping):
        return {str(key): to_typed_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(to_typed_value(item) for item in value)
    if isinstance(value, list):
        return [to_typed_value(item) for item in value]
    return value


def to_artifact_jsonable(value: Any) -> Any:
    """Encode values for JSON artifacts; this is not an equivalence representation."""

    if isinstance(value, TypedNode):
        return {
            "__graph_type__": value.kind.value,
            "element_id": value.element_id,
            "labels": list(value.labels),
            "properties": {key: to_artifact_jsonable(item) for key, item in value.properties},
        }
    if isinstance(value, TypedRelationship):
        return {
            "__graph_type__": value.kind.value,
            "element_id": value.element_id,
            "type": value.relationship_type,
            "start": value.start_element_id,
            "end": value.end_element_id,
            "properties": {key: to_artifact_jsonable(item) for key, item in value.properties},
        }
    if isinstance(value, TypedPath):
        return {
            "__graph_type__": value.kind.value,
            "nodes": [to_artifact_jsonable(node) for node in value.nodes],
            "relationships": [to_artifact_jsonable(rel) for rel in value.relationships],
        }
    if isinstance(value, TypedTemporal):
        return {"__typed_value__": value.kind.value, "value": value.iso_value}
    if isinstance(value, TypedBytes):
        return {"__typed_value__": value.kind.value, "value": value.value.hex()}
    if isinstance(value, MissingValue):
        return {"__typed_value__": "missing"}

    if is_dataclass(value):
        return {field.name: to_artifact_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return to_artifact_jsonable(to_typed_value(value))
    if isinstance(value, date):
        return to_artifact_jsonable(to_typed_value(value))
    if isinstance(value, bytes):
        return to_artifact_jsonable(to_typed_value(value))
    if hasattr(value, "type") and hasattr(value, "items") and (
        hasattr(value, "start_node") or hasattr(value, "start_node_element_id")
    ):
        return {
            "__graph_type__": "relationship",
            "element_id": str(getattr(value, "element_id", getattr(value, "id", ""))),
            "type": str(getattr(value, "type", "")),
            "start": str(getattr(value, "start_node_element_id", getattr(getattr(value, "start_node", None), "element_id", ""))),
            "end": str(getattr(value, "end_node_element_id", getattr(getattr(value, "end_node", None), "element_id", ""))),
            "properties": {str(key): to_artifact_jsonable(item) for key, item in dict(value.items()).items()},
        }
    if hasattr(value, "labels") and hasattr(value, "items"):
        return {
            "__graph_type__": "node",
            "element_id": str(getattr(value, "element_id", getattr(value, "id", ""))),
            "labels": sorted(str(label) for label in getattr(value, "labels", ())),
            "properties": {str(key): to_artifact_jsonable(item) for key, item in dict(value.items()).items()},
        }
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "__graph_type__": "path",
            "nodes": tuple(to_artifact_jsonable(node) for node in getattr(value, "nodes", ())),
            "relationships": tuple(to_artifact_jsonable(rel) for rel in getattr(value, "relationships", ())),
        }
    if isinstance(value, Mapping):
        return {str(key): to_artifact_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [to_artifact_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(to_artifact_jsonable(item) for item in value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if hasattr(value, "items"):
        try:
            return {str(key): to_artifact_jsonable(item) for key, item in value.items()}
        except Exception:
            pass
    return value


def to_jsonable(value: Any) -> Any:
    """Backward-compatible artifact encoder."""

    return to_artifact_jsonable(value)


def stable_json_dumps(value: Any) -> str:
    return json.dumps(to_artifact_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
