from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Callable, Mapping

from graph_sym_perf.core import QueryCase, QueryFamily, TargetSystem, TransformConfig


class SafetyTier(str, Enum):
    CORE = "core"
    EXPERIMENTAL = "experimental"
    CONTROL = "control"
    REMOVED = "removed"


class RuleFamily(str, Enum):
    BOOL = "bool"
    SYM = "sym"


class RuleRole(str, Enum):
    SEMANTIC_EQUIVALENCE = "semantic_equivalence"
    OPTIMIZER_STRESS = "optimizer_stress"
    NOISE_CONTROL = "noise_control"
    RESEARCH_ONLY = "research_only"
    REMOVED = "removed"


TransformFactory = Callable[
    [QueryCase, TransformConfig | None, TargetSystem | None],
    QueryFamily,
]


@dataclass(frozen=True)
class RuleSpec:
    canonical_rule_id: str
    family: RuleFamily
    tier: SafetyTier
    submit_eligible: bool
    proof_id: str
    proof_version: str
    purity_requirements: tuple[str, ...]
    supported_dialects: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    allowed_site_classes: tuple[str, ...]
    implementation_rule_id: str
    transform_factory: TransformFactory = field(repr=False, compare=False)
    proof_theorem: str = ""
    role: RuleRole | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.canonical_rule_id,
                self.proof_id,
                self.proof_version,
                self.implementation_rule_id,
            )
        ):
            raise ValueError("rule identity and proof metadata are required")
        if not self.purity_requirements or not self.supported_dialects or not self.allowed_site_classes:
            raise ValueError("rule purity, dialect, and site scopes are required")
        if not callable(self.transform_factory):
            raise ValueError("rule transform factory must be callable")
        if self.submit_eligible is not (self.tier is SafetyTier.CORE):
            raise ValueError("only CORE rules are submit eligible")
        role = self.role
        if role is None:
            role = {
                SafetyTier.CONTROL: RuleRole.NOISE_CONTROL,
                SafetyTier.EXPERIMENTAL: RuleRole.RESEARCH_ONLY,
                SafetyTier.REMOVED: RuleRole.REMOVED,
            }.get(self.tier, RuleRole.SEMANTIC_EQUIVALENCE)
            object.__setattr__(self, "role", role)
        if self.tier is SafetyTier.CONTROL and role is not RuleRole.NOISE_CONTROL:
            raise ValueError("CONTROL rules must have the noise-control role")
        if self.tier is SafetyTier.REMOVED and role is not RuleRole.REMOVED:
            raise ValueError("REMOVED rules must have the removed role")
        if not self.proof_theorem:
            object.__setattr__(self, "proof_theorem", self.proof_id)

    @property
    def rule_id(self) -> str:
        return self.canonical_rule_id

    @property
    def version(self) -> str:
        return self.proof_version

    @property
    def dialects(self) -> tuple[str, ...]:
        return self.supported_dialects

    @property
    def site_classes(self) -> tuple[str, ...]:
        return self.allowed_site_classes


