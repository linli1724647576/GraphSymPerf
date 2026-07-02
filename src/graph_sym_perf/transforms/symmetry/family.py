"""Conservative Cypher symmetry transformations for GDsmith queries."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from graph_sym_perf.core import QueryCase, SymmetryFamily, SymmetryVariant, TransformConfig
from graph_sym_perf.cypher.ast import (
    EdgePattern as AstEdgePattern,
    MatchClause as AstMatchClause,
    NodePattern as AstNodePattern,
    PatternList as AstPatternList,
    VarLengthPathPattern as AstVarLengthPathPattern,
)
from graph_sym_perf.cypher.parser import CypherParseError, parse_and_predicate, parse_match_clause, parse_query, split_top_level_commas
from graph_sym_perf.cypher.proof import (
    ProofFailure,
    SemanticProof,
    prove_alpha_renaming as prove_alpha_renaming_ra,
    prove_edge_notation as prove_edge_notation_ra,
    prove_label_order as prove_label_order_ra,
    prove_node_order as prove_node_order_ra,
    prove_pattern_list_order as prove_pattern_list_order_ra,
    prove_path_endpoint as prove_path_endpoint_ra,
    prove_pattern_equivalence as prove_pattern_equivalence_ra,
    prove_property_map_order as prove_property_map_order_ra,
    prove_pattern_order as prove_pattern_order_ra,
    prove_predicate_order as prove_predicate_order_ra,
)


class TransformSkipError(ValueError):
    """Raised when a transformation rule is unsafe for a query."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Rule:
    name: str
    apply: Callable[[str], SymmetryVariant]


@dataclass(frozen=True)
class _ProvedReplacement:
    clause: object
    region: object
    replacement: str
    proof: SemanticProof
    metadata: dict[str, object]


WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH)\b", re.I)
UNSAFE_SCOPE_RE = re.compile(r"\b(CALL|EXISTS)\b", re.I)
CLAUSE_RE = re.compile(
    r"\b(OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN|CALL|CREATE|MERGE|DELETE|SET|REMOVE)\b",
    re.I,
)

RULE_SPACE_METADATA: dict[str, dict[str, str]] = {
    "predicate_order_symmetry": {
        "rule_space_id": "S1",
        "rule_type": "predicate_commutativity",
        "structural_level": "expression_tree",
        "optimizer_target": "logical_reorder",
    },
    "pattern_order_symmetry": {
        "rule_space_id": "S2",
        "rule_type": "clause_reordering",
        "structural_level": "query_clause",
        "optimizer_target": "planner_input_order",
    },
    "exploratory_pattern_order_symmetry": {
        "rule_space_id": "S2",
        "rule_type": "clause_reordering",
        "structural_level": "query_clause",
        "optimizer_target": "planner_input_order",
    },
    "pattern_decomposition_symmetry": {
        "rule_space_id": "S3",
        "rule_type": "pattern_decomposition",
        "structural_level": "graph_topology",
        "optimizer_target": "join_decomposition",
    },
    "pattern_fusion_symmetry": {
        "rule_space_id": "S4",
        "rule_type": "pattern_fusion",
        "structural_level": "graph_topology",
        "optimizer_target": "join_merging",
    },
    "edge_notation_symmetry": {
        "rule_space_id": "S6",
        "rule_type": "direction_reversal_notation",
        "structural_level": "graph_semantics",
        "optimizer_target": "index_usage",
    },
    "node_order_symmetry": {
        "rule_space_id": "S6",
        "rule_type": "undirected_endpoint_reversal",
        "structural_level": "graph_semantics",
        "optimizer_target": "index_usage",
    },
    "label_order_symmetry": {
        "rule_space_id": "S7",
        "rule_type": "label_set_permutation",
        "structural_level": "schema_symmetry",
        "optimizer_target": "label_filtering",
    },
    "path_endpoint_symmetry": {
        "rule_space_id": "S8",
        "rule_type": "path_endpoint_rewriting",
        "structural_level": "traversal_semantics",
        "optimizer_target": "expansion_strategy",
    },
    "alpha_renaming_symmetry": {
        "rule_space_id": "S9",
        "rule_type": "variable_binding_shift",
        "structural_level": "scope_structure",
        "optimizer_target": "pipeline_binding",
    },
    "exploratory_with_filter_shift_symmetry": {
        "rule_space_id": "S11",
        "rule_type": "early_late_filter_shift",
        "structural_level": "pipeline_stage",
        "optimizer_target": "pushdown_logic",
    },
    "property_map_order_symmetry": {
        "rule_space_id": "S13",
        "rule_type": "projection_or_property_reordering",
        "structural_level": "output_pipeline",
        "optimizer_target": "projection_pushdown",
    },
    "exploratory_projection_reorder_symmetry": {
        "rule_space_id": "S13",
        "rule_type": "projection_reordering",
        "structural_level": "output_pipeline",
        "optimizer_target": "projection_pushdown",
    },
}


