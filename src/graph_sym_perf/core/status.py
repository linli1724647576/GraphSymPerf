"""Shared status enums for tool evidence and external issue state."""

from __future__ import annotations

from enum import Enum


class ToolSupportLevel(str, Enum):
    NONE = "none"
    PLAN_ONLY = "plan_only"
    PROFILE_SUPPORTED = "profile_supported"
    PROFILE_SUPPORTED_WITHOUT_DB_HITS = "profile_supported_without_db_hits"
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