class RuleRegistry:
    def __init__(self, rules: tuple[RuleSpec, ...], aliases: Mapping[str, str] | None = None) -> None:
        canonical_ids = tuple(rule.canonical_rule_id for rule in rules)
        if len(set(canonical_ids)) != len(canonical_ids):
            raise ValueError("canonical rule ids must be unique")
        for rule in rules:
            if rule.tier is SafetyTier.CORE and not rule.submit_eligible:
                raise ValueError("CORE rules must be submit eligible")
            if rule.tier is SafetyTier.CONTROL and rule.submit_eligible:
                raise ValueError("CONTROL rules cannot be submit eligible")
        normalized_aliases = dict(aliases or {})
        missing_targets = sorted(set(normalized_aliases.values()) - set(canonical_ids))
        if missing_targets:
            raise ValueError(f"alias targets must be canonical rules: {missing_targets}")
        colliding_aliases = sorted(set(normalized_aliases) & set(canonical_ids))
        if colliding_aliases:
            raise ValueError(f"aliases cannot shadow canonical rules: {colliding_aliases}")
        self.rules = rules
        self._rules_by_id = MappingProxyType({rule.canonical_rule_id: rule for rule in rules})
        self._aliases = MappingProxyType(normalized_aliases)

    @classmethod
    def default(cls) -> "RuleRegistry":
        return cls(_bound_default_rules(), _ALIASES)

    def active_rules(
        self,
        *,
        dialect: str,
        tier: SafetyTier,
        capabilities: frozenset[str],
    ) -> tuple[RuleSpec, ...]:
        active_capabilities = set(capabilities)
        if "multiple_node_labels" in active_capabilities:
            active_capabilities.add("multi_label_intersection")
        return tuple(
            rule
            for rule in self.rules
            if rule.tier is tier
            and dialect in rule.supported_dialects
            and set(rule.required_capabilities) <= active_capabilities
        )

    def rules_for(
        self,
        dbms: str,
        tier: SafetyTier,
        capabilities: frozenset[str] | None = None,
    ) -> tuple[RuleSpec, ...]:
        dialect, profile_capabilities = _profile(dbms)
        active_capabilities = frozenset(profile_capabilities if capabilities is None else capabilities)
        return self.active_rules(
            dialect=dialect,
            tier=tier,
            capabilities=active_capabilities,
        )

    def resolve_id(self, rule_id: str) -> str:
        canonical_id = self._aliases.get(rule_id, rule_id)
        if canonical_id not in self._rules_by_id:
            raise KeyError(rule_id)
        return canonical_id

    def get(self, rule_id: str) -> RuleSpec:
        return self._rules_by_id[self.resolve_id(rule_id)]


_ALL_DIALECTS = ("cypher", "agensgraph", "apache_age")

_BOOL_IMPLEMENTATIONS = {
    "constant_true_disjunction": "and_true_or",
    "constant_false_conjunction": "or_false_and",
    "null_partition_tautology": "and_expr_null_partition",
    "null_contradiction": "or_expr_null_contradiction",
    "null_complete_excluded_middle": "and_excluded_middle_or_null",
}


def _family(rule_id: str) -> RuleFamily:
    if any(token in rule_id for token in ("symmetry", "order", "renaming", "pattern", "match")):
        return RuleFamily.SYM
    return RuleFamily.BOOL


def _unbound_transform_factory(
    query_case: QueryCase,
    config: TransformConfig | None,
    target: TargetSystem | None,
) -> QueryFamily:
    del query_case, config, target
    raise RuntimeError("default registry rules must bind a transform factory")


def _rule(
    rule_id: str,
    tier: SafetyTier,
    proof_id: str,
    theorem: str,
    *,
    purity: tuple[str, ...],
    sites: tuple[str, ...],
    role: RuleRole,
    dialects: tuple[str, ...] = _ALL_DIALECTS,
    capabilities: tuple[str, ...] = (),
) -> RuleSpec:
    return RuleSpec(
        canonical_rule_id=rule_id,
        family=_family(rule_id),
        tier=tier,
        submit_eligible=tier is SafetyTier.CORE,
        proof_id=proof_id,
        proof_version="1",
        purity_requirements=purity,
        supported_dialects=dialects,
        required_capabilities=capabilities,
        allowed_site_classes=sites,
        implementation_rule_id=_BOOL_IMPLEMENTATIONS.get(rule_id, rule_id),
        transform_factory=_unbound_transform_factory,
        proof_theorem=theorem,
        role=role,
    )


