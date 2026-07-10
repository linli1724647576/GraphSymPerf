from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DecisionKind(str, Enum):
    ACCEPT = "accept"
    REWRITE = "rewrite"
    REJECT = "reject"


@dataclass(frozen=True)
class PortableQuery:
    text: str


@dataclass(frozen=True)
class CompatibilityDecision:
    kind: DecisionKind
    reason_code: str
    rewritten_query: str | None = None
