from __future__ import annotations

import json
import pickle
from datetime import datetime, timezone

import pytest

import graph_sym_perf.sanitization as sanitization_module
from graph_sym_perf.experiment.evidence_validation import canonical_hash
from graph_sym_perf.sanitization import (
    EvidencePayload,
    SanitizationIntegrityError,
    sanitize_structure,
    sanitize_text,
    unwrap_evidence_payload,
)


def _persist_and_restore_explicit_evidence(value: object) -> object:
    safe, attestations = sanitization_module._sanitize_structure_for_persistence(value)
    persisted = json.loads(json.dumps(safe))
    return sanitization_module._restore_verified_evidence_envelopes(
        persisted,
        attestations,
    )


def test_recursive_sanitizer_redacts_sensitive_keys_and_leak_bearing_strings() -> None:
    value = {
        "password": "mapping-password",
        "'vendor_secret'": "mapping-secret",
        '"service_token"': "mapping-token",
        "NEO4J_AUTH": "neo4j/mapping-auth",
        "nested": [
            {
                "attempt": 3,
                "ratio": 4.5,
                "passed": False,
                "query": "MATCH (n) WHERE n.auth = 'public' RETURN n",
                "results": ["ordinary result", 7],
            },
            (
                "Traceback (most recent call last):\n"
                '  File "/Users/sakuraq/private/worker.py", line 7\n'
                '{"arbitrary_password": "json-password"}; '
                "{'arbitrary_secret': 'python-secret'}; "
                '"arbitrary_token" = "quoted-token"; '
                "Authorization: Custom Multi Word opaque-auth-value; "
                "bolt://neo4j:uri-password@db.internal:7687 "
                "/Users/sakuraq/private/output.json",
            ),
        ],
    }

    sanitized = sanitize_structure(value)

    assert isinstance(sanitized, dict)
    assert isinstance(sanitized["nested"], list)
    assert isinstance(sanitized["nested"][1], tuple)
    assert sanitized["nested"][0] == {
        "attempt": 3,
        "ratio": 4.5,
        "passed": False,
        "query": "MATCH (n) WHERE <redacted-assignment> RETURN n",
        "results": ["ordinary result", 7],
    }
    rendered = repr(sanitized)
    for forbidden in (
        "mapping-password",
        "mapping-secret",
        "mapping-token",
        "mapping-auth",
        "json-password",
        "python-secret",
        "quoted-token",
        "opaque-auth-value",
        "uri-password",
        "/Users/sakuraq",
        "Traceback",
        "bolt://",
    ):
        assert forbidden not in rendered


def test_evidence_payload_is_pickleable_and_round_trips_through_verified_envelope() -> None:
    payload = {
        "query": "MATCH (n) WHERE auth = 'literal' RETURN n",
        "params": {"password": "literal-parameter"},
        "rows": [
            {
                "message": "Authorization: literal row",
                "password": "literal-column",
            },
        ],
    }
    wrapped = EvidencePayload(payload)

    restored = pickle.loads(pickle.dumps(wrapped))
    verified = _persist_and_restore_explicit_evidence(restored)
    envelope = sanitization_module._sanitize_parent_verified_structure(verified)

    assert set(envelope) == {"payload", "content_hash"}
    assert unwrap_evidence_payload(verified) == payload


def test_serialized_typed_envelope_cannot_claim_source_verified_status() -> None:
    body = {"payload": {"query": "RETURN 1"}}
    envelope = {**body, "content_hash": canonical_hash(body)}

    with pytest.raises(SanitizationIntegrityError, match="source boundary"):
        unwrap_evidence_payload(envelope)


def test_evidence_payload_is_revalidated_after_mutable_payload_drift() -> None:
    wrapped = EvidencePayload({"query": "RETURN 1"})
    wrapped.payload["password"] = "late-credential"

    with pytest.raises(SanitizationIntegrityError, match="EVIDENCE_INTEGRITY_ERROR"):
        sanitize_structure(wrapped)
    with pytest.raises(SanitizationIntegrityError, match="EVIDENCE_INTEGRITY_ERROR"):
        unwrap_evidence_payload(wrapped)