def generate_symmetry_family(
    query_case: QueryCase,
    config: TransformConfig | None = None,
    target: object | None = None,
) -> SymmetryFamily:
    """Generate a conservative symmetric query family for one GDsmith query."""

    del target
    transform_config = config or TransformConfig()
    query = query_case.query.rstrip().rstrip(";")
    variants: list[SymmetryVariant] = []
    skipped: list[SymmetryVariant] = []
    seen = {query}
    rules = _enabled_rules(transform_config.enabled_rules)
    if transform_config.max_variants_per_case is not None:
        rules = rules[: max(0, transform_config.max_variants_per_case)]
    for rule in rules:
        try:
            variant = rule.apply(query)
        except (TransformSkipError, CypherParseError, ProofFailure) as exc:
            skipped.append(
                SymmetryVariant(
                    variant_id=f"{query_case.case_id}_{rule.name}",
                    query=query,
                    transform_rule=rule.name,
                    proof_obligation="No static proof obligation because this rule was skipped.",
                    static_safe=False,
                    safety_tier="exploratory" if rule.name.startswith("exploratory_") else "strict",
                    skip_reason=getattr(exc, "code", "UNPROVEN_TRANSFORM"),
                    metadata={"error": str(exc)},
                )
            )
            continue
        if variant.query in seen:
            skipped.append(
                SymmetryVariant(
                    variant_id=f"{query_case.case_id}_{rule.name}",
                    query=variant.query,
                    transform_rule=rule.name,
                    proof_obligation=variant.proof_obligation,
                    static_safe=False,
                    safety_tier=variant.safety_tier,
                    skip_reason="DUPLICATE_VARIANT",
                    metadata=variant.metadata,
                )
            )
            continue
        seen.add(variant.query)
        variants.append(
            SymmetryVariant(
                variant_id=f"{query_case.case_id}_{len(variants) + 1:02d}_{rule.name}",
                query=variant.query,
                transform_rule=variant.transform_rule,
                proof_obligation=variant.proof_obligation,
                static_safe=True,
                safety_tier=variant.safety_tier,
                metadata=variant.metadata,
            )
        )
    return SymmetryFamily(
        case_id=query_case.case_id,
        seed_query=query,
        parameters=query_case.parameters,
        variants=tuple(variants),
        skipped_variants=tuple(skipped),
        source=query_case.source,
        generator_case_id=query_case.generator_case_id,
        raw_query=query_case.raw_query,
    )


def _enabled_rules(raw_rules: Iterable[str]) -> tuple[_Rule, ...]:
    available = {
        "node_order_symmetry": _Rule("node_order_symmetry", _node_order_symmetry),
        "edge_notation_symmetry": _Rule("edge_notation_symmetry", _edge_notation_symmetry),
        "predicate_order_symmetry": _Rule("predicate_order_symmetry", _predicate_order_symmetry),
        "pattern_order_symmetry": _Rule("pattern_order_symmetry", _pattern_order_symmetry),
        "pattern_decomposition_symmetry": _Rule("pattern_decomposition_symmetry", _pattern_decomposition_symmetry),
        "pattern_fusion_symmetry": _Rule("pattern_fusion_symmetry", _pattern_fusion_symmetry),
        "path_endpoint_symmetry": _Rule("path_endpoint_symmetry", _path_endpoint_symmetry),
        "label_order_symmetry": _Rule("label_order_symmetry", _label_order_symmetry),
        "property_map_order_symmetry": _Rule("property_map_order_symmetry", _property_map_order_symmetry),
        "alpha_renaming_symmetry": _Rule("alpha_renaming_symmetry", _alpha_renaming_symmetry),
        "exploratory_pattern_order_symmetry": _Rule(
            "exploratory_pattern_order_symmetry", _exploratory_pattern_order_symmetry
        ),
        "exploratory_predicate_order_symmetry": _Rule(
            "exploratory_predicate_order_symmetry", _exploratory_predicate_order_symmetry
        ),
        "exploratory_optional_match_order_symmetry": _Rule(
            "exploratory_optional_match_order_symmetry", _exploratory_optional_match_order_symmetry
        ),
        "exploratory_with_filter_shift_symmetry": _Rule(
            "exploratory_with_filter_shift_symmetry", _exploratory_with_filter_shift_symmetry
        ),
        "exploratory_projection_reorder_symmetry": _Rule(
            "exploratory_projection_reorder_symmetry", _exploratory_projection_reorder_symmetry
        ),
    }
    selected = tuple(available[name] for name in raw_rules if name in available)
    if not selected:
        raise TransformSkipError("NO_ENABLED_SYMMETRY_RULE", "no supported symmetry rule is enabled")
    return selected


def _require_provable_query_scope(query: str) -> None:
    stripped = query.lstrip()
    if stripped.upper().startswith("OPTIONAL MATCH"):
        raise TransformSkipError("UNSAFE_OPTIONAL_MATCH_START", "OPTIONAL MATCH may change null introduction semantics")
    if WRITE_RE.search(query):
        raise TransformSkipError("WRITE_STATEMENT", "write statements are not transformed")
    if UNSAFE_SCOPE_RE.search(query):
        raise TransformSkipError("UNSAFE_SCOPE", "CALL and EXISTS scopes are not transformed")
    if _has_pattern_comprehension(query):
        raise TransformSkipError("PATTERN_COMPREHENSION", "pattern comprehensions are not transformed")


def _require_exploratory_scope(query: str) -> None:
    if WRITE_RE.search(query):
        raise TransformSkipError("WRITE_STATEMENT", "write statements are not transformed")
    if UNSAFE_SCOPE_RE.search(query):
        raise TransformSkipError("UNSAFE_SCOPE", "CALL and EXISTS scopes are not transformed")
    if _has_pattern_comprehension(query):
        raise TransformSkipError("PATTERN_COMPREHENSION", "pattern comprehensions are not transformed")


def _node_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_node_order(match_clause)
    return _variant_from_proof(
        query,
        "node_order_symmetry",
        proved,
    )


def _edge_notation_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_edge_notation(match_clause)
    return _variant_from_proof(
        query,
        "edge_notation_symmetry",
        proved,
    )


def _predicate_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    replacements: list[tuple[_Region, str, SemanticProof, dict[str, object]]] = []
    first_error: Exception | None = None
    for where in _where_regions(query):
        try:
            proved = _prove_predicate_order(query, where)
        except (CypherParseError, ProofFailure) as exc:
            if first_error is None:
                first_error = exc
            continue
        replacements.append((where, proved.replacement, proved.proof, proved.metadata))
    if not replacements:
        if first_error is not None:
            raise first_error
        raise TransformSkipError("NO_WHERE", "predicate order symmetry requires a WHERE clause")
    transformed = query
    for region, replacement, _proof, _metadata in reversed(replacements):
        transformed = _replace_span(transformed, region.start, region.end, f" {replacement} ")
    proof = replacements[0][2]
    return SymmetryVariant(
        variant_id="",
        query=transformed,
        transform_rule="predicate_order_symmetry",
        proof_obligation=proof.obligation,
        metadata={
            "where_count": len(replacements),
            **_rule_space_metadata("predicate_order_symmetry"),
            "semantic_proof": proof.to_metadata(),
            "proofs": tuple(item[2].to_metadata() for item in replacements),
        },
    )


