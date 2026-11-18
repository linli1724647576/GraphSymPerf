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
    # SY3/SY4 rewrite a where() edge or walk filter from its other endpoint inside match().
    "SY3": MatchingPattern("SY3", "edge"),
    "SY4": MatchingPattern("SY4", "walk"),
    # SY5-SY8 exchange the two patterns of a declarative match() step.
    "SY5": MatchingPattern("SY5", "independent"),
    "SY6": MatchingPattern("SY6", "shared"),
    "SY7": MatchingPattern("SY7", "chain"),
    "SY8": MatchingPattern("SY8", "optional"),
}


def patterns_for(support):
    return dict(PATTERNS)
