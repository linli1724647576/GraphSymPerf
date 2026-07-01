"""Project-level exceptions."""

from __future__ import annotations


class GraphSymPerfError(Exception):
    """Base exception for expected GraphSymPerf failures."""


class ConfigError(GraphSymPerfError):
    """Raised when a run configuration is invalid."""


class AdapterError(GraphSymPerfError):
    """Raised when a database adapter operation fails."""


class PreflightError(GraphSymPerfError):
    """Raised when no real database target can run."""


class QueryExecutionError(AdapterError):
    """Raised when a target database rejects or fails a query."""
