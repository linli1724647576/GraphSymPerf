from __future__ import annotations

from graph_sym_perf.sanitization import sanitize_structure, sanitize_text


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
