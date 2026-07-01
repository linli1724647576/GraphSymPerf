"""Compatibility wrapper for the bool_redundancy transform family."""

from graph_sym_perf.transforms.bool_redundancy.family import (
    GDSMITH_TRANSFORM_RULE,
    TRANSFORM_RULE,
    TransformSkipError,
    analyze_gdsmith_injection_site,
    generate_bool_redundancy_family,
    inject_redundant_predicate,
    render_gdsmith_transform,
)

__all__ = [
    "GDSMITH_TRANSFORM_RULE",
    "TRANSFORM_RULE",
    "TransformSkipError",
    "analyze_gdsmith_injection_site",
    "generate_bool_redundancy_family",
    "inject_redundant_predicate",
    "render_gdsmith_transform",
]