def _pattern_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    clauses = list(parse_query(query).ordinary_match_clauses())
    for clause in clauses:
        parsed = parse_match_clause(clause)
        if isinstance(parsed.pattern, AstPatternList) and parsed.pattern.separator == "comma" and len(parsed.pattern.atoms) >= 2:
            replacement_pattern = replace(parsed.pattern, atoms=tuple(reversed(parsed.pattern.atoms)), text="")
            proof = prove_pattern_list_order_ra(parsed.pattern, replacement_pattern)
            transformed_clause = parsed.replace_pattern(replacement_pattern.render())
            return SymmetryVariant(
                variant_id="",
                query=query[: parsed.clause.start] + transformed_clause + query[parsed.clause.end :],
                transform_rule="pattern_order_symmetry",
                proof_obligation=proof.obligation,
                metadata={
                    **_rule_space_metadata("pattern_order_symmetry"),
                    "semantic_proof": proof.to_metadata(),
                    "pattern_count": len(parsed.pattern.atoms),
                },
            )
    if len(clauses) < 2:
        raise TransformSkipError("NEED_TWO_MATCH_CLAUSES", "pattern order symmetry requires two ordinary MATCH clauses")
    proved = _prove_pattern_order(parse_match_clause(clauses[0]), parse_match_clause(clauses[1]))
    transformed = query[: proved.clause.start] + proved.replacement + query[proved.region.end :]
    return SymmetryVariant(
        variant_id="",
        query=transformed,
        transform_rule="pattern_order_symmetry",
        proof_obligation=proved.proof.obligation,
        metadata=proved.metadata | _rule_space_metadata("pattern_order_symmetry") | {"semantic_proof": proved.proof.to_metadata()},
    )


def _path_endpoint_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_path_endpoint(query, match_clause)
    return _variant_from_proof(
        query,
        "path_endpoint_symmetry",
        proved,
    )


def _label_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_label_order(match_clause)
    return _variant_from_proof(query, "label_order_symmetry", proved)


def _property_map_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_property_map_order(match_clause)
    return _variant_from_proof(query, "property_map_order_symmetry", proved)


def _alpha_renaming_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    match_clause = _first_ast_match_clause(query)
    proved = _prove_alpha_renaming(query, match_clause)
    return _variant_from_proof(query, "alpha_renaming_symmetry", proved)


def _exploratory_pattern_order_symmetry(query: str) -> SymmetryVariant:
    _require_exploratory_scope(query)
    clauses = list(parse_query(query).ordinary_match_clauses())
    if len(clauses) < 2:
        raise TransformSkipError("NEED_TWO_MATCH_CLAUSES", "exploratory pattern order requires two ordinary MATCH clauses")
    first = clauses[0]
    second = clauses[1]
    if first.end != second.start:
        raise TransformSkipError("NON_ADJACENT_MATCH", "MATCH clauses must be adjacent")
    transformed = query[: first.start] + second.text + first.text + query[second.end :]
    return SymmetryVariant(
        variant_id="",
        query=transformed,
        transform_rule="exploratory_pattern_order_symmetry",
        proof_obligation="Exploratory adjacent MATCH swap; dynamic equivalence validation is required.",
        safety_tier="exploratory",
        metadata={
            **_rule_space_metadata("exploratory_pattern_order_symmetry"),
            "exploratory_reason": "adjacent ordinary MATCH clauses swapped before runtime equivalence validation",
        },
    )


def _exploratory_predicate_order_symmetry(query: str) -> SymmetryVariant:
    _require_exploratory_scope(query)
    replacements: list[tuple[_Region, str]] = []
    for where in _where_regions(query):
        predicate = parse_and_predicate(query[where.start : where.end], allow_ordering=True, allow_in=True)
        replacement = predicate.render_reversed()
        if replacement != query[where.start : where.end].strip():
            replacements.append((where, replacement))
    if not replacements:
        raise TransformSkipError("NO_EXPLORATORY_PREDICATE", "no exploratory predicate reorder was possible")
    transformed = query
    for region, replacement in reversed(replacements):
        transformed = _replace_span(transformed, region.start, region.end, f" {replacement} ")
    return SymmetryVariant(
        variant_id="",
        query=transformed,
        transform_rule="exploratory_predicate_order_symmetry",
        proof_obligation="Exploratory predicate reorder with non-total terms; dynamic equivalence validation is required.",
        safety_tier="exploratory",
        metadata={**_rule_space_metadata("predicate_order_symmetry"), "where_count": len(replacements)},
    )


def _exploratory_optional_match_order_symmetry(query: str) -> SymmetryVariant:
    clauses = _clauses(query)
    for first, second in zip(clauses, clauses[1:]):
        if first.keyword == "OPTIONAL MATCH" and second.keyword == "OPTIONAL MATCH" and first.end == second.start:
            transformed = query[: first.start] + second.text + first.text + query[second.end :]
            return SymmetryVariant(
                variant_id="",
                query=transformed,
                transform_rule="exploratory_optional_match_order_symmetry",
                proof_obligation="Exploratory OPTIONAL MATCH swap; null-introduction semantics require dynamic validation.",
                safety_tier="exploratory",
                metadata={
                    "rule_space_id": "S5",
                    "rule_type": "optional_match_lift_lower",
                    "structural_level": "execution_pipeline",
                    "optimizer_target": "outer_join_behavior",
                    "exploratory_reason": "adjacent OPTIONAL MATCH clauses swapped",
                },
            )
    raise TransformSkipError("NEED_TWO_OPTIONAL_MATCH_CLAUSES", "requires adjacent OPTIONAL MATCH clauses")


