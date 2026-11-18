from dataclasses import replace
from .boolean import BooleanPattern
from .conditional import ConditionalPattern
from .demand import DemandPattern
from .existence import ExistencePattern
from .path import PathPatternRule
from .matching import MatchingPattern


# Pattern IDs follow docs/pattern-catalog.en.md.
PATTERNS = {
    "SC1": BooleanPattern("SC1", "OR"),
    "SC2": BooleanPattern("SC2", "AND"),
    "SC3": ConditionalPattern("SC3"),
    "SC4": ConditionalPattern("SC4", coalesce=True),
    "SC5": DemandPattern("SC5", "zero"),
    "SC6": DemandPattern("SC6", "bounded"),
    "SC7": ExistencePattern(),
    "SC8": DemandPattern("SC8", "empty"),
    "SY1": BooleanPattern("SY1", "AND", swap=True),
    "SY2": BooleanPattern("SY2", "OR", swap=True),
    "SY3": PathPatternRule("SY3"),
    "SY4": PathPatternRule("SY4", bounded=True),
    # SY5-SY7 share a swap operation but use different connectivity and graph recipes.
    "SY5": MatchingPattern("SY5", "independent"),
    "SY6": MatchingPattern("SY6", "shared"),
    "SY7": MatchingPattern("SY7", "chain"),
    "SY8": MatchingPattern("SY8", "optional"),
}


def patterns_for(support):
    patterns = dict(PATTERNS)
    for name in ("SC1", "SC2", "SC3", "SC4", "SY1", "SY2"):
        patterns[name] = replace(patterns[name], work_kind=support["work"])
    if support["native_limit"]:
        patterns["SC6"] = DemandPattern("SC6", "native_limit")
    if not support["s7"]:
        del patterns["SC7"]
    return patterns
