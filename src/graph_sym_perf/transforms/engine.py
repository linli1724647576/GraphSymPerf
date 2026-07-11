"""Certificate-bound transformations for the audited executable CORE rules."""

from __future__ import annotations

import re
from dataclasses import replace

from graph_sym_perf.cypher.ast import EdgePattern, NodePattern, PatternList
from graph_sym_perf.cypher.parser import parse_and_predicate, parse_match_clause, parse_query
from graph_sym_perf.cypher.proof import (
    prove_label_order,
    prove_edge_notation,
    prove_alpha_renaming,
    prove_node_order,
    prove_pattern_equivalence,
    prove_pattern_list_order,
    prove_predicate_order,
)
from graph_sym_perf.transforms.rule_models import RuleSpec, SafetyTier
from graph_sym_perf.transforms.safety_points import (
    SafetyCertificate,
    _split_top_level_boolean,
    _strip_outer_parentheses,
)


class RuleNotApplicable(ValueError):
    pass


_PATH = re.compile(r"^clauses\[(?P<clause>\d+)]\.(?P<site>.+)$")
_COMPONENT = re.compile(r"^pattern\.components\[(?P<component>\d+)]$")


def apply_certified_rule(query: str, rule: RuleSpec, certificate: SafetyCertificate) -> str:
    if rule.tier not in {SafetyTier.CORE, SafetyTier.CONTROL}:
        raise ValueError("only CORE and CONTROL rules enter the certified engine")
    if certificate.rule_id != rule.rule_id or certificate.proof_id != rule.proof_id:
        raise ValueError("certificate identity does not match the selected rule")
    if certificate.preconditions != rule.purity_requirements:
        raise ValueError("certificate preconditions do not match the selected rule")
    if rule.rule_id in _BOOLEAN_RULES:
        transformed = _apply_boolean_rule(query, rule.rule_id, certificate)
    elif rule.rule_id == "duplicate_predicate_control":
        transformed = _apply_duplicate_predicate_control(query, certificate)
    elif rule.rule_id == "edge_notation_symmetry":
        transformed = _apply_edge_notation_control(query, certificate)
    elif rule.rule_id == "alpha_renaming_symmetry":
        transformed = _apply_alpha_renaming_control(query, certificate)
    elif rule.rule_id == "predicate_order_symmetry":
        transformed = _apply_predicate_order(query, certificate)
    elif rule.rule_id == "pattern_order_symmetry":
        transformed = _apply_pattern_order(query, certificate)
    elif rule.rule_id == "node_order_symmetry":
        transformed = _apply_node_order(query, certificate)
    elif rule.rule_id == "label_order_symmetry":
        transformed = _apply_label_order(query, certificate)
    else:
        raise RuleNotApplicable(f"no automatic implementation for {rule.rule_id}")
    if transformed == query:
        raise RuleNotApplicable("certified transform produced no structural change")
    return transformed


_BOOLEAN_RULES = frozenset(
    {
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
    }
)


