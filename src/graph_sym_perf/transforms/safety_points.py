from __future__ import annotations

from dataclasses import dataclass

from graph_sym_perf.transforms.rule_models import RuleSpec


@dataclass(frozen=True)
class SafetyCertificate:
    rule_id: str
    ast_path: str
    variables_in_scope: tuple[str, ...]
    expression_type: str
    nullability: str
    clause_context: str
    preconditions: tuple[str, ...]
    proof_id: str
    capability_hash: str


def enumerate_safety_points(query: str, rule: RuleSpec, capability_hash: str) -> tuple[SafetyCertificate, ...]:
    where = query.upper().split(" WHERE ", 1)
    if len(where) != 2 or "where_boolean" not in rule.site_classes:
        return ()
    expression = where[1].split(" RETURN ", 1)[0]
    parts = tuple(item.strip() for item in expression.split(" AND ") if item.strip())
    return tuple(SafetyCertificate(rule.rule_id, f"where[{index}]", (), "boolean", "unknown", "where", rule.purity_requirements, rule.proof_id, capability_hash) for index, _ in enumerate(parts))
