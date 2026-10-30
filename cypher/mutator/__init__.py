from dataclasses import replace
from .boolean import BooleanPattern
from .conditional import ConditionalPattern
from .demand import DemandPattern
from .existence import ExistencePattern
from .path import PathPatternRule
from .matching import MatchingPattern


# These are the rewritten catalog IDs, not the IDs in historical bug reports.
# 这里使用重写后的模式编号，不对应历史 BUG 报告中的同名编号。
PATTERNS = {
    "S1": BooleanPattern("S1", "OR"),
    "S2": BooleanPattern("S2", "AND"),
    "S3": ConditionalPattern("S3"),
    "S4": ConditionalPattern("S4", coalesce=True),
    "S5": DemandPattern("S5", "zero"),
    "S6": DemandPattern("S6", "bounded"),
    "S7": ExistencePattern(),
    "S8": DemandPattern("S8", "empty"),
    "Y1": BooleanPattern("Y1", "AND", swap=True),
    "Y2": BooleanPattern("Y2", "OR", swap=True),
    "Y3": PathPatternRule("Y3"),
    "Y4": PathPatternRule("Y4", bounded=True),
    # Y5-Y7 share a swap operation but use different connectivity and graph recipes.
    # Y5-Y7 共用交换操作，但变量连接关系和固定构图配方不同。
    "Y5": MatchingPattern("Y5", "independent"),
    "Y6": MatchingPattern("Y6", "shared"),
    "Y7": MatchingPattern("Y7", "chain"),
    "Y8": MatchingPattern("Y8", "optional"),
}


def patterns_for(support):
    patterns = dict(PATTERNS)
    for name in ("S1", "S2", "S3", "S4", "Y1", "Y2"):
        patterns[name] = replace(patterns[name], work_kind=support["work"])
    if support["native_limit"]:
        patterns["S6"] = DemandPattern("S6", "native_limit")
    if not support["s7"]:
        del patterns["S7"]
    return patterns
