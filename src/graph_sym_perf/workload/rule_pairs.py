"""Build one certificate-bound query pair for one immutable rule-plan entry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from graph_sym_perf.core import QueryCase, QueryVariant, TargetSystem, TransformConfig
from graph_sym_perf.transforms.engine import apply_certified_rule_with_proof
from graph_sym_perf.transforms.rule_models import RuleRegistry, RuleSpec
from graph_sym_perf.transforms.registry import generate_rule_transform
from graph_sym_perf.transforms.safety_points import (
    SafetyCertificate,
    canonical_semantic_proof_hash,
    validate_bound_semantic_proof,
)
from graph_sym_perf.workload.models import CertifiedQueryPair, PairBuildOutcome

if TYPE_CHECKING:
    from graph_sym_perf.experiment.plan_models import RulePlanEntry


class RulePairFactory:
    def __init__(self, registry: RuleRegistry) -> None:
        self.registry = registry

    def build(
        self,
        query: str,
        entry: RulePlanEntry,
        target: TargetSystem,
    ) -> PairBuildOutcome:
        try:
            rule = self.registry.get(entry.rule_id)
        except (KeyError, TypeError, ValueError):
            return _quarantine("RULE_PLAN_INVARIANT_MISMATCH")
        if not _entry_matches_rule(entry, target, rule):
            return _quarantine("RULE_PLAN_INVARIANT_MISMATCH")

        family_name = "bool_redundancy" if rule.family.value == "bool" else "symmetry"
        config = TransformConfig(
            enabled_families=(family_name,),
            enabled_rules=(rule.implementation_rule_id,),
            max_variants_per_case=1,
            gdsmith_safe_only=True,
        )
        query_case = QueryCase(
            case_id=f"{entry.entry_id}:seed",
            template_name="gdsmith",
            query=query,
            parameters={},
            base_predicate="",
            safe_expression="",
            source="gdsmith",
            generator_case_id=entry.entry_id,
        )
        try:
            family = generate_rule_transform(rule, query_case, config, target)
        except TypeError:
            return _quarantine("TRANSFORM_INVARIANT_MISMATCH")
        except ValueError:
            return _quarantine("TRANSFORM_INVARIANT_MISMATCH")
        variants = tuple(variant for variant in family.variants if variant.static_safe)
        if not variants:
            return PairBuildOutcome(None, _skip_code(family.skipped_variants))
        if len(variants) != 1:
            return _quarantine("TRANSFORM_INVARIANT_MISMATCH")

        variant = variants[0]
        metadata = dict(variant.metadata)
        certificate = metadata.get("safety_certificate")
        if not isinstance(certificate, SafetyCertificate):
            return _quarantine("SAFETY_CERTIFICATE_INVARIANT_MISMATCH")
        if not _variant_matches_rule(variant, certificate, rule, metadata):
            return _quarantine("SAFETY_CERTIFICATE_INVARIANT_MISMATCH")

        semantic_proof = metadata.get("semantic_proof")
        if not isinstance(semantic_proof, Mapping):
            return _quarantine("SEMANTIC_PROOF_INVARIANT_MISMATCH")
        try:
            validate_certified_pair(
                rule,
                query_a=query,
                query_b=variant.query,
                certificate=certificate,
                semantic_proof=semantic_proof,
            )
        except (TypeError, ValueError):
            return _quarantine("SEMANTIC_PROOF_INVARIANT_MISMATCH")
        proof_sha256 = canonical_semantic_proof_hash(semantic_proof)
        if metadata.get("semantic_proof_sha256") not in {None, proof_sha256}:
            return _quarantine("SEMANTIC_PROOF_INVARIANT_MISMATCH")
        metadata["semantic_proof_sha256"] = proof_sha256
        pair_hash = certified_pair_hash(query, variant.query, rule.canonical_rule_id, proof_sha256)
        return PairBuildOutcome(
            CertifiedQueryPair(
                pair_hash=pair_hash,
                query_a=query,
                query_b=variant.query,
                parameters=family.parameters,
                certificate=certificate,
                transform_metadata=metadata,
            )
        )


def _entry_matches_rule(entry: RulePlanEntry, target: TargetSystem, rule: RuleSpec) -> bool:
    stream_matches_tier = (
        (entry.stream.value == "discovery" and entry.tier == "core")
        or (entry.stream.value == "control" and entry.tier == "control")
    )
    return bool(
        entry.target is target
        and entry.rule_id == rule.canonical_rule_id
        and entry.family == rule.family.value
        and entry.tier == rule.tier.value
        and entry.proof_version == rule.proof_version
        and stream_matches_tier
    )


def _variant_matches_rule(
    variant: QueryVariant,
    certificate: SafetyCertificate,
    rule: RuleSpec,
    metadata: Mapping[str, object],
) -> bool:
    allowed_site_classes = rule.allowed_site_classes
    expected_pattern_site = (
        "ordinary_match_comma_pattern_list"
        if rule.canonical_rule_id == "pattern_order_symmetry"
        else None
    )
    metadata_site = metadata.get("site_class")
    return bool(
        variant.transform_rule == rule.canonical_rule_id
        and variant.safety_tier == rule.tier.value
        and certificate.canonical_rule_id == rule.canonical_rule_id
        and certificate.family == rule.family.value
        and certificate.tier == rule.tier.value
        and certificate.proof_id == rule.proof_id
        and certificate.proof_version == rule.proof_version
        and certificate.site_class in allowed_site_classes
        and (metadata_site is None or metadata_site == expected_pattern_site)
    )


def validate_certified_pair(
    rule: RuleSpec,
    *,
    query_a: str,
    query_b: str,
    certificate: SafetyCertificate,
    semantic_proof: Mapping[str, object],
) -> None:
    validate_bound_semantic_proof(rule, certificate, semantic_proof)
    replay = apply_certified_rule_with_proof(query_a, rule, certificate)
    if (
        replay.query != query_b
        or canonical_semantic_proof_hash(replay.semantic_proof)
        != canonical_semantic_proof_hash(semantic_proof)
        or replay.certificate != certificate
    ):
        raise ValueError("certified pair does not match the independently replayed engine proof")


def _skip_code(skipped_variants: tuple[object, ...]) -> str:
    reasons = {
        getattr(variant, "skip_reason", None)
        for variant in skipped_variants
        if getattr(variant, "skip_reason", None)
    }
    if any(str(reason).startswith("PROOF_") for reason in reasons):
        return "PROOF_PRECONDITION_REJECTED"
    return "RULE_NOT_APPLICABLE"


def certified_pair_hash(query_a: str, query_b: str, rule_id: str, proof_sha256: str) -> str:
    payload = json.dumps(
        {
            "proof_sha256": proof_sha256,
            "query_a": query_a,
            "query_b": query_b,
            "rule_id": rule_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    from hashlib import sha256

    return sha256(payload.encode("utf-8")).hexdigest()


def _quarantine(code: str) -> PairBuildOutcome:
    return PairBuildOutcome(None, code, quarantine=True)


__all__ = ["RulePairFactory", "certified_pair_hash", "validate_certified_pair"]
