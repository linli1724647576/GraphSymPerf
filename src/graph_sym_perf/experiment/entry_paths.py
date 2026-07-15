"""Canonical filesystem identity for schema-v3 RulePlan entries."""

from __future__ import annotations

import hashlib
import re


def rule_plan_entry_directory_name(entry_id: str) -> str:
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError("entry_id must be a non-empty string")
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", entry_id).strip("-._") or "entry"
    digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:120]}-{digest}"
