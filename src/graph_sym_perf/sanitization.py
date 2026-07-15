"""Shared recursive sanitization for process and durable-artifact boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


REDACTED = "<redacted>"
T = TypeVar("T")


@dataclass(frozen=True)
class EvidencePayload(Generic[T]):
    """Explicit trust boundary for typed query/result evidence."""

    payload: T

    def __post_init__(self) -> None:
        _require_typed_evidence_payload(self.payload)


class SanitizationIntegrityError(ValueError):
    """A generic boundary would invalidate already sealed evidence."""

_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[._-]+")
_CREDENTIAL_KEY_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "credentials",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    }
)
_CREDENTIAL_KEY_SUFFIX_TOKENS = frozenset({"header", "value"})
_PLAIN_INLINE_SECRET_NAME = (
    r"(?:password|passwd|pwd|secret|token|authorization|credential|api[_-]?key)"
)
_PREFIXED_INLINE_SECRET_NAME = (
    r"(?:(?:[A-Za-z0-9]+[_-])+"
    r"(?:password|passwd|pwd|secret|token|auth))"
)
_INLINE_SECRET_NAME = (
    rf"(?:{_PLAIN_INLINE_SECRET_NAME}|{_PREFIXED_INLINE_SECRET_NAME})"
)
_INLINE_AUTH_NAME = r"(?:authorization|auth|(?:[A-Za-z0-9]+[_-])+auth)"
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
_DOTTED_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<dotted_auth_quote>['\"]?)"
    r"(?:[A-Za-z0-9_-]+\.)+"
    r"(?:authorization|auth)"
    r"(?P=dotted_auth_quote)\s*[:=]\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^;\r\n]*)",
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
_QUOTED_FILE_URI = re.compile(
    r"(?P<file_uri_quote>['\"])file://[^\r\n]*?(?P=file_uri_quote)",
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
_CANONICAL_HASH = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_ENVELOPE_KEYS = frozenset({"payload", "content_hash"})
_TYPED_EVIDENCE_KEYS = frozenset({"query", "params", "rows", "results"})


def is_sensitive_key(value: object) -> bool:
    """Return whether a structured key names a credential-bearing value."""

    normalized = _normalized_key(value)
    if normalized is None:
        return False
    tokens = _key_tokens(normalized)
    for index, token in enumerate(tokens):
        if (
            token in _CREDENTIAL_KEY_TOKENS
            and all(
                suffix in _CREDENTIAL_KEY_SUFFIX_TOKENS
                for suffix in tokens[index + 1 :]
            )
        ):
            return True
        if (
            token == "api"
            and tokens[index : index + 2] == ("api", "key")
            and all(
                suffix in _CREDENTIAL_KEY_SUFFIX_TOKENS
                for suffix in tokens[index + 2 :]
            )
        ):
            return True
    return False


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


def _key_tokens(value: str) -> tuple[str, ...]:
    separated = _KEY_SEPARATORS.sub(" ", value)
    tokens: list[str] = []
    for chunk in separated.split():
        chunk = _CAMEL_ACRONYM_BOUNDARY.sub(" ", chunk)
        chunk = _CAMEL_WORD_BOUNDARY.sub(" ", chunk)
        tokens.extend(part.lower() for part in chunk.split() if part)
    return tuple(tokens)


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
    rendered = _DOTTED_AUTHORIZATION_ASSIGNMENT.sub(
        "<redacted-assignment>",
        rendered,
    )
    rendered = _DOTTED_SENSITIVE_ASSIGNMENT.sub("<redacted-assignment>", rendered)
    rendered = _AUTHORIZATION_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _SENSITIVE_ASSIGNMENT.sub(r"\g<prefix><redacted>", rendered)
    rendered = _QUOTED_FILE_URI.sub("<file-uri>", rendered)
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

    return _sanitize_structure(value)


def _sanitize_structure(value: Any) -> Any:
    """Internal recursion used by the public boundary sanitizer."""

    if isinstance(value, EvidencePayload):
        return _seal_evidence_payload(value)
    if isinstance(value, Mapping):
        for key in value:
            _require_safe_mapping_key(key)
        canonical_envelope = _require_valid_canonical_envelope_if_recognized(value)
        if canonical_envelope and _is_typed_evidence_payload(value.get("payload")):
            return value
        sanitized = {
            key: (
                REDACTED
                if is_sensitive_key(key)
                else _sanitize_structure(item)
            )
            for key, item in value.items()
        }
        if canonical_envelope:
            if sanitized != dict(value):
                raise SanitizationIntegrityError(
                    "EVIDENCE_INTEGRITY_ERROR: boundary sanitization would alter sealed evidence"
                )
            return value
        return sanitized
    if isinstance(value, list):
        return [_sanitize_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_structure(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


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


def _require_valid_canonical_envelope_if_recognized(
    value: Mapping[Any, Any],
) -> bool:
    if frozenset(value) != _CANONICAL_ENVELOPE_KEYS:
        return False
    content_hash = value.get("content_hash")
    body = {"payload": value.get("payload")}
    if not isinstance(content_hash, str) or _CANONICAL_HASH.fullmatch(content_hash) is None:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: invalid canonical evidence envelope"
        )
    try:
        expected_hash = _project_canonical_hash(body)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: canonical evidence envelope is not hashable"
        ) from exc
    if expected_hash != content_hash:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: invalid canonical evidence envelope"
        )
    return True


def _seal_evidence_payload(value: EvidencePayload[Any]) -> dict[str, object]:
    _require_typed_evidence_payload(value.payload)
    body = {"payload": value.payload}
    return {**body, "content_hash": _project_canonical_hash(body)}


def unwrap_evidence_payload(value: object) -> object:
    """Validate and unwrap an explicit typed-evidence value or serialized envelope."""

    if isinstance(value, EvidencePayload):
        _require_typed_evidence_payload(value.payload)
        return value.payload
    if not isinstance(value, Mapping):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: typed evidence must be a canonical envelope"
        )
    if not _require_valid_canonical_envelope_if_recognized(value):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: typed evidence must be a canonical envelope"
        )
    payload = value.get("payload")
    _require_typed_evidence_payload(payload)
    return payload


def _require_typed_evidence_payload(value: object) -> None:
    if not _is_typed_evidence_payload(value):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: typed evidence payload has an invalid schema"
        )


def _is_typed_evidence_payload(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and all(isinstance(key, str) for key in value)
        and frozenset(value).issubset(_TYPED_EVIDENCE_KEYS)
    )


def _project_canonical_hash(payload: object) -> str:
    # Import lazily because evidence_validation reaches runtime.records, which
    # imports this boundary sanitizer during package initialization.
    from graph_sym_perf.experiment.evidence_validation import canonical_hash

    return canonical_hash(payload)


__all__ = [
    "EvidencePayload",
    "REDACTED",
    "SanitizationIntegrityError",
    "is_sensitive_key",
    "sanitize_structure",
    "sanitize_text",
    "unwrap_evidence_payload",
]
