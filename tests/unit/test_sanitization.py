from __future__ import annotations

import pytest

from graph_sym_perf.sanitization import (
    SanitizationIntegrityError,
    sanitize_structure,
    sanitize_text,
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
        "query": "MATCH (n) WHERE n.auth = 'public' RETURN n",
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


def test_text_sanitizer_redacts_entire_multiword_authorization_value() -> None:
    sanitized = sanitize_text(
        "request failed Authorization: Signature keyId=public-part algorithm=hmac opaque-tail; retryable=true"
    )

    assert "Signature" not in sanitized
    assert "keyId" not in sanitized
    assert "opaque-tail" not in sanitized
    assert "retryable=true" in sanitized


def test_recursive_sanitizer_rejects_changes_to_nested_sealed_evidence() -> None:
    sealed = {
        "content_hash": "a" * 64,
        "payload": {
            "message": "Authorization: Custom Scheme sealed-auth-tail; retry=true",
        },
    }

    with pytest.raises(
        SanitizationIntegrityError,
        match="EVIDENCE_INTEGRITY_ERROR",
    ):
        sanitize_structure({"result": {"nested": [sealed]}})


@pytest.mark.parametrize(
    "seal_key",
    ("content_hash", "report_hash", "receipt", "registry_signature"),
)
def test_already_sanitized_sealed_evidence_passes_unchanged(seal_key: str) -> None:
    sealed = {
        seal_key: "b" * 64,
        "payload": {
            "message": "Authorization: <redacted>; retry=true",
            "query": "MATCH (n) WHERE auth = 'public' AND token = 'visible' RETURN n",
        },
    }

    assert sanitize_structure(sealed) is sealed


def test_evidence_fields_preserve_query_result_and_record_string_semantics() -> None:
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

    assert sanitized["query"] == value["query"]
    assert sanitized["query_b"] == value["query_b"]
    assert sanitized["results"][0] == value["results"][0]
    assert sanitized["results"][1]["value"] == value["results"][1]["value"]
    assert sanitized["results"][1]["password"] == "<redacted>"
    assert sanitized["records"][0]["token"] == "<redacted>"
    assert sanitized["records"][0]["value"] == "bolt://literal-result"


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
        "config.neo4j.password": "mapping-password",
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
    ):
        assert forbidden not in rendered


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
