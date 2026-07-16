from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from graph_sym_perf.cypher.ast import CypherQuery, PatternList
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query
from graph_sym_perf.equivalence.purity import assess_query_purity
from graph_sym_perf.transforms.rule_models import RuleRegistry, RuleSpec, SafetyTier


_CANONICAL_CERTIFICATE_FIELDS = (
    "canonical_rule_id",
    "family",
    "tier",
    "submit_eligible",
    "proof_id",
    "proof_version",
    "site_class",
    "ast_path",
    "capability_hash",
    "semantic_proof_hash",
)
_LEGACY_CERTIFICATE_FIELDS = (
    "rule_id",
    "ast_path",
    "variables_in_scope",
    "expression_type",
    "nullability",
    "clause_context",
    "preconditions",
    "proof_id",
    "capability_hash",
)


@dataclass(frozen=True, init=False)
class SafetyCertificate:
    canonical_rule_id: str
    family: str
    tier: str
    submit_eligible: bool
    proof_id: str
    proof_version: str
    site_class: str
    ast_path: str
    capability_hash: str
    semantic_proof_hash: str
    semantic_proof_template_hash: str = ""
    variables_in_scope: tuple[str, ...] = ()
    expression_type: str = ""
    nullability: str = ""
    clause_context: str = ""
    preconditions: tuple[str, ...] = ()
    rule_id: str = ""

    def __init__(self, *args: object, **kwargs: object) -> None:
        legacy_input = "canonical_rule_id" not in kwargs and "rule_id" in kwargs
        if args:
            if len(args) == len(_LEGACY_CERTIFICATE_FIELDS):
                positional_fields = _LEGACY_CERTIFICATE_FIELDS
                legacy_input = True
            elif len(args) == len(_CANONICAL_CERTIFICATE_FIELDS):
                positional_fields = _CANONICAL_CERTIFICATE_FIELDS
                legacy_input = False
            else:
                raise TypeError("SafetyCertificate expects 9 legacy or 10 canonical positional arguments")
            overlap = set(positional_fields) & set(kwargs)
            if overlap:
                raise TypeError(f"multiple values for certificate fields: {sorted(overlap)}")
            kwargs = {**dict(zip(positional_fields, args, strict=True)), **kwargs}
        allowed = (
            set(_CANONICAL_CERTIFICATE_FIELDS)
            | set(_LEGACY_CERTIFICATE_FIELDS)
            | {"semantic_proof_template_hash"}
        )
        unexpected = sorted(set(kwargs) - allowed)
        if unexpected:
            raise TypeError(f"unexpected certificate fields: {unexpected}")
        defaults: dict[str, object] = {
            "canonical_rule_id": "",
            "family": "",
            "tier": "",
            "submit_eligible": None,
            "proof_id": "",
            "proof_version": "",
            "site_class": "",
            "ast_path": "",
            "capability_hash": "",
            "semantic_proof_hash": "",
            "semantic_proof_template_hash": "",
            "variables_in_scope": (),
            "expression_type": "",
            "nullability": "",
            "clause_context": "",
            "preconditions": (),
            "rule_id": "",
        }
        defaults.update(kwargs)
        for name, value in defaults.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_legacy_input", legacy_input)
        self.__post_init__()

    def __post_init__(self) -> None:
        registry = RuleRegistry.default()
        supplied_ids = tuple(
            rule_id for rule_id in (self.canonical_rule_id, self.rule_id) if rule_id
        )
        if not supplied_ids:
            raise ValueError("certificate rule id is required")
        try:
            resolved_ids = tuple(registry.resolve_id(rule_id) for rule_id in supplied_ids)
        except KeyError as exc:
            raise ValueError(f"unknown canonical rule: {exc.args[0]}") from exc
        if len(set(resolved_ids)) != 1:
            raise ValueError("legacy and canonical rule ids must resolve to the same rule")
        canonical_rule_id = resolved_ids[0]
        object.__setattr__(self, "canonical_rule_id", canonical_rule_id)
        object.__setattr__(self, "rule_id", canonical_rule_id)
        rule = registry.get(canonical_rule_id)
        if self._legacy_input:
            self._validate_legacy(rule)
        else:
            self._validate_canonical(rule)

    def _validate_canonical(self, rule: RuleSpec) -> None:
        required = {
            "family": self.family,
            "tier": self.tier,
            "submit_eligible": self.submit_eligible,
            "proof_id": self.proof_id,
            "proof_version": self.proof_version,
            "site_class": self.site_class,
            "ast_path": self.ast_path,
            "capability_hash": self.capability_hash,
            "semantic_proof_hash": self.semantic_proof_hash,
        }
        missing = sorted(
            name
            for name, value in required.items()
            if value is None or (isinstance(value, str) and not value)
        )
        if missing:
            raise ValueError(f"canonical certificate fields are required: {missing}")
        self._validate_site(rule, self.site_class)
        expected = {
            "family": rule.family.value,
            "tier": rule.tier.value,
            "submit_eligible": rule.submit_eligible,
            "proof_id": rule.proof_id,
            "proof_version": rule.proof_version,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"certificate {name} does not match canonical rule facts")
        template_hash = _semantic_proof_hash(rule, self.site_class)
        if self.semantic_proof_template_hash:
            if self.semantic_proof_template_hash != template_hash:
                raise ValueError("certificate semantic proof template hash does not match canonical rule facts")
            if not _sha256_hex(self.semantic_proof_hash):
                raise ValueError("certificate bound semantic proof hash must be SHA-256")
        else:
            if self.semantic_proof_hash != template_hash:
                raise ValueError("unbound certificate semantic proof hash must match canonical rule facts")
            object.__setattr__(self, "semantic_proof_template_hash", template_hash)
        if self.preconditions and self.preconditions != rule.purity_requirements:
            raise ValueError("certificate preconditions do not match canonical rule facts")

    def _validate_legacy(self, rule: RuleSpec) -> None:
        if not all(
            (
                self.ast_path,
                self.expression_type,
                self.nullability,
                self.clause_context,
                self.preconditions,
                self.proof_id,
                self.capability_hash,
            )
        ):
            raise ValueError("legacy certificate fields must be complete")
        site_class = _legacy_site_class(self.ast_path)
        self._validate_site(rule, site_class)
        expected_site_facts = _LEGACY_SITE_FACTS[site_class]
        actual_site_facts = (self.expression_type, self.nullability, self.clause_context)
        if actual_site_facts != expected_site_facts:
            raise ValueError("legacy certificate site facts do not match the AST path")
        if self.proof_id != rule.proof_id:
            raise ValueError("legacy certificate proof_id does not match the canonical rule")
        if self.preconditions != rule.purity_requirements:
            raise ValueError("legacy certificate preconditions do not match the canonical rule")
        expected_new_facts = {
            "family": rule.family.value,
            "tier": rule.tier.value,
            "submit_eligible": rule.submit_eligible,
            "proof_version": rule.proof_version,
            "site_class": site_class,
            "semantic_proof_hash": _semantic_proof_hash(rule, site_class),
            "semantic_proof_template_hash": _semantic_proof_hash(rule, site_class),
        }
        for name, value in expected_new_facts.items():
            current = getattr(self, name)
            if current is not None and current != "" and current != value:
                raise ValueError(f"legacy certificate {name} conflicts with the canonical rule")
            object.__setattr__(self, name, value)

    @property
    def proof_bound(self) -> bool:
        return self.semantic_proof_hash != self.semantic_proof_template_hash

    def _validate_site(self, rule: RuleSpec, site_class: str) -> None:
        if site_class not in rule.allowed_site_classes:
            raise ValueError("certificate site class is not allowed by the canonical rule")
        if not _valid_ast_path(site_class, self.ast_path):
            raise ValueError("certificate AST path does not identify the declared site class")