_BOOL_CORE = (
    _rule("constant_true_disjunction", SafetyTier.CORE, "kleene_or_true_v1", "P OR TRUE = TRUE", purity=("pure", "total", "low_cost"), sites=("where_boolean",), role=RuleRole.OPTIMIZER_STRESS),
    _rule("constant_false_conjunction", SafetyTier.CORE, "kleene_and_false_v1", "P AND FALSE = FALSE", purity=("pure", "total", "low_cost"), sites=("where_boolean",), role=RuleRole.OPTIMIZER_STRESS),
    _rule("null_partition_tautology", SafetyTier.CORE, "kleene_null_partition_v1", "(P IS NULL) OR (P IS NOT NULL) = TRUE", purity=("pure",), sites=("where_boolean",), role=RuleRole.SEMANTIC_EQUIVALENCE),
    _rule("null_contradiction", SafetyTier.CORE, "kleene_null_contradiction_v1", "(P IS NULL) AND (P IS NOT NULL) = FALSE", purity=("pure",), sites=("where_boolean",), role=RuleRole.SEMANTIC_EQUIVALENCE),
    _rule("null_complete_excluded_middle", SafetyTier.CORE, "three_valued_excluded_middle_v1", "P OR NOT P OR P IS NULL = TRUE", purity=("pure",), sites=("where_boolean",), role=RuleRole.SEMANTIC_EQUIVALENCE),
)

_SYM_CORE = (
    _rule("pattern_order_symmetry", SafetyTier.CORE, "graph_pattern_product_commutativity_v1", "constituent path-pattern conjunction commutes inside one MATCH", purity=("pure", "relationship_uniqueness_preserved"), sites=("match_pattern_component",), role=RuleRole.OPTIMIZER_STRESS),
    _rule("predicate_order_symmetry", SafetyTier.CORE, "kleene_conjunction_commutativity_v1", "pure total conjunction terms commute", purity=("pure", "total"), sites=("where_boolean",), role=RuleRole.OPTIMIZER_STRESS),
    _rule("node_order_symmetry", SafetyTier.CORE, "edge_endpoint_alpha_equivalence_v1", "unobserved endpoint traversal orientation preserves matches", purity=("pure", "same_graph_pattern"), sites=("match_pattern_component",), role=RuleRole.OPTIMIZER_STRESS),
    _rule("label_order_symmetry", SafetyTier.CORE, "label_conjunction_commutativity_v1", "multiple labels denote an unordered conjunction", purity=("pure",), sites=("match_pattern_component",), role=RuleRole.OPTIMIZER_STRESS, capabilities=("multi_label_intersection",)),
)

_CONTROL = (
    _rule("duplicate_predicate_control", SafetyTier.CONTROL, "idempotent_predicate_control_v1", "P AND P = P for selection", purity=("pure", "total"), sites=("where_boolean",), role=RuleRole.NOISE_CONTROL),
    _rule("edge_notation_symmetry", SafetyTier.CONTROL, "edge_parser_normalization_v1", "surface-equivalent edge notation normalizes identically", purity=("pure",), sites=("match_pattern_component",), role=RuleRole.NOISE_CONTROL),
    _rule("alpha_renaming_symmetry", SafetyTier.CONTROL, "bound_variable_alpha_equivalence_v1", "consistent renaming of bound variables is alpha-equivalent", purity=("pure", "capture_avoiding"), sites=("bound_variable",), role=RuleRole.NOISE_CONTROL),
)

