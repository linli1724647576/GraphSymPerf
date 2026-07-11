import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path("scripts/migrate_public_issue_evidence.py")
    spec = importlib.util.spec_from_file_location("graphsymperf_issue_migrator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_attachment_policy_rejects_arbitrary_hosts_and_private_dns(monkeypatch) -> None:
    module = _module()
    with pytest.raises(ValueError, match="approved public GitHub host"):
        module._validate_public_attachment_url("https://example.com/reproducer.cypher")

    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="non-public address"):
        module._validate_public_attachment_url("https://github.com/user-attachments/files/1/reproducer.cypher")


def test_runtime_claim_records_only_machine_readable_runtime_ratio() -> None:
    module = _module()
    source = "https://github.com/example/project/issues/1"
    assert module._runtime_claim("Queries show a 16.2x runtime gap", source) == {
        "status": "public_issue_claim_only_not_v3",
        "median_ratio": 16.2,
        "source_url": source,
    }
    assert module._runtime_claim("PROFILE rows differ by 100x", source)["status"] == "unknown"