_LEGACY_SITE_FACTS = {
    "where_boolean": ("boolean", "three_valued", "where"),
    "match_pattern_component": ("graph_pattern", "pattern_defined", "match"),
    "mandatory_match_clause": ("graph_pattern", "non_null_introducing", "match"),
    "bound_variable": ("bound_variable", "scope_bound", "match"),
}


def _legacy_site_class(ast_path: str) -> str:
    for site_class in _LEGACY_SITE_FACTS:
        if _valid_ast_path(site_class, ast_path):
            return site_class
    raise ValueError("legacy certificate AST path does not identify a supported site class")


def _valid_ast_path(site_class: str, ast_path: str) -> bool:
    patterns = {
        "where_boolean": r"clauses\[\d+\]\.where(?:\.(?:and|or)\[\d+\])*",
        "match_pattern_component": r"clauses\[\d+\]\.pattern\.components\[\d+\]",
        "mandatory_match_clause": r"clauses\[\d+\]\.mandatory_match",
        "bound_variable": r"clauses\[\d+\]\.variables\[[^\]]+\]",
    }
    pattern = patterns.get(site_class)
    return pattern is not None and re.fullmatch(pattern, ast_path) is not None


def enumerate_safety_points(
    ast: str | CypherQuery,
    rule: RuleSpec,
    capability_hash: str,
) -> tuple[SafetyCertificate, ...]:
    """Enumerate every provable site in the supported AST fragment.

    Unsupported clauses are skipped rather than guessed. The caller records an
    explicit not-applicable outcome when no certificate is returned.
    """

    if not capability_hash:
        raise ValueError("capability hash is required")
    source = ast if isinstance(ast, str) else ast.text
    if rule.tier in {SafetyTier.CORE, SafetyTier.CONTROL} and not assess_query_purity(source).safe:
        return ()
    query = parse_query(ast) if isinstance(ast, str) else ast
    certificates: list[SafetyCertificate] = []
    bound: set[str] = set()
    for clause_index, clause in enumerate(query.clauses):
        if clause.keyword != "MATCH":
            continue
        try:
            match = parse_match_clause(clause)
        except CypherParseError:
            continue
        bound.update(match.pattern.variables if hasattr(match.pattern, "variables") else {match.pattern.variable})
        variables = tuple(sorted(bound))
        if "mandatory_match_clause" in rule.site_classes:
            certificates.append(
                _certificate(
                    rule,
                    f"clauses[{clause_index}].mandatory_match",
                    "mandatory_match_clause",
                    variables,
                    "graph_pattern",
                    "non_null_introducing",
                    "match",
                    capability_hash,
                )
            )
        if "match_pattern_component" in rule.site_classes:
            for component_index, _component in enumerate(_pattern_components(match.pattern)):
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].pattern.components[{component_index}]",
                        "match_pattern_component",
                        variables,
                        "graph_pattern",
                        "pattern_defined",
                        "match",
                        capability_hash,
                    )
                )
        if "bound_variable" in rule.site_classes:
            for variable in variables:
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].variables[{variable}]",
                        "bound_variable",
                        variables,
                        "bound_variable",
                        "scope_bound",
                        "match",
                        capability_hash,
                    )
                )
        if (
            "where_boolean" in rule.site_classes
            and match.where_region is not None
            and _conservative_boolean_expression(match.where_region.text)
        ):
            for suffix in _boolean_subtree_paths(match.where_region.text):
                certificates.append(
                    _certificate(
                        rule,
                        f"clauses[{clause_index}].where{suffix}",
                        "where_boolean",
                        variables,
                        "boolean",
                        "three_valued",
                        "where",
                        capability_hash,
                    )
                )
    return tuple(certificates)