def _pattern_decomposition_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    for clause in parse_query(query).ordinary_match_clauses():
        parsed = parse_match_clause(clause)
        if not isinstance(parsed.pattern, AstPatternList) or parsed.pattern.separator != "chain":
            continue
        if len(parsed.pattern.atoms) < 2 or not all(isinstance(atom, AstEdgePattern) for atom in parsed.pattern.atoms):
            continue
        decomposed_pattern = AstPatternList(text="", atoms=parsed.pattern.atoms, separator="comma")
        proof = prove_pattern_equivalence_ra(
            parsed.pattern,
            decomposed_pattern,
            theorem="MatchChainDecompositionEquivalence",
            fragment="one ordinary MATCH chain split into adjacent MATCH clauses",
            premises=(
                "the chain is decomposed into the same ordered relationship atoms",
                "shared endpoint variables, labels, relationship variables, types, and properties are unchanged",
                "the original MATCH WHERE predicate, if present, is kept after all decomposed atoms are bound",
            ),
            invariant="a chain pattern and the natural join of its edge atoms produce the same binding multiset",
        )
        replacement = " ".join(f"MATCH {atom.render()}" for atom in parsed.pattern.atoms)
        if parsed.where_region is not None:
            replacement = f"{replacement} WHERE {parsed.where_region.text}"
        transformed = _splice_query(query, parsed.clause.start, parsed.clause.end, replacement)
        return SymmetryVariant(
            variant_id="",
            query=transformed,
            transform_rule="pattern_decomposition_symmetry",
            proof_obligation=proof.obligation,
            metadata={
                **_rule_space_metadata("pattern_decomposition_symmetry"),
                "semantic_proof": proof.to_metadata(),
                "decomposed_atom_count": len(parsed.pattern.atoms),
            },
        )
    raise TransformSkipError("NO_DECOMPOSABLE_CHAIN", "requires one ordinary MATCH chain with at least two edge atoms")


def _pattern_fusion_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    clauses = list(parse_query(query).ordinary_match_clauses())
    for first_raw, second_raw in zip(clauses, clauses[1:]):
        if first_raw.end != second_raw.start:
            continue
        first = parse_match_clause(first_raw)
        second = parse_match_clause(second_raw)
        if first.where_region is not None:
            continue
        if not isinstance(first.pattern, AstEdgePattern) or not isinstance(second.pattern, AstEdgePattern):
            continue
        if not _same_node_pattern(first.pattern.right, second.pattern.left):
            continue
        original_pattern = AstPatternList(text="", atoms=(first.pattern, second.pattern), separator="comma")
        fused_pattern = AstPatternList(text="", atoms=(first.pattern, second.pattern), separator="chain")
        proof = prove_pattern_equivalence_ra(
            original_pattern,
            fused_pattern,
            theorem="AdjacentMatchChainFusionEquivalence",
            fragment="two adjacent ordinary MATCH edge atoms fused into one chain pattern",
            premises=(
                "the adjacent MATCH clauses bind the same shared endpoint variable",
                "relationship variables, node labels, types, properties, and directions are unchanged",
                "the first MATCH has no WHERE filter; any second MATCH WHERE predicate remains after the fused chain",
            ),
            invariant="adjacent MATCH edge atoms and the equivalent chain pattern lower to the same natural join",
        )
        replacement = f"MATCH {fused_pattern.render()}"
        if second.where_region is not None:
            replacement = f"{replacement} WHERE {second.where_region.text}"
        transformed = _splice_query(query, first.clause.start, second.clause.end, replacement)
        return SymmetryVariant(
            variant_id="",
            query=transformed,
            transform_rule="pattern_fusion_symmetry",
            proof_obligation=proof.obligation,
            metadata={
                **_rule_space_metadata("pattern_fusion_symmetry"),
                "semantic_proof": proof.to_metadata(),
                "fused_atom_count": 2,
            },
        )
    raise TransformSkipError("NO_FUSIBLE_MATCH_CHAIN", "requires adjacent MATCH edge atoms sharing one endpoint")


def _exploratory_with_filter_shift_symmetry(query: str) -> SymmetryVariant:
    _require_exploratory_scope(query)
    clauses = _clauses(query)
    for first, second in zip(clauses, clauses[1:]):
        if first.keyword != "MATCH" or second.keyword != "WITH":
            continue
        parsed = parse_match_clause(first)
        if parsed.where_region is None:
            continue
        if _keyword_in_clause(second.text, "WHERE"):
            continue
        where_variables = _property_variables(parsed.where_region.text)
        projected = _with_projected_variables(second.text)
        if where_variables and not set(where_variables).issubset(projected):
            raise TransformSkipError("WITH_FILTER_VARIABLE_NOT_PROJECTED", "WITH clause does not project every WHERE variable")
        where_keyword = re.search(r"\bWHERE\b", first.text, re.I)
        if where_keyword is None:
            continue
        match_without_where = first.text[: where_keyword.start()].rstrip()
        replacement = f"{match_without_where} {second.text.rstrip()} WHERE {parsed.where_region.text}"
        transformed = _splice_query(query, first.start, second.end, replacement)
        return SymmetryVariant(
            variant_id="",
            query=transformed,
            transform_rule="exploratory_with_filter_shift_symmetry",
            proof_obligation="Exploratory WHERE-to-WITH filter shift; dynamic equivalence validation is required.",
            safety_tier="exploratory",
            metadata={
                **_rule_space_metadata("exploratory_with_filter_shift_symmetry"),
                "where_variables": where_variables,
                "projected_variables": tuple(sorted(projected)),
            },
        )
    raise TransformSkipError("NO_SHIFTABLE_WITH_FILTER", "requires MATCH ... WHERE followed by WITH projecting predicate variables")


