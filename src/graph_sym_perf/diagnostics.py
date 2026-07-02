"""Run-level diagnostics shared by summaries and health checks."""

from __future__ import annotations

from collections import Counter
from typing import Mapping

from graph_sym_perf.core import CaseRecord, QueryVariant


def summarize_skip_errors(records: list[CaseRecord] | tuple[CaseRecord, ...]) -> Mapping[str, object]:
    skipped_records = [record for record in records if record.state == "skipped"]
    error_records = [record for record in records if record.state == "error"]
    skipped_by_system: Counter[str] = Counter()
    skipped_by_rule: Counter[str] = Counter()
    skipped_by_family: Counter[str] = Counter()
    skipped_by_reason: Counter[str] = Counter()
    errors_by_system: Counter[str] = Counter()
    errors_by_rule: Counter[str] = Counter()
    errors_by_reason: Counter[str] = Counter()
    skipped_variant_reasons: Counter[str] = Counter()

    for record in skipped_records:
        rule = _record_rule(record)
        family = _record_family(record)
        reason = normalize_reason(record.reason or _variant_reason(record.family.skipped_variants))
        skipped_by_system[record.system.value] += 1
        skipped_by_rule[rule] += 1
        skipped_by_family[family] += 1
        skipped_by_reason[reason] += 1
    for record in error_records:
        rule = _record_rule(record)
        reason = normalize_reason(record.reason)
        errors_by_system[record.system.value] += 1
        errors_by_rule[rule] += 1
        errors_by_reason[reason] += 1
    for record in records:
        for variant in record.family.skipped_variants:
            skipped_variant_reasons[normalize_reason(variant.skip_reason or variant.metadata.get("error") or "")] += 1

    return {
        "skipped_total": len(skipped_records),
        "error_total": len(error_records),
        "skipped_by_system": dict(sorted(skipped_by_system.items())),
        "skipped_by_rule": dict(sorted(skipped_by_rule.items())),
        "skipped_by_family": dict(sorted(skipped_by_family.items())),
        "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
        "errors_by_system": dict(sorted(errors_by_system.items())),
        "errors_by_rule": dict(sorted(errors_by_rule.items())),
        "errors_by_reason": dict(sorted(errors_by_reason.items())),
        "skipped_variant_reasons": dict(sorted(skipped_variant_reasons.items())),
    }


def normalize_reason(reason: object) -> str:
    raw = str(reason or "").strip()
    upper = raw.upper()
    if not raw:
        return "unknown"
    if "NOT_REPLAYABLE" in upper:
        return "unsupported_syntax"
    if "NO_STATICALLY_SAFE_VARIANT" in upper or "NO_SAFE" in upper:
        return "no_safe_injection_site"
    if "PROFILE" in upper and ("NOT" in upper or "NO_" in upper or "UNAVAILABLE" in upper):
        return "profile_not_available"
    if "PROFILE" in upper and "TIMEOUT" in upper:
        return "profile_timeout"
    if "TIMEOUT" in upper:
        return "result_timeout"
    if "PARSE" in upper or "SYNTAX" in upper:
        return "dialect_parse_error"
    if "EMPTY" in upper or "INVALID" in upper:
        return "generated_query_empty_or_invalid"
    if "UNSUPPORTED" in upper:
        if "FUNCTION" in upper:
            return "unsupported_function"
        return "unsupported_syntax"
    if "QUERY_ERROR" in upper or "ADAPTER" in upper or "ERROR" in upper or "EXCEPTION" in upper:
        return "adapter_error"
    return raw


def _record_family(record: CaseRecord) -> str:
    variants = record.family.variants or record.family.skipped_variants
    if variants:
        family = variants[0].metadata.get("transform_family")
        if family:
            return str(family)
        if "symmetry" in variants[0].transform_rule:
            return "symmetry"
    return "unknown"


def _record_rule(record: CaseRecord) -> str:
    if record.pmc is not None and record.pmc.transform_rule:
        return record.pmc.transform_rule
    variants = record.family.variants or record.family.skipped_variants
    if variants:
        return variants[0].transform_rule
    return "unknown"


def _variant_reason(variants: tuple[QueryVariant, ...]) -> str:
    for variant in variants:
        if variant.skip_reason:
            return str(variant.skip_reason)
        if variant.metadata.get("error"):
            return str(variant.metadata["error"])
    return ""
