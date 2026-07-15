"""Shared recursive sanitization for process and durable-artifact boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "<redacted>"

_SENSITIVE_KEY_SUFFIX = re.compile(
    r"(?:password|passwd|pwd|secret|token|authorization|credential|api[_-]?key|auth)$",
    re.IGNORECASE,
)
_INLINE_KEY_PREFIX = (
    r"(?<![A-Za-z0-9_.])"
    r"(?P<key_quote>['\"]?)"
    r"[A-Za-z0-9_-]*"
)
_INLINE_KEY_SUFFIX = (
    r"(?:password|passwd|pwd|secret|token|authorization|credential|api[_-]?key|auth)"
    r"(?P=key_quote)"
    r"\s*[:=]\s*"
)
_INLINE_AUTH_KEY_SUFFIX = (
    r"(?:authorization|auth)"
    r"(?P=key_quote)"
    r"\s*[:=]\s*"
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
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)\b[a-z]:\\(?:[^\s'\"<>]+\\)*[^\s'\"<>:,;)]*",
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:[^/\s'\"<>]+/)*[^/\s'\"<>:,;)]*",
)
_TRACEBACK_FILE_LINE = re.compile(
    r"^File\s+[\"'].*[\"'],\s*line\s+\d+",
    re.IGNORECASE,
)
_TRACEBACK_HEADER = re.compile(
    r"traceback\s*\(most recent call last\)\s*:?,?",
    re.IGNORECASE,
)


def is_sensitive_key(value: object) -> bool:
    """Return whether a structured key names a credential-bearing value."""

    if not isinstance(value, str):
        return False
    normalized = value.strip()
    while (
        len(normalized) >= 2
        and normalized[0] in {'"', "'"}
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1].strip()
    return _SENSITIVE_KEY_SUFFIX.search(normalized) is not None


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
    rendered = _AUTHORIZATION_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _SENSITIVE_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _CONNECTION_URI.sub("<connection-uri>", rendered)
    rendered = _SECRET_BEARING_TOKEN.sub(REDACTED, rendered)
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

    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key) else sanitize_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_structure(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


__all__ = [
    "REDACTED",
    "is_sensitive_key",
    "sanitize_structure",
    "sanitize_text",
]
