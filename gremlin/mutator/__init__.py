from .boolean import BooleanPattern
from .conditional import ConditionalPattern
from .demand import DemandPattern


PATTERNS = {
    "S1": BooleanPattern("S1", "or"), "S2": BooleanPattern("S2", "and"),
    "S3": ConditionalPattern("S3"), "S4": ConditionalPattern("S4", coalesce=True),
    "S5": DemandPattern("S5"), "S6": DemandPattern("S6"), "S8": DemandPattern("S8"),
    "Y1": BooleanPattern("Y1", "and", swap=True), "Y2": BooleanPattern("Y2", "or", swap=True),
}


def patterns_for(support):
    return dict(PATTERNS)
