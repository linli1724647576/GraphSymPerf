from .boolean import BooleanPattern
from .conditional import ConditionalPattern
from .demand import DemandPattern
from .existence import ExistencePattern
from .matching import MatchingPattern


PATTERNS = {
    "SC1": BooleanPattern("SC1", "or"),
    "SC2": BooleanPattern("SC2", "and"),
    "SC3": ConditionalPattern("SC3"),
    "SC4": ConditionalPattern("SC4", coalesce=True),
    "SC5": DemandPattern("SC5"),
    "SC6": DemandPattern("SC6"),
    "SC7": ExistencePattern(),
    "SC8": DemandPattern("SC8"),
    "SY1": BooleanPattern("SY1", "and", swap=True),
    "SY2": BooleanPattern("SY2", "or", swap=True),
    # SY5-SY7 exchange the two patterns of a declarative match() step.
    # SY5-SY7 交换声明式 match() 步骤中的两个模式。
    "SY5": MatchingPattern("SY5", "independent"),
    "SY6": MatchingPattern("SY6", "shared"),
    "SY7": MatchingPattern("SY7", "chain"),
}


def patterns_for(support):
    return dict(PATTERNS)
