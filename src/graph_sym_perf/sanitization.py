"""Shared recursive sanitization for process and durable-artifact boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "<redacted>"


class SanitizationIntegrityError(ValueError):
    """A generic boundary would invalidate already sealed evidence."""

_SENSITIVE_KEY_SUFFIX = re.compile(
    r"(?:password|passwd|pwd|secret|token|authorization|credential|api[_-]?key|auth)$",
    re.IGNORECASE,
)
_PLAIN_INLINE_SECRET_NAME = (
    r"(?:password|passwd|pwd|secret|authorization|credential|api[_-]?key)"
)
_PREFIXED_INLINE_SECRET_NAME = (
    r"(?:(?:[A-Za-z0-9]+[_-])+"
    r"(?:password|passwd|pwd|secret|token|auth))"
)
_INLINE_SECRET_NAME = (
    rf"(?:{_PLAIN_INLINE_SECRET_NAME}|{_PREFIXED_INLINE_SECRET_NAME})"
)
_INLINE_AUTH_NAME = r"(?:authorization|(?:[A-Za-z0-9]+[_-])+auth)"
_INLINE_KEY_PREFIX = (
    r"(?<![A-Za-z0-9_.])"
    r"(?P<key_quote>['\"]?)"
)
_INLINE_KEY_SUFFIX = (
    rf"{_INLINE_SECRET_NAME}"
    r"(?P=key_quote)"
    r"\s*[:=]\s*"
)
_INLINE_AUTH_KEY_SUFFIX = (
    rf"{_INLINE_AUTH_NAME}"
    r"(?P=key_quote)"
    r"\s*[:=]\s*"
)
_DOTTED_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<dotted_quote>['\"]?)"
    r"(?:[A-Za-z0-9_-]+\.)+"
    r"(?:password|passwd|pwd|secret|token|authorization|credential|api[_-]?key|auth)"
    r"(?P=dotted_quote)\s*[:=]\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}}\]]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    rf"(?P<prefix>{_INLINE_KEY_PREFIX}{_INLINE_AUTH_KEY_SUFFIX})"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^;\r\n]*)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?P<prefix>{_INLINE_KEY_PREFIX}{_INLINE_KEY_SUFFIX})"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}}\]]+)",
    re.IGNORECASE,
)
_CONNECTION_URI = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+",
    re.IGNORECASE,
)
_SECRET_BEARING_TOKEN = re.compile(
    r"(?i)\b[^\s,;]*secret[^\s,;]*\b",
)
_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?P<path_quote>['\"])(?:[a-z]:\\|/)[^\r\n]*?(?P=path_quote)",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)\b[a-z]:\\[^'\"<>\r\n,;)]*",
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:[^'\"<>\r\n,;)]*)",
)
_TRACEBACK_FILE_LINE = re.compile(
    r"^File\s+[\"'].*[\"'],\s*line\s+\d+",
    re.IGNORECASE,
)
_TRACEBACK_HEADER = re.compile(
    r"traceback\s*\(most recent call last\)\s*:?,?",
    re.IGNORECASE,
)
_SEAL_KEY = re.compile(
    r"(?:^|_)(?:content_hash|report_hash|receipt(?:_hash)?|signature(?:_hash)?)$",
    re.IGNORECASE,
)
_EVIDENCE_TEXT_FIELDS = frozenset(
    {
        "cypher",
        "graph_statements",
        "parameter_values",
        "parameters",
        "queries",
        "query",
        "query_a",
        "query_b",
        "query_texts",
        "record",
        "records",
        "result",
        "results",
        "result_rows",
        "rows",
        "source_query",
        "statement",
        "statements",
        "transformed_query",
    }
)
_DIAGNOSTIC_FIELD_PARTS = frozenset(
    {
        "detail",
        "details",
        "diagnostic",
        "diagnostics",
        "error",
        "exception",
        "message",
        "reason",
        "stderr",
        "stdout",
        "traceback",
        "warning",
        "warnings",
    }
)


def is_sensitive_key(value: object) -> bool:
    """Return whether a structured key names a credential-bearing value."""

    normalized = _normalized_key(value)
    if normalized is None:
        return False
    return _SENSITIVE_KEY_SUFFIX.search(normalized) is not None


def _normalized_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    while (
        len(normalized) >= 2
        and normalized[0] in {'"', "'"}
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1].strip()
    return normalized


def sanitize_text(
    value: object,
    *,
    limit: int | None = None,
    single_line: bool = False,
) -> str:
    """Remove credential, URI, path, and traceback leakage from one string."""

    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise ValueError("sanitized text limit must be a positive integer")
    safe_lines: list[str] = []
    for line in str(value).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("traceback (most recent call last)"):
            continue
        if _TRACEBACK_FILE_LINE.match(stripped):
            continue
        safe_lines.append(stripped if single_line else line)
    rendered = (" " if single_line else "\n").join(safe_lines)
    rendered = _TRACEBACK_HEADER.sub("", rendered)
    rendered = _DOTTED_SENSITIVE_ASSIGNMENT.sub("<redacted-assignment>", rendered)
    rendered = _AUTHORIZATION_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _SENSITIVE_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _CONNECTION_URI.sub("<connection-uri>", rendered)
    rendered = _SECRET_BEARING_TOKEN.sub(REDACTED, rendered)
    rendered = _QUOTED_ABSOLUTE_PATH.sub("<absolute-path>", rendered)
    rendered = _WINDOWS_ABSOLUTE_PATH.sub("<absolute-path>", rendered)
    rendered = _POSIX_ABSOLUTE_PATH.sub("<absolute-path>", rendered)
    if single_line:
        rendered = re.sub(r"\s+", " ", rendered).strip()
    if not rendered and single_line:
        rendered = "diagnostic details redacted"
    if limit is not None and len(rendered) > limit:
        rendered = rendered[: max(0, limit - 1)].rstrip() + "…"
    return rendered


def sanitize_structure(value: Any) -> Any:
    """Recursively sanitize JSON-like values while preserving container shape."""

    return _sanitize_structure(value, preserve_text=False)


def _sanitize_structure(value: Any, *, preserve_text: bool) -> Any:
    """Internal field-aware recursion used by the public boundary sanitizer."""

    if isinstance(value, Mapping):
        for key in value:
            _require_safe_mapping_key(key)
        sanitized = {
            key: (
                REDACTED
                if is_sensitive_key(key)
                else _sanitize_structure(
                    item,
                    preserve_text=_field_preserves_text(key, inherited=preserve_text),
                )
            )
            for key, item in value.items()
        }
        if _is_sealed_mapping(value):
            if sanitized != dict(value):
                raise SanitizationIntegrityError(
                    "EVIDENCE_INTEGRITY_ERROR: boundary sanitization would alter sealed evidence"
                )
            return value
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_structure(item, preserve_text=preserve_text)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_structure(item, preserve_text=preserve_text)
            for item in value
        )
    if isinstance(value, str):
        return value if preserve_text else sanitize_text(value)
    return value


def _field_preserves_text(key: object, *, inherited: bool) -> bool:
    normalized = _normalized_key(key)
    if normalized is None:
        return inherited
    lowered = normalized.lower()
    parts = frozenset(part for part in re.split(r"[._-]+", lowered) if part)
    if parts & _DIAGNOSTIC_FIELD_PARTS or lowered in {"health_event", "reason_codes"}:
        return False
    if lowered in _EVIDENCE_TEXT_FIELDS or lowered.endswith("_query"):
        return True
    return inherited


def _require_safe_mapping_key(key: object) -> None:
    normalized = _normalized_key(key)
    if normalized is None:
        return
    if (
        is_sensitive_key(key)
        and re.fullmatch(r"[A-Za-z0-9_.-]+", normalized) is not None
    ):
        return
    if sanitize_text(str(key)) != str(key):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: mapping key contains leak-bearing material"
        )


def _is_sealed_mapping(value: Mapping[Any, Any]) -> bool:
    return any(
        normalized is not None and _SEAL_KEY.search(normalized) is not None
        for key in value
        if (normalized := _normalized_key(key)) is not None
    )


__all__ = [
    "REDACTED",
    "SanitizationIntegrityError",
    "is_sensitive_key",
    "sanitize_structure",
    "sanitize_text",
]