def test_text_sanitizer_redacts_entire_multiword_authorization_value() -> None:
    sanitized = sanitize_text(
        "request failed Authorization: Signature keyId=public-part algorithm=hmac opaque-tail; retryable=true"
    )

    assert "Signature" not in sanitized
    assert "keyId" not in sanitized
    assert "opaque-tail" not in sanitized
    assert "retryable=true" in sanitized


def test_recursive_sanitizer_rejects_changes_to_nested_sealed_evidence() -> None:
    body = {
        "payload": {
            "message": "Authorization: Custom Scheme sealed-auth-tail; retry=true",
        },
    }
    sealed = {**body, "content_hash": canonical_hash(body)}

    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure({"diagnostics": {"nested": [sealed]}})


def test_valid_sanitized_canonical_envelope_passes_unchanged() -> None:
    body = {
        "payload": {
            "message": "Authorization: <redacted>; retry=true",
            "query": "MATCH (n) RETURN n",
        },
    }
    sealed = {**body, "content_hash": canonical_hash(body)}

    assert sanitize_structure(sealed) is sealed


@pytest.mark.parametrize("content_hash", ("not-a-hash", "a" * 64))
def test_invalid_canonical_envelope_claim_fails_closed(content_hash: str) -> None:
    envelope = {
        "payload": {"message": "already safe"},
        "content_hash": content_hash,
    }

    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(envelope)


@pytest.mark.parametrize(
    "error",
    (TypeError("type"), ValueError("value"), ArithmeticError("arithmetic")),
)
def test_canonical_hash_errors_are_wrapped_as_sanitization_integrity(
    monkeypatch,
    error: Exception,
) -> None:
    def fail_hash(_payload):
        raise error

    monkeypatch.setattr(sanitization_module, "_project_canonical_hash", fail_hash)

    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(
            {
                "payload": {"message": "ordinary value"},
                "content_hash": "a" * 64,
            }
        )


@pytest.mark.parametrize("non_finite", (float("nan"), float("inf")))
def test_non_finite_canonical_envelope_payload_fails_with_integrity_error(
    non_finite: float,
) -> None:
    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(
            {
                "payload": {"message": non_finite},
                "content_hash": "a" * 64,
            }
        )


def test_non_finite_typed_evidence_initial_seal_fails_with_integrity_error() -> None:
    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(EvidencePayload({"rows": [float("nan")]}))


@pytest.mark.parametrize(
    "unsupported",
    (
        b"database-bytes",
        datetime(2026, 7, 16, tzinfo=timezone.utc),
        {"unordered", "set"},
        object(),
    ),
    ids=("bytes", "datetime", "set", "object"),
)
def test_typed_evidence_rejects_non_json_persistence_values(
    unsupported: object,
) -> None:
    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(EvidencePayload({"rows": [unsupported]}))


def test_typed_evidence_is_normalized_before_its_persisted_hash_is_computed() -> None:
    envelope = sanitize_structure(
        EvidencePayload(
            {
                "rows": (
                    {"nested": (1, 2, 3)},
                )
            }
        )
    )

    assert envelope["payload"] == {"rows": [{"nested": [1, 2, 3]}]}
    assert envelope["content_hash"] == canonical_hash(
        {"payload": envelope["payload"]}
    )


def test_typed_evidence_rejects_non_string_mapping_keys() -> None:
    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(EvidencePayload({"rows": [{1: "integer-key"}]}))


