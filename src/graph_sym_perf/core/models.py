"""JSON-friendly immutable data contracts for GraphSymPerf."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from graph_sym_perf.core.capabilities import EngineCapabilities
from graph_sym_perf.core.status import ClaimLevel, DeveloperStatus, ToolSupportLevel

if TYPE_CHECKING:
    from graph_sym_perf.coverage import PMCEvidence


class TargetSystem(str, Enum):
    """Supported graph database targets."""

    NEO4J = "neo4j"
    MEMGRAPH = "memgraph"
    FALKORDB = "falkordb"
    AGENSGRAPH = "agensgraph"
    APACHE_AGE = "apache_age"
    ARCADEDB = "arcadedb"
    KUZU = "kuzu"

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
    kind: str = "ldbc_snb_mini"
    dataset_id: str = "graph_sym_perf_ldbc_mini"
    seed: int = 20260513
    person_count: int = 8
    post_count: int = 6
    forum_count: int = 2
    tag_count: int = 4
    allow_load_env: str = "GRAPHSYMPERF_ALLOW_LOAD"


@dataclass(frozen=True)
class GdsmithConfig:
    jar_env: str = "GRAPHSYMPERF_GDSMITH_JAR"
    algorithm: str = "compared3"
    num_tries: int = 1
    num_queries: int = 20
    random_seed: int = 20260513
    allow_destructive_env: str = "GRAPHSYMPERF_GDSMITH_ALLOW_DESTRUCTIVE"
    no_proxy_launcher_env: str = "GRAPHSYMPERF_GDSMITH_NO_PROXY_LAUNCHER"
    backend_extra_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    timeout_seconds: float = 180.0


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
        "node_order_symmetry",
        "edge_notation_symmetry",
        "predicate_order_symmetry",
        "pattern_order_symmetry",
        "pattern_decomposition_symmetry",
        "pattern_fusion_symmetry",
        "path_endpoint_symmetry",
        "label_order_symmetry",
        "property_map_order_symmetry",
        "alpha_renaming_symmetry",
        "exploratory_pattern_order_symmetry",
        "exploratory_predicate_order_symmetry",
        "exploratory_optional_match_order_symmetry",
        "exploratory_with_filter_shift_symmetry",
        "exploratory_projection_reorder_symmetry",
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
class OracleConfig:
    enable_structural_diff: bool = True
    enable_profile_metrics: bool = True


@dataclass(frozen=True)
class CoverageConfig:
    enabled: bool = True
    include_transformation_context: bool = True
    write_per_case_evidence: bool = True
    ratio_buckets: tuple[float, ...] = (1.5, 2.0, 5.0, 10.0, 50.0, 100.0, 1000.0)
    # Per-metric minimum ratio thresholds for cardinality/work PMC units.
    # Metrics below threshold are excluded from coverage to reduce noise.
    metric_thresholds: Mapping[str, float] = field(
        default_factory=lambda: {
            "rows": 1.5,
            "db_hits": 1.5,
            "estimated_rows": 2.0,
            "memory_bytes": 1.5,
            "page_cache_misses": 1.5,
        }
    )
    novelty_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "structural": 0.5,
            "operator_delta": 1.0,
            "access_delta": 2.0,
            "expand_delta": 1.5,
            "join_delta": 2.0,
            "predicate_delta": 2.0,
            "barrier_delta": 1.5,
            "cardinality_delta": 1.5,
            "work_delta": 2.0,
            "transformation_context": 0.2,
        }
    )


@dataclass(frozen=True)
class FeedbackConfig:
    enabled: bool = False
    exploration_rate: float = 0.15
    seed: int = 20260522
    min_rules_per_case: int = 1
    max_rules_per_case: int | None = None
    write_artifacts: bool = True
    reward_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "generated": 1.0,
            "equivalence_passed": 2.0,
            "profile_available": 3.0,
            "candidate_count": 5.0,
            "strong_candidate_count": 8.0,
            "pmc_new_units": 1.5,
            "pmc_units": 0.2,
            "equivalence_failed": -2.0,
            "timeout": -3.0,
            "error": -2.0,
            "skipped": -1.0,
        }
    )


@dataclass(frozen=True)
class ClusteringConfig:
    enabled: bool = True
    write_signatures: bool = True
    representative_policy: str = "support_then_ratio_then_size"
    include_plan_only: bool = True
    include_exploratory: bool = False


@dataclass(frozen=True)
class RunConfig:
    source_path: str
    paths: RunPaths
    systems: tuple[SystemConfig, ...]
    dataset: DatasetConfig
    query_generation: QueryGenerationConfig
    equivalence: EquivalenceConfig
    confirmation: ConfirmationConfig
    oracle: OracleConfig = field(default_factory=OracleConfig)
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
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
    source: str = "ldbc_mini"
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
    support_level: ToolSupportLevel | str = ToolSupportLevel.PLAN_ONLY
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
    pmc: PMCEvidence | None = None
    exploratory_pmc: PMCEvidence | None = None
    # Per-variant PMC evidences (bool_redundancy pairwise strategy).  Observed
    # for coverage but not used for case-level candidate state.
    _per_variant_pmcs: tuple[Any, ...] = ()

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
    plan_bug_kind: str = "unknown"
    support_level: ToolSupportLevel | str = ToolSupportLevel.PLAN_ONLY
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


def to_jsonable(value: Any) -> Any:
    """Convert project objects and common driver values to JSON-safe values."""

    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(to_jsonable(item) for item in value)
    if hasattr(value, "labels") and hasattr(value, "items"):
        return {
            "__graph_type__": "node",
            "element_id": str(getattr(value, "element_id", getattr(value, "id", ""))),
            "labels": sorted(str(label) for label in getattr(value, "labels", ())),
            "properties": {str(key): to_jsonable(item) for key, item in dict(value.items()).items()},
        }
    if hasattr(value, "type") and hasattr(value, "items") and (
        hasattr(value, "start_node") or hasattr(value, "start_node_element_id")
    ):
        return {
            "__graph_type__": "relationship",
            "element_id": str(getattr(value, "element_id", getattr(value, "id", ""))),
            "type": str(getattr(value, "type", "")),
            "start": str(getattr(value, "start_node_element_id", getattr(getattr(value, "start_node", None), "element_id", ""))),
            "end": str(getattr(value, "end_node_element_id", getattr(getattr(value, "end_node", None), "element_id", ""))),
            "properties": {str(key): to_jsonable(item) for key, item in dict(value.items()).items()},
        }
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "__graph_type__": "path",
            "nodes": tuple(to_jsonable(node) for node in getattr(value, "nodes", ())),
            "relationships": tuple(to_jsonable(rel) for rel in getattr(value, "relationships", ())),
        }
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if hasattr(value, "items"):
        try:
            return {str(key): to_jsonable(item) for key, item in value.items()}
        except Exception:
            pass
    return value


def stable_json_dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