def _exploratory_projection_reorder_symmetry(query: str) -> SymmetryVariant:
    _require_exploratory_scope(query)
    clauses = _clauses(query)
    for clause in reversed(clauses):
        if clause.keyword != "RETURN":
            continue
        body = clause.text[len("RETURN") :].strip()
        if not body or "*" in body or re.search(r"\b(ORDER\s+BY|SKIP|LIMIT|DISTINCT)\b", body, re.I):
            continue
        items = split_top_level_commas(body)
        if len(items) < 2:
            continue
        transformed_items = tuple(reversed(items))
        if tuple(items) == transformed_items:
            continue
        replacement = "RETURN " + ", ".join(transformed_items)
        transformed = _splice_query(query, clause.start, clause.end, replacement)
        return SymmetryVariant(
            variant_id="",
            query=transformed,
            transform_rule="exploratory_projection_reorder_symmetry",
            proof_obligation="Exploratory RETURN projection reorder; dynamic equivalence validation is required.",
            safety_tier="exploratory",
            metadata={
                **_rule_space_metadata("exploratory_projection_reorder_symmetry"),
                "projection_count": len(items),
            },
        )
    raise TransformSkipError("NO_REORDERABLE_PROJECTION", "requires a RETURN clause with at least two explicit projections")


@dataclass(frozen=True)
class _Clause:
    keyword: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Region:
    start: int
    end: int


def _first_ast_match_clause(query: str) -> AstMatchClause:
    for clause in parse_query(query).ordinary_match_clauses():
        return parse_match_clause(clause)
    raise TransformSkipError("NO_MATCH_CLAUSE", "query does not contain an ordinary MATCH clause")


def _clauses(query: str) -> tuple[_Clause, ...]:
    matches = [
        match
        for match in CLAUSE_RE.finditer(query)
        if not _inside_string(query, match.start()) and _is_clause_keyword(query, match)
    ]
    clauses: list[_Clause] = []
    for index, match in enumerate(matches):
        keyword = " ".join(match.group(1).upper().split())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(query)
        clauses.append(_Clause(keyword=keyword, start=match.start(), end=end, text=query[match.start() : end]))
    return tuple(clauses)


def _first_where_region(query: str) -> _Region | None:
    regions = _where_regions(query)
    return regions[0] if regions else None


def _where_regions(query: str) -> tuple[_Region, ...]:
    regions: list[_Region] = []
    for match in re.finditer(r"\bWHERE\b", query, re.I):
        if _inside_string(query, match.start()):
            continue
        start = match.end()
        end = _next_clause_start(query, start, ("RETURN", "WITH", "MATCH", "OPTIONAL MATCH", "UNWIND"))
        regions.append(_Region(start=start, end=end))
    return tuple(regions)


def _prove_node_order(match_clause: AstMatchClause) -> _ProvedReplacement:
    if isinstance(match_clause.pattern, AstPatternList):
        replacement_pattern = _reverse_undirected_pattern_list(match_clause.pattern)
        proof = prove_pattern_equivalence_ra(
            match_clause.pattern,
            replacement_pattern,
            theorem="UndirectedPatternChainEndpointCommutativity",
            fragment="one undirected chain or comma-separated pattern list in an ordinary MATCH clause",
            premises=(
                "all rewritten relationship atoms are undirected",
                "the same variables, labels, types, and properties occur before and after rewriting",
                "the surrounding query keeps the same externally visible variable names",
            ),
            invariant="undirected edge incidence and natural-join conjunction are commutative",
        )
        return _ProvedReplacement(
            clause=match_clause.clause,
            region=match_clause.pattern_region,
            replacement=replacement_pattern.render(),
            proof=proof,
            metadata={"ast_fragment": "undirected_pattern_list", "ra_canonical": proof.original_ra},
        )
    if not isinstance(match_clause.pattern, AstEdgePattern):
        raise TransformSkipError("STANDALONE_UNDIRECTED_EDGE_REQUIRED", "expected one undirected edge AST atom")
    replacement_pattern = replace(
        match_clause.pattern,
        left=match_clause.pattern.right,
        right=match_clause.pattern.left,
        direction="undirected",
        text="",
    )
    proof = prove_node_order_ra(match_clause.pattern, replacement_pattern)
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={"ast_fragment": "undirected_edge", "ra_canonical": proof.original_ra},
    )


def _prove_edge_notation(match_clause: AstMatchClause) -> _ProvedReplacement:
    if isinstance(match_clause.pattern, AstPatternList):
        replacement_pattern = _reverse_directed_notation_pattern_list(match_clause.pattern)
        proof = prove_pattern_equivalence_ra(
            match_clause.pattern,
            replacement_pattern,
            theorem="DirectedEdgeNotationEquivalence",
            fragment="one chain or comma-separated pattern list with directed relationship atoms",
            premises=(
                "each rewritten directed relationship lowers to the same ordered source-target RA atom",
                "relationship variables, endpoint variables, labels, types, and property maps are unchanged",
                "comma-separated pattern conjunction lowers to the same natural-join RA expression",
            ),
            invariant="alternate directed-edge notation preserves the same relationship predicate over bindings",
        )
        return _ProvedReplacement(
            clause=match_clause.clause,
            region=match_clause.pattern_region,
            replacement=replacement_pattern.render(),
            proof=proof,
            metadata={"ast_fragment": "directed_pattern_list", "ra_canonical": proof.original_ra},
        )
    if not isinstance(match_clause.pattern, AstEdgePattern):
        raise TransformSkipError("STANDALONE_DIRECTED_EDGE_REQUIRED", "expected one directed edge AST atom")
    replacement_pattern = replace(
        match_clause.pattern,
        left=match_clause.pattern.right,
        right=match_clause.pattern.left,
        direction="in",
        text="",
    )
    proof = prove_edge_notation_ra(match_clause.pattern, replacement_pattern)
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={"ast_fragment": "directed_edge", "ra_canonical": proof.original_ra},
    )


def _prove_predicate_order(query: str, where: _Region) -> _ProvedReplacement:
    predicate = parse_and_predicate(query[where.start : where.end])
    replacement = predicate.render_reversed()
    proof = prove_predicate_order_ra(predicate, parse_and_predicate(replacement))
    return _ProvedReplacement(
        clause=_Clause(keyword="WHERE", start=where.start, end=where.end, text=query[where.start : where.end]),
        region=where,
        replacement=replacement,
        proof=proof,
        metadata={"term_count": len(predicate.terms), "ra_canonical": proof.original_ra},
    )