def _apply_boolean_rule(query: str, rule_id: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    if match.where_region is None or not certificate.ast_path.startswith(f"clauses[{_clause_index(certificate)}].where"):
        raise RuleNotApplicable("boolean rule requires a certified WHERE subtree")
    expression = _boolean_expression_at_path(match.where_region.text, certificate.ast_path)
    additions = {
        "constant_true_disjunction": f"({expression}) OR true",
        "constant_false_conjunction": f"NOT (({expression}) AND false)",
        "null_partition_tautology": f"({expression}) IS NULL OR ({expression}) IS NOT NULL",
        "null_contradiction": f"NOT (({expression}) IS NULL AND ({expression}) IS NOT NULL)",
        "null_complete_excluded_middle": f"({expression}) OR NOT ({expression}) OR ({expression}) IS NULL",
    }
    replacement = f"({match.where_region.text}) AND ({additions[rule_id]})"
    return _replace_query_region(query, clause.start + match.where_region.start, clause.start + match.where_region.end, replacement)


def _apply_predicate_order(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    if match.where_region is None or certificate.ast_path != f"clauses[{_clause_index(certificate)}].where":
        raise RuleNotApplicable("predicate-order proof requires the complete WHERE conjunction")
    original = parse_and_predicate(match.where_region.text)
    replacement = original.render_reversed()
    transformed = parse_and_predicate(replacement)
    prove_predicate_order(original, transformed)
    return _replace_query_region(query, clause.start + match.where_region.start, clause.start + match.where_region.end, replacement)


def _apply_duplicate_predicate_control(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    if match.where_region is None:
        raise RuleNotApplicable("duplicate-predicate control requires WHERE")
    expression = _boolean_expression_at_path(match.where_region.text, certificate.ast_path)
    replacement = f"({match.where_region.text}) AND ({expression})"
    return _replace_query_region(query, clause.start + match.where_region.start, clause.start + match.where_region.end, replacement)


def _apply_edge_notation_control(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    component_index = _component_index(certificate)
    atom = _component(match.pattern, component_index)
    if not isinstance(atom, EdgePattern) or atom.direction not in {"out", "in"}:
        raise RuleNotApplicable("edge-notation control requires a directed edge")
    transformed_atom = replace(
        atom,
        left=atom.right,
        right=atom.left,
        direction="in" if atom.direction == "out" else "out",
        text="",
    )
    if atom.direction == "out":
        prove_edge_notation(atom, transformed_atom)
    else:
        prove_edge_notation(transformed_atom, atom)
    transformed_pattern = _replace_component(match.pattern, component_index, transformed_atom)
    return _replace_pattern(query, clause.start, match, transformed_pattern.render())


def _apply_alpha_renaming_control(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    variable_match = re.search(r"\.variables\[([A-Za-z]\w*)]$", certificate.ast_path)
    if variable_match is None:
        raise RuleNotApplicable("alpha-renaming control requires a bound-variable certificate")
    variable = variable_match.group(1)
    occurrences = tuple(re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(variable)}(?![A-Za-z0-9_])", query))
    if len(occurrences) != 1 or not isinstance(match.pattern, NodePattern) or match.pattern.variable != variable:
        raise RuleNotApplicable("alpha-renaming requires one unobserved standalone node variable")
    renamed = f"{variable}_control"
    transformed_node = replace(
        match.pattern,
        variable=renamed,
        text=re.sub(
            rf"(\(\s*){re.escape(variable)}(?![A-Za-z0-9_])",
            rf"\1{renamed}",
            match.pattern.text,
            count=1,
        ),
    )
    prove_alpha_renaming(match.pattern, transformed_node)
    return _replace_pattern(query, clause.start, match, transformed_node.render())


def _apply_pattern_order(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    component_index = _component_index(certificate)
    if not isinstance(match.pattern, PatternList) or match.pattern.separator != "comma" or len(match.pattern.atoms) < 2:
        raise RuleNotApplicable("pattern order requires a comma-separated MATCH pattern list")
    if component_index >= len(match.pattern.atoms):
        raise RuleNotApplicable("certified pattern component is outside the parsed list")
    atoms = list(match.pattern.atoms)
    other = (component_index + 1) % len(atoms)
    atoms[component_index], atoms[other] = atoms[other], atoms[component_index]
    transformed = replace(match.pattern, atoms=tuple(atoms), text="")
    prove_pattern_list_order(match.pattern, transformed)
    return _replace_pattern(query, clause.start, match, transformed.render())


def _apply_node_order(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    component_index = _component_index(certificate)
    atom = _component(match.pattern, component_index)
    if not isinstance(atom, EdgePattern) or atom.direction != "undirected":
        raise RuleNotApplicable("node order requires a certified undirected edge component")
    transformed_atom = replace(atom, left=atom.right, right=atom.left, text="")
    prove_node_order(atom, transformed_atom)
    transformed_pattern = _replace_component(match.pattern, component_index, transformed_atom)
    return _replace_pattern(query, clause.start, match, transformed_pattern.render())


def _apply_label_order(query: str, certificate: SafetyCertificate) -> str:
    clause, match = _match_at_certificate(query, certificate)
    component_index = _component_index(certificate)
    atom = _component(match.pattern, component_index)
    transformed_atom = _reverse_first_label_list(atom)
    transformed_pattern = _replace_component(match.pattern, component_index, transformed_atom)
    prove_pattern_equivalence(
        match.pattern,
        transformed_pattern,
        theorem="NodeLabelIntersectionCommutativity",
        fragment="one certified node inside a MATCH component",
        premises=("the unique static label set is unchanged", "variables and properties are unchanged"),
        invariant="label conjunction order is unobservable",
    )
    return _replace_pattern(query, clause.start, match, transformed_pattern.render())


def _reverse_first_label_list(atom: object) -> object:
    if isinstance(atom, NodePattern):
        if len(set(atom.labels)) < 2:
            raise RuleNotApplicable("node has fewer than two unique labels")
        labels = tuple(reversed(atom.labels))
        return replace(atom, labels=labels, text=_render_node_with_labels(atom, labels))
    if isinstance(atom, EdgePattern):
        for side in ("left", "right"):
            node = getattr(atom, side)
            if len(set(node.labels)) >= 2:
                labels = tuple(reversed(node.labels))
                transformed_node = replace(node, labels=labels, text=_render_node_with_labels(node, labels))
                return replace(atom, **{side: transformed_node, "text": ""})
    raise RuleNotApplicable("component has no node with two unique labels")


def _render_node_with_labels(node: NodePattern, labels: tuple[str, ...]) -> str:
    label_pattern = r"(?:\s*:\s*[A-Za-z]\w*)+"
    replacement = "".join(f":{label}" for label in labels)
    rendered, count = re.subn(label_pattern, replacement, node.text, count=1)
    if count != 1:
        raise RuleNotApplicable("node label tokens cannot be rendered without loss")
    return rendered


def _match_at_certificate(query: str, certificate: SafetyCertificate):
    index = _clause_index(certificate)
    parsed = parse_query(query)
    if index >= len(parsed.clauses) or parsed.clauses[index].keyword != "MATCH":
        raise RuleNotApplicable("certificate does not identify an ordinary MATCH clause")
    clause = parsed.clauses[index]
    return clause, parse_match_clause(clause)


def _clause_index(certificate: SafetyCertificate) -> int:
    match = _PATH.fullmatch(certificate.ast_path)
    if match is None:
        raise ValueError("certificate AST path is invalid")
    return int(match.group("clause"))


def _component_index(certificate: SafetyCertificate) -> int:
    path = _PATH.fullmatch(certificate.ast_path)
    assert path is not None
    component = _COMPONENT.fullmatch(path.group("site"))
    if component is None:
        raise RuleNotApplicable("certificate does not identify a MATCH component")
    return int(component.group("component"))


def _component(pattern: object, index: int) -> object:
    if isinstance(pattern, PatternList):
        if index >= len(pattern.atoms):
            raise RuleNotApplicable("component index is outside the pattern list")
        return pattern.atoms[index]
    if index != 0:
        raise RuleNotApplicable("standalone pattern only has component zero")
    return pattern


def _replace_component(pattern: object, index: int, replacement: object):
    if isinstance(pattern, PatternList):
        atoms = list(pattern.atoms)
        atoms[index] = replacement
        return replace(pattern, atoms=tuple(atoms), text="")
    return replacement


def _replace_pattern(query: str, clause_start: int, match: object, replacement: str) -> str:
    return _replace_query_region(
        query,
        clause_start + match.pattern_region.start,
        clause_start + match.pattern_region.end,
        replacement,
    )


def _replace_query_region(query: str, start: int, end: int, replacement: str) -> str:
    prefix, suffix = query[:start], query[end:]
    if prefix and replacement and not prefix[-1].isspace() and not replacement[0].isspace():
        replacement = " " + replacement
    if suffix and replacement and not suffix[0].isspace() and not replacement[-1].isspace():
        replacement += " "
    return prefix + replacement + suffix


def _boolean_expression_at_path(expression: str, ast_path: str) -> str:
    suffix = ast_path.split(".where", 1)[1]
    current = _strip_outer_parentheses(expression)
    for operator, raw_index in re.findall(r"\.(and|or)\[(\d+)]", suffix):
        split = _split_top_level_boolean(current)
        if split is None or split[0].lower() != operator:
            raise RuleNotApplicable("certificate boolean subtree no longer matches the query")
        parts = split[1]
        index = int(raw_index)
        if index >= len(parts):
            raise RuleNotApplicable("certificate boolean subtree index is out of bounds")
        current = _strip_outer_parentheses(parts[index])
    return current


__all__ = ["RuleNotApplicable", "apply_certified_rule"]