def test_marker_named_fields_do_not_claim_a_generic_seal() -> None:
    ordinary = {
        "content_hash": "not-a-seal",
        "report_hash": "b" * 64,
        "receipt": "literal receipt",
        "signature": "literal signature",
        "password": "opaque-password-value",
        "message": "Authorization: Custom Scheme diagnostic-tail; retry=true",
    }

    sanitized = sanitize_structure(ordinary)

    assert sanitized["content_hash"] == "not-a-seal"
    assert sanitized["report_hash"] == "b" * 64
    assert sanitized["receipt"] == "literal receipt"
    assert sanitized["signature"] == "literal signature"
    assert sanitized["password"] == "<redacted>"
    assert "diagnostic-tail" not in sanitized["message"]


def test_ordinary_evidence_named_fields_do_not_activate_preservation() -> None:
    value = {
        "query": "MATCH (n) WHERE auth = 'public' AND token = 'visible' RETURN n",
        "query_b": "RETURN '/public/result path' AS value",
        "results": [
            "Authorization: literal result value",
            {"value": "/Users/example/result value", "password": "result-secret"},
        ],
        "records": (
            {"token": "record-secret", "value": "bolt://literal-result"},
        ),
    }

    sanitized = sanitize_structure(value)

    rendered = repr(sanitized)
    for forbidden in (
        "'public'",
        "'visible'",
        "/public/result path",
        "literal result value",
        "/Users/example",
        "result-secret",
        "record-secret",
        "bolt://literal-result",
    ):
        assert forbidden not in rendered
    assert sanitized["results"][1]["password"] == "<redacted>"
    assert sanitized["records"][0]["token"] == "<redacted>"


def test_typed_evidence_subtrees_preserve_all_database_values_and_keys() -> None:
    payload = {
        "query": "MATCH (n) WHERE token = 'literal' RETURN n",
        "params": {
            "password": "literal-password-parameter",
            "message": "Authorization: literal parameter",
        },
        "rows": [
            {
                "content_hash": "not-a-seal",
                "receipt": "literal-receipt-column",
                "signature": "literal-signature-column",
                "password": "literal-password-column",
                "token": "literal-token-column",
                "error": "file:///Users/example/Literal Row/File Name.txt",
                "reason": "auth=literal-row-value",
                "bolt://literal-key": "literal-key-value",
                "leaked-secret-material": "literal-secret-key-value",
                "nested_invalid_envelope": {
                    "payload": {"message": "literal nested row"},
                    "content_hash": "not-a-hash",
                },
            },
        ],
        "results": ["Authorization: literal result"],
    }

    verified_result = _persist_and_restore_explicit_evidence(
        {"evidence": EvidencePayload(payload)}
    )
    restored = unwrap_evidence_payload(verified_result["evidence"])

    assert restored == payload
    assert isinstance(restored["rows"], list)


def test_invalid_nested_envelope_under_ordinary_results_key_fails_closed() -> None:
    value = {
        "results": [
            {
                "payload": {"message": "ordinary nested value"},
                "content_hash": "not-a-hash",
            }
        ]
    }

    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure(value)


def test_diagnostic_fields_clean_dotted_assignments_and_full_spaced_paths() -> None:
    value = {
        "message": (
            "config.neo4j.password = dotted-password; "
            "{'adapter_token': 'quoted-token'}; "
            'failed at "/Users/sakuraq/My Secret Folder/output file.json"'
        ),
        "reason_codes": ["Authorization: Custom Scheme diagnostic-auth-tail; retry=true"],
        "details": (
            "failed at /Users/sakuraq/My Other Secret Folder/unquoted output.json; "
            "retry=true"
        ),
        "error": (
            "token=bare-token auth = bare-auth; "
            "config.neo4j.Authorization = Signature keyId=public algorithm=hmac "
            "dotted-auth-tail; retry=true; "
            'failed reading "file:///Users/sakuraq/My URI Folder/output file.db"'
        ),
        "config.neo4j.password": "mapping-password",
        "password_value": "hunter2",
        "access_token_value": "opaque-access-123",
        "authorization_header": "Bearer abc.def",
        "credentials": {"user": "neo4j", "pass": "opaque-pass"},
    }

    rendered = repr(sanitize_structure(value))

    for forbidden in (
        "dotted-password",
        "quoted-token",
        "/Users/sakuraq",
        "My Secret Folder",
        "My Other Secret Folder",
        "diagnostic-auth-tail",
        "mapping-password",
        "bare-token",
        "bare-auth",
        "dotted-auth-tail",
        "keyId",
        "My URI Folder",
        "hunter2",
        "opaque-access-123",
        "abc.def",
        "opaque-pass",
    ):
        assert forbidden not in rendered
    assert "retry=true" in rendered