def _prove_label_order(match_clause: AstMatchClause) -> _ProvedReplacement:
    node = _first_reorderable_label_node(match_clause.pattern)
    if node is None:
        raise TransformSkipError("NO_REORDERABLE_LABELS", "no node pattern has two or more unique labels")
    replacement_pattern = _replace_node_text(match_clause.pattern, node.text, _render_node_with_labels(node, _permuted(node.labels)))
    replacement_node = parse_match_clause(
        replace(match_clause.clause, text=match_clause.replace_pattern(replacement_pattern.render()))
    ).pattern
    proof = prove_pattern_equivalence_ra(
        match_clause.pattern,
        replacement_node,
        theorem="NodeLabelIntersectionCommutativity",
        fragment="node labels inside one MATCH pattern",
        premises=(
            "the transformed labels are a permutation of the same unique label set",
            "node variables and property maps are unchanged",
        ),
        invariant="label conjunction order is not observable",
    )
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={"node": node.variable, "label_count": len(node.labels), "ra_canonical": proof.original_ra},
    )


def _prove_property_map_order(match_clause: AstMatchClause) -> _ProvedReplacement:
    target = _first_reorderable_property_atom(match_clause.pattern)
    if target is None:
        raise TransformSkipError("NO_REORDERABLE_PROPERTY_MAP", "no property map has two or more unique safe keys")
    old_text, new_text = target
    replacement_pattern = _replace_pattern_text(match_clause.pattern, old_text, new_text)
    replacement_match = parse_match_clause(
        replace(match_clause.clause, text=match_clause.replace_pattern(replacement_pattern.render()))
    )
    proof = prove_pattern_equivalence_ra(
        match_clause.pattern,
        replacement_match.pattern,
        theorem="PropertyMapKeyConjunctionCommutativity",
        fragment="property map inside one MATCH pattern",
        premises=(
            "the transformed property map contains the same unique static keys and values",
            "values are pure literals or parameters accepted by the parser",
        ),
        invariant="property predicate conjunction order is not observable",
    )
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={"ra_canonical": proof.original_ra},
    )


def _prove_pattern_order(first: AstMatchClause, second: AstMatchClause) -> _ProvedReplacement:
    if first.clause.end != second.clause.start:
        raise TransformSkipError("NON_ADJACENT_MATCH", "MATCH clauses must be adjacent")
    proof = prove_pattern_order_ra(first, second, second, first)
    return _ProvedReplacement(
        clause=first.clause,
        region=_Region(start=first.clause.start, end=second.clause.end),
        replacement=second.clause.text + first.clause.text,
        proof=proof,
        metadata={
            "first_fragment": type(first.pattern).__name__,
            "second_fragment": type(second.pattern).__name__,
            "ra_canonical": proof.original_ra,
        },
    )


def _prove_alpha_renaming(query: str, match_clause: AstMatchClause) -> _ProvedReplacement:
    target = _first_unobservable_variable(query, match_clause.pattern)
    if target is None:
        raise TransformSkipError("NO_UNOBSERVABLE_VARIABLE", "no single-use internal variable can be alpha-renamed")
    old_name = target
    new_name = _fresh_variable(query, old_name)
    replacement_pattern = _replace_pattern_text(match_clause.pattern, old_name, new_name, word=True)
    replacement_match = parse_match_clause(
        replace(match_clause.clause, text=match_clause.replace_pattern(replacement_pattern.render()))
    )
    proof = prove_alpha_renaming_ra(_first_atom_with_variable(match_clause.pattern, new_name, old_name), _first_atom_with_variable(replacement_match.pattern, new_name, old_name))
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={"old_variable": old_name, "new_variable": new_name, "ra_canonical": proof.original_ra},
    )


def _prove_path_endpoint(query: str, match_clause: AstMatchClause) -> _ProvedReplacement:
    if not isinstance(match_clause.pattern, AstVarLengthPathPattern):
        raise TransformSkipError("STANDALONE_FINITE_UNDIRECTED_PATH_REQUIRED", "expected one finite path AST atom")
    hidden_variables = _hidden_path_variables(query, match_clause)
    replacement_pattern = replace(
        match_clause.pattern,
        left=match_clause.pattern.right,
        right=match_clause.pattern.left,
        text="",
    )
    proof = prove_path_endpoint_ra(match_clause.pattern, replacement_pattern, hidden_variables=hidden_variables)
    return _ProvedReplacement(
        clause=match_clause.clause,
        region=match_clause.pattern_region,
        replacement=replacement_pattern.render(),
        proof=proof,
        metadata={
            "ast_fragment": "finite_undirected_path",
            "hidden_variables": hidden_variables,
            "ra_canonical": proof.original_ra,
        },
    )


def _hidden_path_variables(query: str, match_clause: AstMatchClause) -> tuple[str, ...]:
    pattern = match_clause.pattern
    if not isinstance(pattern, AstVarLengthPathPattern):
        return ()
    names = tuple(name for name in (pattern.path_variable, pattern.relationship.variable) if name)
    tail = query[match_clause.clause.start + match_clause.pattern_region.end :]
    if re.search(r"\b(RETURN|WITH)\s+\*", tail, re.I):
        return ()
    return tuple(name for name in names if not re.search(rf"\b{re.escape(name)}\b", tail))


