"""Shared recursive sanitization for process and durable-artifact boundaries."""

from __future__ import annotations

import math
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


_VERIFIED_EVIDENCE_CAPABILITY = object()


@dataclass(frozen=True)
class _VerifiedEvidenceEnvelope:
    """Process-local evidence capability created after durable verification."""

    payload: object
    content_hash: str
    _capability: object

    def __post_init__(self) -> None:
        if self._capability is not _VERIFIED_EVIDENCE_CAPABILITY:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: verified evidence capability is invalid"
            )

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
_COMPACT_CREDENTIAL_BASES = (
    "authorization",
    "credentials",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "auth",
    "pwd",
    "apikey",
)
_COMPACT_CREDENTIAL_SUFFIXES = ("header", "value")
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
    if _is_compact_credential_key(normalized):
        return True
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


def _is_compact_credential_key(value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", value)
    stem = compact.lower()
    for suffix in _COMPACT_CREDENTIAL_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    for base in _COMPACT_CREDENTIAL_BASES:
        if not stem.endswith(base):
            continue
        prefix = stem[: -len(base)]
        if not prefix or compact.isupper() or compact.islower():
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
    rendered = _SECRET_BEARING_TOKEN.sub(_redact_secret_bearing_token, rendered)
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


def _redact_secret_bearing_token(match: re.Match[str]) -> str:
    token = match.group(0)
    assignment_key = re.split(r"[:=]", token, maxsplit=1)[0].strip("'\"")
    if is_sensitive_key(assignment_key) or "secret" in _key_tokens(token):
        return REDACTED
    return token


def sanitize_structure(value: Any) -> Any:
    """Recursively sanitize JSON-like values while preserving container shape."""

    return _sanitize_structure(
        value,
        allow_parent_verified_evidence=False,
        strict_json=False,
    )


def _sanitize_source_worker_structure(value: Any) -> Any:
    """Normalize and sanitize a nonisolated worker result before persistence."""

    return _sanitize_structure(
        value,
        allow_parent_verified_evidence=False,
        strict_json=True,
    )


def _sanitize_parent_verified_structure(value: Any) -> Any:
    """Consume capabilities created by parent-side durable verification only."""

    return _sanitize_structure(
        value,
        allow_parent_verified_evidence=True,
        strict_json=True,
    )


def _sanitize_structure_for_persistence(
    value: Any,
) -> tuple[Any, tuple[dict[str, object], ...]]:
    """Sanitize a child result and attest only explicit evidence wrappers."""

    attestations: list[dict[str, object]] = []
    sanitized = _sanitize_structure(
        value,
        evidence_attestations=attestations,
        path=(),
        allow_parent_verified_evidence=False,
        strict_json=True,
    )
    return sanitized, tuple(attestations)


def _sanitize_structure(
    value: Any,
    *,
    evidence_attestations: list[dict[str, object]] | None = None,
    path: tuple[str | int, ...] = (),
    allow_parent_verified_evidence: bool,
    strict_json: bool,
) -> Any:
    """Internal recursion used by the public boundary sanitizer."""

    if isinstance(value, _VerifiedEvidenceEnvelope):
        if not allow_parent_verified_evidence:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: verified evidence is invalid at a source boundary"
            )
        return _verified_evidence_as_mapping(value)
    if isinstance(value, EvidencePayload):
        envelope = _seal_evidence_payload(value)
        if evidence_attestations is not None:
            evidence_attestations.append(
                {
                    "path": list(path),
                    "content_hash": envelope["content_hash"],
                }
            )
        return envelope
    if isinstance(value, Mapping):
        for key in value:
            if strict_json and not isinstance(key, str):
                raise SanitizationIntegrityError(
                    "EVIDENCE_INTEGRITY_ERROR: worker result mapping keys must be strings"
                )
            _require_safe_mapping_key(key)
        canonical_envelope = _require_valid_canonical_envelope_if_recognized(value)
        sanitized = {
            key: (
                REDACTED
                if is_sensitive_key(key)
                else _sanitize_structure(
                    item,
                    evidence_attestations=evidence_attestations,
                    path=path + (key,),
                    allow_parent_verified_evidence=allow_parent_verified_evidence,
                    strict_json=strict_json,
                )
            )
            for key, item in value.items()
        }
        if canonical_envelope:
            if sanitized != dict(value):
                raise SanitizationIntegrityError(
                    "EVIDENCE_INTEGRITY_ERROR: boundary sanitization would alter sealed evidence"
                )
            return sanitized if strict_json else value
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_structure(
                item,
                evidence_attestations=evidence_attestations,
                path=path + (index,),
                allow_parent_verified_evidence=allow_parent_verified_evidence,
                strict_json=strict_json,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        normalized = [
            _sanitize_structure(
                item,
                evidence_attestations=evidence_attestations,
                path=path + (index,),
                allow_parent_verified_evidence=allow_parent_verified_evidence,
                strict_json=strict_json,
            )
            for index, item in enumerate(value)
        ]
        return normalized if strict_json else tuple(normalized)
    if isinstance(value, str):
        sanitized_text = sanitize_text(value)
        if strict_json:
            try:
                sanitized_text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise SanitizationIntegrityError(
                    "EVIDENCE_INTEGRITY_ERROR: worker result string is not UTF-8 persistable"
                ) from exc
        return sanitized_text
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not strict_json or math.isfinite(value):
            return value
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: worker result contains a non-finite number"
        )
    if strict_json:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: worker result contains a non-persistable value"
        )
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
    expected_hash = _canonical_envelope_hash(body)
    if expected_hash != content_hash:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: invalid canonical evidence envelope"
        )
    return True


