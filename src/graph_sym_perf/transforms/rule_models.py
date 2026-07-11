from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetyTier(str, Enum):
    CORE = "core"
    EXPERIMENTAL = "experimental"
    CONTROL = "control"
    REMOVED = "removed"


class RuleRole(str, Enum):
    SEMANTIC_EQUIVALENCE = "semantic_equivalence"
    OPTIMIZER_STRESS = "optimizer_stress"
    NOISE_CONTROL = "noise_control"
    RESEARCH_ONLY = "research_only"
    REMOVED = "removed"


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    version: str
    tier: SafetyTier
    proof_id: str
    proof_theorem: str
    purity_requirements: tuple[str, ...]
    dialects: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    site_classes: tuple[str, ...]
    submit_eligible: bool
    role: RuleRole

    def __post_init__(self) -> None:
        if not all((self.rule_id, self.version, self.proof_id, self.proof_theorem)):
            raise ValueError("rule identity and proof metadata are required")
        if not self.purity_requirements or not self.dialects or not self.site_classes:
            raise ValueError("rule purity, dialect, and site scopes are required")
        if self.submit_eligible is not (self.tier is SafetyTier.CORE):
            raise ValueError("only CORE rules are submit eligible")
        if self.tier is SafetyTier.CONTROL and self.role is not RuleRole.NOISE_CONTROL:
            raise ValueError("CONTROL rules must have the noise-control role")
        if self.tier is SafetyTier.REMOVED and self.role is not RuleRole.REMOVED:
            raise ValueError("REMOVED rules must have the removed role")


class RuleRegistry:
    def __init__(self, rules: tuple[RuleSpec, ...]) -> None:
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("rule ids must be unique")
        self.rules = rules

    @classmethod
    def default(cls) -> "RuleRegistry":
        return cls(_AUDITED_RULES)

    def rules_for(
        self,
        dbms: str,
        tier: SafetyTier,
        capabilities: frozenset[str] | None = None,
    ) -> tuple[RuleSpec, ...]:
        dialect, profile_capabilities = _profile(dbms)
        active_capabilities = profile_capabilities if capabilities is None else set(capabilities)
        return tuple(
            rule
            for rule in self.rules
            if rule.tier is tier
            and dialect in rule.dialects
            and set(rule.required_capabilities) <= active_capabilities
        )

    def get(self, rule_id: str) -> RuleSpec:
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise KeyError(rule_id)


_ALL_DIALECTS = ("cypher", "agensgraph", "apache_age")


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
        rule_id=rule_id,
        version="1",
        tier=tier,
        proof_id=proof_id,
        proof_theorem=theorem,
        purity_requirements=purity,
        dialects=dialects,
        required_capabilities=capabilities,
        site_classes=sites,
        submit_eligible=tier is SafetyTier.CORE,
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
    _rule("label_order_symmetry", SafetyTier.CORE, "label_conjunction_commutativity_v1", "multiple labels denote an unordered conjunction", purity=("pure",), sites=("match_pattern_component",), role=RuleRole.OPTIMIZER_STRESS, capabilities=("multiple_node_labels",)),
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


def _profile(dbms: str) -> tuple[str, set[str]]:
    if dbms == "agensgraph":
        return "agensgraph", {"single_node_label"}
    if dbms == "apache_age":
        return "apache_age", {"single_node_label", "explicit_result_schema"}
    if dbms in {"arcadedb", "ladybugdb"}:
        return "cypher", {"single_node_label"}
    if dbms not in {"neo4j", "memgraph", "falkordb"}:
        raise ValueError(f"unknown DBMS capability profile: {dbms}")
    return "cypher", {"multiple_node_labels"}


__all__ = ["RuleRegistry", "RuleRole", "RuleSpec", "SafetyTier"]