def _certificate(
    rule: RuleSpec,
    ast_path: str,
    site_class: str,
    variables: tuple[str, ...],
    expression_type: str,
    nullability: str,
    context: str,
    capability_hash: str,
) -> SafetyCertificate:
    return SafetyCertificate(
        canonical_rule_id=rule.canonical_rule_id,
        family=rule.family.value,
        tier=rule.tier.value,
        submit_eligible=rule.submit_eligible,
        proof_id=rule.proof_id,
        proof_version=rule.proof_version,
        site_class=site_class,
        ast_path=ast_path,
        capability_hash=capability_hash,
        semantic_proof_hash=_semantic_proof_hash(rule, site_class),
        rule_id=rule.rule_id,
        variables_in_scope=variables,
        expression_type=expression_type,
        nullability=nullability,
        clause_context=context,
        preconditions=rule.purity_requirements,
    )


def _semantic_proof_hash(rule: RuleSpec, site_class: str) -> str:
    facts = (
        rule.canonical_rule_id,
        rule.family.value,
        rule.tier.value,
        str(rule.submit_eligible),
        rule.proof_id,
        rule.proof_version,
        rule.purity_requirements,
        rule.supported_dialects,
        rule.required_capabilities,
        site_class,
    )
    return sha256(repr(facts).encode("utf-8")).hexdigest()