_EXPERIMENTAL = (
    _rule("double_negation_predicate", SafetyTier.EXPERIMENTAL, "kleene_double_negation_v1", "NOT NOT P = P", purity=("pure",), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("de_morgan_predicate", SafetyTier.EXPERIMENTAL, "kleene_de_morgan_v1", "Kleene NOT distributes by De Morgan duality", purity=("pure", "total", "no_pattern_predicate"), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("case_true_experimental", SafetyTier.EXPERIMENTAL, "case_boolean_identity_v1", "CASE boolean normalization", purity=("pure", "total"), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("guarded_contradiction_experimental", SafetyTier.EXPERIMENTAL, "guarded_contradiction_v1", "guarded contradiction preserves the selected branch", purity=("pure", "total"), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("multilayer_redundancy_experimental", SafetyTier.EXPERIMENTAL, "multilayer_boolean_identity_v1", "nested identities preserve truth value", purity=("pure", "total"), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("numeric_equality_range", SafetyTier.EXPERIMENTAL, "finite_numeric_equality_range_v1", "x=c iff x>=c and x<=c over same finite numeric type", purity=("pure", "finite_numeric", "no_implicit_cast"), sites=("where_boolean",), role=RuleRole.RESEARCH_ONLY),
    _rule("path_endpoint_symmetry", SafetyTier.EXPERIMENTAL, "path_endpoint_multiplicity_pending", "endpoint reversal under hidden path values", purity=("pure", "path_values_unobserved"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("pattern_decomposition_symmetry", SafetyTier.EXPERIMENTAL, "relationship_uniqueness_pending", "pattern decomposition preserves relationship uniqueness scope", purity=("pure", "relationship_uniqueness_proof_required"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("pattern_fusion_symmetry", SafetyTier.EXPERIMENTAL, "relationship_uniqueness_pending", "pattern fusion preserves relationship uniqueness scope", purity=("pure", "relationship_uniqueness_proof_required"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("property_map_where_symmetry", SafetyTier.EXPERIMENTAL, "property_map_selection_pending", "static property map is a selection predicate", purity=("pure", "ordinary_match", "single_static_key"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("label_where_symmetry", SafetyTier.EXPERIMENTAL, "label_predicate_pending", "single pattern label is equivalent to a label predicate", purity=("pure", "single_label"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("single_hop_varlength_symmetry", SafetyTier.EXPERIMENTAL, "single_hop_varlength_pending", "hidden [:T*1..1] and [:T] preserve multiplicity", purity=("pure", "path_values_unobserved"), sites=("match_pattern_component",), role=RuleRole.RESEARCH_ONLY),
    _rule("independent_mandatory_match_reorder", SafetyTier.EXPERIMENTAL, "mandatory_match_dependency_pending", "consecutive dependency-free mandatory MATCH clauses commute", purity=("pure", "dependency_graph_independent", "where_attachment_preserved"), sites=("mandatory_match_clause",), role=RuleRole.RESEARCH_ONLY),
)

_REMOVED_IDS = (
    "and_not_expr_null_contradiction",
    "and_not_false_and",
    "and_double_negated_true_or",
    "and_null_guarded_true",
    "and_distributive_absorption",
    "property_map_order_symmetry",
    "exploratory_pattern_order_symmetry",
    "exploratory_predicate_order_symmetry",
    "exploratory_optional_match_order_symmetry",
    "exploratory_with_filter_shift_symmetry",
    "exploratory_projection_reorder_symmetry",
)
_REMOVED = tuple(
    _rule(rule_id, SafetyTier.REMOVED, "removed_by_2026_07_audit", "not part of the executable semantic space", purity=("not_executable",), sites=("removed",), role=RuleRole.REMOVED)
    for rule_id in _REMOVED_IDS
)

_AUDITED_RULES = _BOOL_CORE + _SYM_CORE + _CONTROL + _EXPERIMENTAL + _REMOVED

_ALIASES = {"and_excluded_middle_or_null": "null_complete_excluded_middle"}


@lru_cache(maxsize=1)
def _bound_default_rules() -> tuple[RuleSpec, ...]:
    from graph_sym_perf.transforms.bool_redundancy import generate_bool_redundancy_family
    from graph_sym_perf.transforms.symmetry import generate_symmetry_family

    factories = {
        RuleFamily.BOOL: generate_bool_redundancy_family,
        RuleFamily.SYM: generate_symmetry_family,
    }
    return tuple(
        replace(rule, transform_factory=factories[rule.family])
        for rule in _AUDITED_RULES
    )


def _profile(dbms: str) -> tuple[str, set[str]]:
    if dbms == "agensgraph":
        return "agensgraph", {"single_node_label"}
    if dbms == "apache_age":
        return "apache_age", {"single_node_label", "explicit_result_schema"}
    if dbms in {"arcadedb", "ladybugdb"}:
        return "cypher", {"single_node_label"}
    if dbms not in {"neo4j", "memgraph", "falkordb"}:
        raise ValueError(f"unknown DBMS capability profile: {dbms}")
    return "cypher", {"multiple_node_labels", "multi_label_intersection"}


__all__ = [
    "RuleFamily",
    "RuleRegistry",
    "RuleRole",
    "RuleSpec",
    "SafetyTier",
    "TransformFactory",
]