@pytest.mark.parametrize(
    "key",
    (
        "password_value",
        "access-token-value",
        "config.neo4j.password",
        "passwordValue",
        "accessToken",
        "authorizationHeader",
        "neo4jPassword",
        "clientSecret",
        "AccessTokenValue",
        "NEO4J_PASSWORD",
        "APIKey",
        "PassWord",
        "pAssword",
        "PASSword",
        "passWord",
        "PASSWORDVALUE",
        "ACCESSTOKEN",
        "TOKENVALUE",
        "TOKENHEADER",
        "AUTHHEADER",
        "AUTHVALUE",
        "CREDENTIALVALUE",
        "CREDENTIALSVALUE",
        "PASSWDVALUE",
        "PWDVALUE",
        "APIKEYHEADER",
        "NEO4JPASSWORDHEADER",
        "USERPASSWORD",
        "REDISPASSWORD",
        "AZURECLIENTSECRET",
        "JWTSECRET",
        "REFRESHTOKEN",
        "redispassword",
        "REDISPASSWORDHEADER",
    ),
)
def test_operational_sensitive_key_tokenizer_covers_common_naming_styles(
    key: str,
) -> None:
    assert sanitize_structure({key: "opaque-credential"}) == {key: "<redacted>"}


def test_compact_credential_grammar_composes_prefix_base_and_suffix() -> None:
    for base in (
        "auth",
        "authorization",
        "credential",
        "credentials",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    ):
        for suffix in ("", "value", "header"):
            key = f"{base}{suffix}".upper()
            assert sanitize_structure({key: "opaque-credential"}) == {
                key: "<redacted>"
            }
    for prefix in ("neo4j", "client", "vendor"):
        for base in ("password", "secret", "token"):
            for suffix in ("", "value", "header"):
                key = f"{prefix}{base}{suffix}".upper()
                assert sanitize_structure({key: "opaque-credential"}) == {
                    key: "<redacted>"
                }


@pytest.mark.parametrize(
    "key",
    (
        "compassWord",
        "tokenizer",
        "authorizationStatus",
        "secretary",
        "passage",
        "accessTokenization",
    ),
)
def test_compact_credential_detection_avoids_ordinary_key_false_positives(
    key: str,
) -> None:
    assert sanitize_structure({key: "ordinary-value"}) == {
        key: "ordinary-value"
    }


def test_typed_evidence_columns_are_not_interpreted_as_operational_keys() -> None:
    payload = {
        "rows": [
            {
                "passwordValue": "literal-password-column",
                "accessToken": "literal-token-column",
                "authorizationHeader": "literal-header-column",
                "neo4jPassword": "literal-neo4j-column",
                "clientSecret": "literal-secret-column",
            }
        ]
    }

    verified = _persist_and_restore_explicit_evidence(EvidencePayload(payload))

    assert unwrap_evidence_payload(verified) == payload


@pytest.mark.parametrize(
    "unsafe_key",
    (
        "bolt://neo4j:key-secret@db.internal:7687",
        "/Users/sakuraq/My Secret Folder/output.json",
        "leaked-secret-material",
    ),
)
def test_mapping_keys_with_leak_bearing_material_fail_closed(unsafe_key: str) -> None:
    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure({unsafe_key: "value"})