_SEMANTIC_PROOF_FIELDS = frozenset(
    {
        "ast_path",
        "canonical_rule_id",
        "family",
        "fragment",
        "invariant",
        "original_ra",
        "premises",
        "proof_id",
        "proof_version",
        "site_class",
        "submit_eligible",
        "theorem",
        "tier",
        "transformed_ra",
    }
)


def bind_semantic_proof(
    rule: RuleSpec,
    certificate: SafetyCertificate,
    proof: Mapping[str, object],
) -> tuple[dict[str, object], SafetyCertificate]:
    if set(proof) != {
        "theorem", "fragment", "premises", "invariant", "original_ra", "transformed_ra"
    }:
        raise ValueError("engine semantic proof metadata does not match the strict schema")
    bound = {
        "ast_path": certificate.ast_path,
        "canonical_rule_id": rule.canonical_rule_id,
        "family": rule.family.value,
        "fragment": proof["fragment"],
        "invariant": proof["invariant"],
        "original_ra": proof["original_ra"],
        "premises": proof["premises"],
        "proof_id": rule.proof_id,
        "proof_version": rule.proof_version,
        "site_class": certificate.site_class,
        "submit_eligible": rule.submit_eligible,
        "theorem": proof["theorem"],
        "tier": rule.tier.value,
        "transformed_ra": proof["transformed_ra"],
    }
    proof_hash = canonical_semantic_proof_hash(bound)
    signed = SafetyCertificate(
        canonical_rule_id=rule.canonical_rule_id,
        family=rule.family.value,
        tier=rule.tier.value,
        submit_eligible=rule.submit_eligible,
        proof_id=rule.proof_id,
        proof_version=rule.proof_version,
        site_class=certificate.site_class,
        ast_path=certificate.ast_path,
        capability_hash=certificate.capability_hash,
        semantic_proof_hash=proof_hash,
        semantic_proof_template_hash=certificate.semantic_proof_template_hash,
        rule_id=rule.rule_id,
        variables_in_scope=certificate.variables_in_scope,
        expression_type=certificate.expression_type,
        nullability=certificate.nullability,
        clause_context=certificate.clause_context,
        preconditions=rule.purity_requirements,
    )
    validate_bound_semantic_proof(rule, signed, bound)
    return bound, signed


