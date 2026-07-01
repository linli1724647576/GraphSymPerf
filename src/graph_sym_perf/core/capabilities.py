"""Engine capability declarations used by adapters and oracles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineCapabilities:
    supports_execute: bool = True
    supports_explain: bool = False
    supports_profile: bool = False
    has_db_hits: bool = False
    has_actual_rows: bool = False
    has_estimated_rows: bool = False
    has_page_cache_metrics: bool = False
    has_memory_metrics: bool = False
    plan_tree_reliable: bool = False
    profile_text_reliable: bool = False
    latency_reliable: bool = True
    supports_destructive_load: bool = False
    dialect: str = "cypher"
    notes: tuple[str, ...] = ()