def _reverse_directed_notation_pattern_list(pattern: AstPatternList) -> AstPatternList:
    if pattern.separator == "chain":
        atoms = tuple(_flip_edge_notation(atom) for atom in reversed(pattern.atoms) if isinstance(atom, AstEdgePattern))
        if len(atoms) != len(pattern.atoms) or all(atom.direction == "undirected" for atom in atoms):
            raise TransformSkipError("NO_DIRECTED_EDGE", "chain has no directed relationship atom")
        return replace(pattern, atoms=atoms, text="")
    changed = False
    atoms = []
    for atom in pattern.atoms:
        if isinstance(atom, AstEdgePattern):
            rewritten = _flip_edge_notation(atom)
            changed = changed or rewritten != atom
            atoms.append(rewritten)
        elif isinstance(atom, AstPatternList):
            rewritten = _reverse_directed_notation_pattern_list(atom)
            changed = True
            atoms.append(rewritten)
        else:
            atoms.append(atom)
    if not changed:
        raise TransformSkipError("NO_DIRECTED_EDGE", "pattern list has no directed relationship atom")
    return replace(pattern, atoms=tuple(atoms), text="")


def _reverse_undirected_pattern_list(pattern: AstPatternList) -> AstPatternList:
    if pattern.separator == "chain":
        if not all(isinstance(atom, AstEdgePattern) and atom.direction == "undirected" for atom in pattern.atoms):
            raise TransformSkipError("NODE_ORDER_FRAGMENT", "node-order chain proof requires only undirected edges")
        atoms = tuple(_swap_edge_endpoints(atom) for atom in reversed(pattern.atoms) if isinstance(atom, AstEdgePattern))
        return replace(pattern, atoms=atoms, text="")
    atoms = []
    changed = False
    for atom in pattern.atoms:
        if isinstance(atom, AstEdgePattern) and atom.direction == "undirected":
            atoms.append(_swap_edge_endpoints(atom))
            changed = True
        elif isinstance(atom, AstPatternList):
            rewritten = _reverse_undirected_pattern_list(atom)
            atoms.append(rewritten)
            changed = True
        else:
            atoms.append(atom)
    if not changed:
        raise TransformSkipError("NODE_ORDER_FRAGMENT", "pattern list has no undirected relationship atom")
    return replace(pattern, atoms=tuple(atoms), text="")


def _flip_edge_notation(edge: AstEdgePattern) -> AstEdgePattern:
    if edge.direction == "out":
        return replace(edge, left=edge.right, right=edge.left, direction="in", text="")
    if edge.direction == "in":
        return replace(edge, left=edge.right, right=edge.left, direction="out", text="")
    return edge


def _swap_edge_endpoints(edge: AstEdgePattern) -> AstEdgePattern:
    return replace(edge, left=edge.right, right=edge.left, text="")


def _pattern_atoms(pattern: object) -> tuple[object, ...]:
    if isinstance(pattern, AstPatternList):
        atoms: list[object] = []
        for atom in pattern.atoms:
            atoms.extend(_pattern_atoms(atom))
        return tuple(atoms)
    return (pattern,)


def _node_patterns(pattern: object) -> tuple[AstNodePattern, ...]:
    nodes: list[AstNodePattern] = []
    for atom in _pattern_atoms(pattern):
        if isinstance(atom, AstNodePattern):
            nodes.append(atom)
        if isinstance(atom, AstEdgePattern | AstVarLengthPathPattern):
            nodes.extend((atom.left, atom.right))
    return tuple(nodes)


def _first_reorderable_label_node(pattern: object) -> AstNodePattern | None:
    for node in _node_patterns(pattern):
        if len(node.labels) >= 2 and len(set(node.labels)) == len(node.labels):
            return node
    return None


def _permuted(values: tuple[str, ...]) -> tuple[str, ...]:
    sorted_values = tuple(sorted(values))
    return tuple(reversed(values)) if values == sorted_values else sorted_values


def _render_node_with_labels(node: AstNodePattern, labels: tuple[str, ...]) -> str:
    labels_text = "".join(f":{label}" for label in labels)
    property_text = _property_text(node.properties)
    return f"({node.variable}{labels_text}{property_text})"


def _property_text(properties: tuple[tuple[str, str], ...]) -> str:
    if not properties:
        return ""
    return " {" + ", ".join(f"{key}: {value}" for key, value in properties) + "}"


def _first_reorderable_property_atom(pattern: object) -> tuple[str, str] | None:
    for atom in _pattern_atoms(pattern):
        candidates: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        if isinstance(atom, AstNodePattern):
            candidates.append((atom.text, atom.properties))
        elif isinstance(atom, AstEdgePattern):
            candidates.extend(((atom.left.text, atom.left.properties), (atom.relationship.text, atom.relationship.properties), (atom.right.text, atom.right.properties)))
        elif isinstance(atom, AstVarLengthPathPattern):
            candidates.extend(((atom.left.text, atom.left.properties), (atom.relationship.text, atom.relationship.properties), (atom.right.text, atom.right.properties)))
        for raw_text, properties in candidates:
            if len(properties) >= 2:
                reordered = tuple(sorted(properties))
                if reordered == properties:
                    reordered = tuple(reversed(properties))
                return raw_text, _replace_property_map(raw_text, reordered)
    return None


def _replace_property_map(raw_text: str, properties: tuple[tuple[str, str], ...]) -> str:
    replacement = "{" + ", ".join(f"{key}: {value}" for key, value in properties) + "}"
    return re.sub(r"\{[^{}]*\}", replacement, raw_text, count=1)


def _replace_node_text(pattern: object, old_text: str, new_text: str):
    return _replace_pattern_text(pattern, old_text, new_text)


def _replace_pattern_text(pattern: object, old_text: str, new_text: str, *, word: bool = False):
    if isinstance(pattern, AstPatternList):
        return replace(pattern, atoms=tuple(_replace_pattern_text(atom, old_text, new_text, word=word) for atom in pattern.atoms), text="")
    if isinstance(pattern, AstNodePattern):
        rendered = _word_replace(pattern.text, old_text, new_text) if word else pattern.text.replace(old_text, new_text, 1)
        return parse_match_clause(replace(_Clause(keyword="MATCH", start=0, end=len(f"MATCH {rendered}"), text=f"MATCH {rendered}"))).pattern
    if isinstance(pattern, AstEdgePattern):
        rendered = _word_replace(pattern.render(), old_text, new_text) if word else pattern.render().replace(old_text, new_text, 1)
        return parse_match_clause(replace(_Clause(keyword="MATCH", start=0, end=len(f"MATCH {rendered}"), text=f"MATCH {rendered}"))).pattern
    if isinstance(pattern, AstVarLengthPathPattern):
        rendered = _word_replace(pattern.render(), old_text, new_text) if word else pattern.render().replace(old_text, new_text, 1)
        return parse_match_clause(replace(_Clause(keyword="MATCH", start=0, end=len(f"MATCH {rendered}"), text=f"MATCH {rendered}"))).pattern
    return pattern