def validate_bound_semantic_proof(
    rule: RuleSpec,
    certificate: SafetyCertificate,
    proof: Mapping[str, object],
) -> None:
    if set(proof) != _SEMANTIC_PROOF_FIELDS:
        raise ValueError("bound semantic proof metadata does not match the strict schema")
    expected = {
        "ast_path": certificate.ast_path,
        "canonical_rule_id": rule.canonical_rule_id,
        "family": rule.family.value,
        "proof_id": rule.proof_id,
        "proof_version": rule.proof_version,
        "site_class": certificate.site_class,
        "submit_eligible": rule.submit_eligible,
        "tier": rule.tier.value,
    }
    if any(proof.get(name) != value for name, value in expected.items()):
        raise ValueError("bound semantic proof identity conflicts with canonical rule facts")
    for field in ("theorem", "fragment", "invariant"):
        if not isinstance(proof.get(field), str) or not proof[field]:
            raise ValueError(f"bound semantic proof {field} must be non-empty")
    premises = proof.get("premises")
    if not isinstance(premises, list | tuple) or not premises or not all(
        isinstance(item, str) and item for item in premises
    ):
        raise ValueError("bound semantic proof premises must be non-empty strings")
    for field in ("original_ra", "transformed_ra"):
        if not isinstance(proof.get(field), list | tuple) or not proof[field]:
            raise ValueError(f"bound semantic proof {field} must be non-empty")
    if certificate.semantic_proof_template_hash != _semantic_proof_hash(rule, certificate.site_class):
        raise ValueError("certificate semantic proof template does not match canonical rule facts")
    if not certificate.proof_bound:
        raise ValueError("certificate is not bound to an actual semantic proof")
    if certificate.semantic_proof_hash != canonical_semantic_proof_hash(proof):
        raise ValueError("certificate semantic proof hash does not bind the supplied proof")


def canonical_semantic_proof_hash(proof: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(proof),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _pattern_components(pattern: object) -> tuple[object, ...]:
    if isinstance(pattern, PatternList):
        return tuple(pattern.atoms)
    return (pattern,)


def _boolean_subtree_paths(expression: str) -> tuple[str, ...]:
    paths: list[str] = [""]

    def visit(text: str, prefix: str) -> None:
        split = _split_top_level_boolean(text)
        if split is None:
            return
        operator, parts = split
        for index, part in enumerate(parts):
            child = f"{prefix}.{operator.lower()}[{index}]"
            paths.append(child)
            visit(_strip_outer_parentheses(part), child)

    visit(_strip_outer_parentheses(expression.strip()), "")
    return tuple(paths)


def _split_top_level_boolean(expression: str) -> tuple[str, tuple[str, ...]] | None:
    for operator in ("OR", "AND"):
        parts = _split_top_level_keyword(expression, operator)
        if len(parts) > 1:
            return operator, parts
    return None


def _split_top_level_keyword(expression: str, keyword: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    upper = expression.upper()
    while index < len(expression):
        char = expression[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        end = index + len(keyword)
        if (
            depth == 0
            and upper[index:end] == keyword
            and (index == 0 or not (expression[index - 1].isalnum() or expression[index - 1] == "_"))
            and (end == len(expression) or not (expression[end].isalnum() or expression[end] == "_"))
        ):
            parts.append(expression[start:index].strip())
            start = end
            index = end
            continue
        index += 1
    if not parts:
        return (expression.strip(),)
    parts.append(expression[start:].strip())
    return tuple(part for part in parts if part)


def _strip_outer_parentheses(expression: str) -> str:
    value = expression.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _conservative_boolean_expression(expression: str) -> bool:
    # CORE rules may duplicate a predicate. Keep the automatic fragment to
    # property/parameter/literal comparisons, NULL tests and boolean connectives.
    if re.search(r"\b(CASE|WHEN|THEN|ELSE|END|EXISTS|CALL|IN)\b", expression, re.IGNORECASE):
        return False
    if re.search(r"[+*/%]|\[[^\]]*]", expression):
        return False
    function_like_names = re.findall(r"\b([A-Za-z_]\w*)\s*\(", expression)
    if any(name.upper() not in {"AND", "OR", "NOT"} for name in function_like_names):
        return False
    scrubbed = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "'literal'", expression)
    return re.fullmatch(r"[A-Za-z0-9_.$'\"\s()=<>!-]+", scrubbed) is not None


__all__ = ["SafetyCertificate", "enumerate_safety_points"]
