"""Shared status enums for tool evidence and external issue state."""

from __future__ import annotations

from enum import Enum


class ToolSupportLevel(str, Enum):
    NONE = "none"
    PLAN_ONLY = "plan_only"
    PROFILE_SUPPORTED = "profile_supported"
    PROFILE_SUPPORTED_WITHOUT_DB_HITS = "profile_supported_without_db_hits"
    PROFILE_AND_RUNTIME_SUPPORTED = "profile_and_runtime_supported"
    LATENCY_SUPPORTED = "latency_supported"


class DeveloperStatus(str, Enum):
    UNKNOWN = "unknown"
    REPORTED = "reported"
    CONFIRMED = "confirmed"
    FIXED = "fixed"
    WONT_FIX = "wont_fix"
    UPSTREAM = "upstream"
    REJECTED = "rejected"


class ClaimLevel(str, Enum):
    CANDIDATE = "candidate"
    STRONG_CANDIDATE = "strong_candidate"
    DEVELOPER_CONFIRMED = "developer_confirmed"
    FIXED = "fixed"
    UPSTREAM_LIMITATION = "upstream_limitation"


PROFILE_SUPPORT_LEVELS = frozenset(
    {
        ToolSupportLevel.PROFILE_SUPPORTED.value,
        ToolSupportLevel.PROFILE_SUPPORTED_WITHOUT_DB_HITS.value,
        ToolSupportLevel.PROFILE_AND_RUNTIME_SUPPORTED.value,
    }
)

RUNTIME_SUPPORT_LEVELS = frozenset(
    {
        ToolSupportLevel.PROFILE_AND_RUNTIME_SUPPORTED.value,
        # Legacy validation artifacts used this value before runtime was made
        # profile-gated.  Keep readers backward-compatible, but new health
        # checks still flag it as invalid for fresh runs.
        ToolSupportLevel.LATENCY_SUPPORTED.value,
    }
)


def support_level_value(value: object) -> str:
    if isinstance(value, ToolSupportLevel):
        return value.value
    if value is None:
        return ToolSupportLevel.NONE.value
    return str(value)


def is_profile_supported_level(value: object) -> bool:
    return support_level_value(value) in PROFILE_SUPPORT_LEVELS


def is_runtime_supported_level(value: object) -> bool:
    return support_level_value(value) in RUNTIME_SUPPORT_LEVELS


def support_rank(value: object) -> int:
    return {
        ToolSupportLevel.PROFILE_AND_RUNTIME_SUPPORTED.value: 5,
        ToolSupportLevel.PROFILE_SUPPORTED.value: 4,
        ToolSupportLevel.PROFILE_SUPPORTED_WITHOUT_DB_HITS.value: 3,
        ToolSupportLevel.LATENCY_SUPPORTED.value: 2,
        ToolSupportLevel.PLAN_ONLY.value: 1,
        ToolSupportLevel.NONE.value: 0,
    }.get(support_level_value(value), 0)