def _word_replace(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text, count=1)


def _first_unobservable_variable(query: str, pattern: object) -> str | None:
    if re.search(r"\b(RETURN|WITH)\s+\*", query, re.I):
        return None
    names = sorted(_pattern_variable_names(pattern))
    for name in names:
        if len(re.findall(rf"\b{re.escape(name)}\b", query)) == 1:
            return name
    return None


def _pattern_variable_names(pattern: object) -> set[str]:
    names: set[str] = set()
    for atom in _pattern_atoms(pattern):
        if isinstance(atom, AstNodePattern):
            names.add(atom.variable)
        elif isinstance(atom, AstEdgePattern):
            names.update((atom.left.variable, atom.right.variable))
            if atom.relationship.variable:
                names.add(atom.relationship.variable)
        elif isinstance(atom, AstVarLengthPathPattern):
            names.update((atom.left.variable, atom.right.variable))
            if atom.path_variable:
                names.add(atom.path_variable)
            if atom.relationship.variable:
                names.add(atom.relationship.variable)
    return names


def _fresh_variable(query: str, base: str) -> str:
    index = 0
    while True:
        candidate = f"{base}_sym{index}"
        if not re.search(rf"\b{re.escape(candidate)}\b", query):
            return candidate
        index += 1


def _first_atom_with_variable(pattern: object, new_name: str, old_name: str):
    for atom in _pattern_atoms(pattern):
        if re.search(rf"\b({re.escape(new_name)}|{re.escape(old_name)})\b", atom.render() if hasattr(atom, "render") else ""):
            return atom
    raise TransformSkipError("NO_ALPHA_ATOM", "alpha-renamed variable atom was not found")


def _same_node_pattern(left: AstNodePattern, right: AstNodePattern) -> bool:
    return (
        left.variable == right.variable
        and tuple(left.labels) == tuple(right.labels)
        and tuple(left.properties) == tuple(right.properties)
    )


def _property_variables(predicate: str) -> tuple[str, ...]:
    variables: list[str] = []
    for match in re.finditer(r"\b([A-Za-z]\w*)\s*\.", predicate):
        variable = match.group(1)
        if variable not in variables:
            variables.append(variable)
    return tuple(variables)


def _with_projected_variables(clause_text: str) -> set[str]:
    body = re.sub(r"^\s*WITH\b", "", clause_text, count=1, flags=re.I).strip()
    where_match = re.search(r"\bWHERE\b", body, re.I)
    if where_match is not None:
        body = body[: where_match.start()].strip()
    projected: set[str] = set()
    for item in split_top_level_commas(body):
        stripped = item.strip()
        alias = re.search(r"\bAS\s+([A-Za-z]\w*)$", stripped, re.I)
        if alias is not None:
            projected.add(alias.group(1))
            continue
        if re.fullmatch(r"[A-Za-z]\w*", stripped):
            projected.add(stripped)
    return projected


def _keyword_in_clause(text: str, keyword: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.I)
    return any(not _inside_string(text, match.start()) for match in pattern.finditer(text))


def _splice_query(query: str, start: int, end: int, replacement: str) -> str:
    prefix = query[:start]
    suffix = query[end:]
    if suffix and not suffix[0].isspace() and not replacement.endswith(" "):
        replacement += " "
    return prefix + replacement + suffix


def _rule_space_metadata(rule: str) -> dict[str, str]:
    return dict(RULE_SPACE_METADATA.get(rule, {}))


def _next_clause_start(query: str, start: int, keywords: tuple[str, ...]) -> int:
    for match in CLAUSE_RE.finditer(query, start):
        if _inside_string(query, match.start()):
            continue
        keyword = " ".join(match.group(1).upper().split())
        if keyword in keywords:
            return match.start()
    return len(query)


def _variant_from_proof(
    query: str,
    rule: str,
    proved: _ProvedReplacement,
) -> SymmetryVariant:
    transformed_clause = _replace_span(
        proved.clause.text,
        proved.region.start,
        proved.region.end,
        proved.replacement,
    )
    transformed = query[: proved.clause.start] + transformed_clause + query[proved.clause.end :]
    return SymmetryVariant(
        variant_id="",
        query=transformed,
        transform_rule=rule,
        proof_obligation=proved.proof.obligation,
        metadata=proved.metadata | _rule_space_metadata(rule) | {"semantic_proof": proved.proof.to_metadata()},
    )


def _replace_span(text: str, start: int, end: int, replacement: str) -> str:
    return text[:start] + replacement + text[end:]


def _is_clause_keyword(query: str, match: re.Match[str]) -> bool:
    keyword = " ".join(match.group(1).upper().split())
    if keyword != "WITH":
        return True
    prefix = query[: match.start()].rstrip().upper()
    return not (prefix.endswith("STARTS") or prefix.endswith("ENDS"))


def _inside_string(text: str, position: int) -> bool:
    quote: str | None = None
    escaped = False
    for char in text[:position]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is None and char in {"'", '"'}:
            quote = char
            continue
        if quote == char:
            quote = None
    return quote is not None


def _escaped(text: str, position: int) -> bool:
    count = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _has_pattern_comprehension(query: str) -> bool:
    for match in re.finditer(r"\[[^\]]*\|[^\]]*\]", query, re.S):
        snippet = match.group(0)
        if "(" in snippet and ")" in snippet:
            return True
    return False