def _seal_evidence_payload(value: EvidencePayload[Any]) -> dict[str, object]:
    _require_typed_evidence_payload(value.payload)
    normalized_payload = _normalize_evidence_value(value.payload)
    _require_typed_evidence_payload(normalized_payload)
    body = {"payload": normalized_payload}
    return {**body, "content_hash": _canonical_envelope_hash(body)}


def _verified_evidence_as_mapping(
    value: _VerifiedEvidenceEnvelope,
) -> dict[str, object]:
    normalized_payload = _normalize_evidence_value(value.payload)
    if normalized_payload != value.payload:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: verified evidence payload is not normalized"
        )
    body = {"payload": normalized_payload}
    envelope = {**body, "content_hash": value.content_hash}
    _require_typed_evidence_payload(value.payload)
    if not _require_valid_canonical_envelope_if_recognized(envelope):  # pragma: no cover
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: verified evidence envelope is invalid"
        )
    return envelope


def _canonical_envelope_hash(body: Mapping[str, object]) -> str:
    try:
        return _project_canonical_hash(body)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: canonical evidence envelope is not hashable"
        ) from exc


def unwrap_evidence_payload(value: object) -> object:
    """Unwrap explicit evidence or a process-locally verified envelope."""

    if isinstance(value, EvidencePayload):
        _require_typed_evidence_payload(value.payload)
        return value.payload
    if isinstance(value, _VerifiedEvidenceEnvelope):
        _verified_evidence_as_mapping(value)
        return value.payload
    raise SanitizationIntegrityError(
        "EVIDENCE_INTEGRITY_ERROR: typed evidence was not verified at its source boundary"
    )


def _restore_verified_evidence_envelopes(
    value: object,
    attestations: object,
) -> object:
    """Restore capabilities after file SHA and envelope attestations are checked."""

    if not isinstance(attestations, (list, tuple)):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: evidence attestations have an invalid schema"
        )
    seen_paths: set[tuple[str | int, ...]] = set()
    for attestation in attestations:
        if not isinstance(attestation, Mapping) or frozenset(attestation) != {
            "path",
            "content_hash",
        }:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: evidence attestation has an invalid schema"
            )
        raw_path = attestation.get("path")
        content_hash = attestation.get("content_hash")
        if not isinstance(raw_path, (list, tuple)) or not all(
            isinstance(segment, str)
            or (isinstance(segment, int) and not isinstance(segment, bool))
            for segment in raw_path
        ):
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: evidence attestation path is invalid"
            )
        path = tuple(raw_path)
        if path in seen_paths:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: duplicate evidence attestation path"
            )
        seen_paths.add(path)
        if not isinstance(content_hash, str) or _CANONICAL_HASH.fullmatch(content_hash) is None:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: evidence attestation hash is invalid"
            )
        envelope = _value_at_path(value, path)
        if not isinstance(envelope, Mapping):
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: attested evidence envelope is missing"
            )
        if not _require_valid_canonical_envelope_if_recognized(envelope):
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: attested evidence envelope is invalid"
            )
        if envelope.get("content_hash") != content_hash:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: evidence attestation hash mismatch"
            )
        payload = envelope.get("payload")
        _require_typed_evidence_payload(payload)
        normalized_payload = _normalize_evidence_value(payload)
        if normalized_payload != payload:
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: attested evidence payload is not normalized"
            )
        verified = _VerifiedEvidenceEnvelope(
            payload=normalized_payload,
            content_hash=content_hash,
            _capability=_VERIFIED_EVIDENCE_CAPABILITY,
        )
        value = _replace_value_at_path(value, path, verified)
    return value


def _value_at_path(value: object, path: tuple[str | int, ...]) -> object:
    current = value
    for segment in path:
        if isinstance(current, Mapping) and isinstance(segment, str):
            if segment not in current:
                break
            current = current[segment]
            continue
        if (
            isinstance(current, (list, tuple))
            and isinstance(segment, int)
            and not isinstance(segment, bool)
            and 0 <= segment < len(current)
        ):
            current = current[segment]
            continue
        break
    else:
        return current
    raise SanitizationIntegrityError(
        "EVIDENCE_INTEGRITY_ERROR: evidence attestation path is missing"
    )


def _replace_value_at_path(
    value: object,
    path: tuple[str | int, ...],
    replacement: object,
) -> object:
    if not path:
        return replacement
    parent = _value_at_path(value, path[:-1])
    segment = path[-1]
    if isinstance(parent, dict) and isinstance(segment, str) and segment in parent:
        parent[segment] = replacement
        return value
    if (
        isinstance(parent, list)
        and isinstance(segment, int)
        and not isinstance(segment, bool)
        and 0 <= segment < len(parent)
    ):
        parent[segment] = replacement
        return value
    raise SanitizationIntegrityError(
        "EVIDENCE_INTEGRITY_ERROR: evidence attestation path cannot be restored"
    )


def _require_typed_evidence_payload(value: object) -> None:
    if not _is_typed_evidence_payload(value):
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: typed evidence payload has an invalid schema"
        )


def _normalize_evidence_value(value: object) -> object:
    """Return the unique strict-JSON representation used for sealing and writing."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise SanitizationIntegrityError(
            "EVIDENCE_INTEGRITY_ERROR: typed evidence contains a non-finite number"
        )
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SanitizationIntegrityError(
                "EVIDENCE_INTEGRITY_ERROR: typed evidence mapping keys must be strings"
            )
        return {
            key: _normalize_evidence_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_evidence_value(item) for item in value]
    raise SanitizationIntegrityError(
        "EVIDENCE_INTEGRITY_ERROR: typed evidence contains a non-JSON value"
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
