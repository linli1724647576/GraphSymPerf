#!/usr/bin/env python3
"""Rebuild historical issue packages from public GitHub pages only.

This script deliberately does not read legacy local run/BUG artifacts. Missing
v3 validation and reducer evidence is represented explicitly as unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from graph_sym_perf.issues.evidence import REQUIRED_EVIDENCE_FILES, IssueEvidence, write_evidence_package


ISSUES = {
    "apache-age-2428": ("apache/age", 2428, "apache_age", "uninterested", "null_contradiction"),
    "arcadedb-4405": ("ArcadeData/arcadedb", 4405, "arcadedb", "fixed", "multilayer_redundancy_experimental"),
    "arcadedb-5116": ("ArcadeData/arcadedb", 5116, "arcadedb", "fixed", "pattern_order_symmetry"),
    "arcadedb-5117": ("ArcadeData/arcadedb", 5117, "arcadedb", "fixed", "pattern_order_symmetry"),
    "falkordb-2081": ("FalkorDB/FalkorDB", 2081, "falkordb", "waiting_reply", "removed:and_double_negated_true_or"),
    "ladybugdb-668": ("LadybugDB/ladybug", 668, "ladybugdb", "waiting_reply", "removed:and_double_negated_true_or"),
    "ladybugdb-669": ("LadybugDB/ladybug", 669, "ladybugdb", "waiting_reply", "null_complete_excluded_middle"),
    "memgraph-4208": ("memgraph/memgraph", 4208, "memgraph", "confirmed", "guarded_contradiction_experimental"),
    "neo4j-13783": ("neo4j/neo4j", 13783, "neo4j", "fixed", "constant_false_conjunction"),
    "neo4j-13813": ("neo4j/neo4j", 13813, "neo4j", "confirmed", "pattern_order_symmetry"),
    "neo4j-13814": ("neo4j/neo4j", 13814, "neo4j", "uninterested", "null_contradiction"),
}
_MAX_PUBLIC_ATTACHMENT_BYTES = 10 * 1024 * 1024
_PUBLIC_ATTACHMENT_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "user-images.githubusercontent.com",
        "user-attachments.githubusercontent.com",
        "github-production-user-asset-6210df.s3.amazonaws.com",
    }
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="issues/evidence")
    parser.add_argument("--last-checked", default="2026-07-11")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    if args.inventory_only:
        for package in sorted(path for path in root.iterdir() if path.is_dir()):
            _write_package_inventory(package)
        _write_global_inventory(root)
        return 0
    for issue_id, (repository, number, dbms, status, rule_id) in ISSUES.items():
        payload = _github_issue(repository, number)
        package = root / issue_id
        if package.exists():
            unexpected = {path.name for path in package.iterdir()} - {"metadata.json"}
            generated_contract = {
                "metadata.yaml", "setup.cypher", "original.cypher", "transformed.cypher",
                "reduced_original.cypher", "reduced_transformed.cypher", "validation.json",
                "README.md", "evidence_inventory.json",
            }
            if unexpected and not (args.refresh and {path.name for path in package.iterdir()} == generated_contract):
                raise RuntimeError(f"refusing to replace non-skeleton package {package}: {sorted(unexpected)}")
            shutil.rmtree(package)
        body = str(payload.get("body") or "")
        issue_url = str(payload.get("html_url") or f"https://github.com/{repository}/issues/{number}")
        queries = _query_blocks(body)
        setup, setup_source = _public_setup(body)
        unknown = "unknown: public issue body does not expose this artifact"
        files = {
            "setup.cypher": setup or f"// {unknown}\n",
            "original.cypher": (queries[0] + "\n") if queries else f"// {unknown}\n",
            "transformed.cypher": (queries[1] + "\n") if len(queries) > 1 else f"// {unknown}\n",
            "reduced_original.cypher": "// unknown: no v3 reducer evidence exists for this historical issue\n",
            "reduced_transformed.cypher": "// unknown: no v3 reducer evidence exists for this historical issue\n",
            "validation.json": json.dumps(
                {
                    "revalidated_by_v3": False,
                    "reason": "historical public issue; independent two-session runtime validation was not run",
                    "source_url": issue_url,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "README.md": (
                f"# {issue_id} evidence boundary\n\n"
                f"Source: {issue_url}\n\n"
                "This package was reconstructed only from the public issue body/attachment. "
                "It is historical evidence, not a v3 validation result. Missing reduced queries, "
                "paired timings, fingerprints, and protocol hashes remain explicitly unknown.\n"
            ),
        }
        metadata = {
            "issue_url": issue_url,
            "title": str(payload.get("title") or "unknown"),
            "dbms": dbms,
            "dbms_version": _version(body),
            "detector_version": "historical_pre_v3_unknown",
            "rule_id": rule_id,
            "rule_version": "historical_unknown",
            "validator_version": "not_run_v3",
            "reducer_version": "not_run_v3",
            "discovery_runtime": _runtime_claim(f"{payload.get('title') or ''}\n{body}", issue_url),
            "validated_runtime": "unknown",
            "developer_status": status,
            "github_state": str(payload.get("state") or "unknown"),
            "github_state_reason": str(payload.get("state_reason") or "unknown"),
            "closed_at": payload.get("closed_at") or "unknown",
            "status_evidence_url": issue_url,
            "status_source": "historical_registry_label plus current public GitHub state",
            "setup_source_url": setup_source or "unknown",
            "last_checked_at": args.last_checked,
            "revalidated_by_v3": False,
            "unknown_reason": "v3 validation/reducer artifacts do not exist; no deleted local artifact was restored",
        }
        write_evidence_package(root, IssueEvidence(issue_id, metadata, files))
    _write_global_inventory(root)
    return 0


def _github_issue(repository: str, number: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/issues/{number}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "GraphSymPerf-evidence-migrator"},
    )
    value = None
    error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)
            break
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    if value is None:
        raise RuntimeError(f"failed to fetch {repository}#{number}: {error}")
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub issue response is not an object: {repository}#{number}")
    return value


def _query_blocks(body: str) -> tuple[str, ...]:
    blocks = []
    for match in re.finditer(r"```(?:cypher|text|sql)[ \t]*\r?\n(?P<body>.*?)```", body, re.IGNORECASE | re.DOTALL):
        block = match.group("body").strip()
        upper = block.upper()
        if "MATCH" in upper and "RETURN" in upper and "PROFILE EXCERPT" not in upper and "COUNT(" not in upper:
            cleaned = re.sub(
                r"^(?:PROFILE|EXPLAIN[^\n]*|GRAPH\.PROFILE[^\n]*)\n",
                "",
                block,
                count=1,
                flags=re.IGNORECASE,
            )
            blocks.append(cleaned.strip())
    profiled = [block for block in blocks if "MATCH" in block.upper()]
    return tuple(profiled[:2])


def _public_setup(body: str) -> tuple[str | None, str | None]:
    links = re.findall(r"\[[^]]*(?:reproducer|setup|graph)[^]]*]\((https://[^)]+)\)", body, re.IGNORECASE)
    for link in links:
        try:
            _validate_public_attachment_url(link)
            request = urllib.request.Request(link, headers={"User-Agent": "GraphSymPerf-evidence-migrator"})
            with urllib.request.urlopen(request, timeout=30) as response:
                _validate_public_attachment_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > _MAX_PUBLIC_ATTACHMENT_BYTES:
                    raise ValueError("public attachment exceeds the 10 MiB evidence limit")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_PUBLIC_ATTACHMENT_BYTES:
                        raise ValueError("public attachment exceeds the 10 MiB evidence limit")
                    chunks.append(chunk)
                content = b"".join(chunks).decode("utf-8")
            if content.strip():
                return content, link
        except Exception:
            continue
    return None, links[0] if links else None


def _validate_public_attachment_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in _PUBLIC_ATTACHMENT_HOSTS:
        raise ValueError("evidence attachment must use an approved public GitHub host")
    if hostname == "github.com" and not parsed.path.startswith("/user-attachments/"):
        raise ValueError("GitHub evidence URL is not a public user attachment")
    for answer in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise ValueError("evidence attachment resolved to a non-public address")


def _version(body: str) -> str:
    for pattern in (
        r"(?:DBMS|Neo4j|Memgraph|FalkorDB|ArcadeDB|Apache AGE|Ladybug) Version:\s*(.+)",
        r"### Ladybug version\s*\n+\s*(.+)",
    ):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            value = match.group(1).strip().replace("**", "").strip().rstrip(",;")
            if value.startswith("`") and value.endswith("`"):
                value = value[1:-1]
            return value
    return "unknown"


def _runtime_claim(text: str, source_url: str) -> dict[str, object]:
    patterns = (
        r"(?P<ratio>\d+(?:\.\d+)?)\s*[x×]\s*runtime",
        r"runtime(?:\s+gap|\s+ratio)?[^\n]{0,80}?(?P<ratio>\d+(?:\.\d+)?)\s*[x×]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return {
                "status": "public_issue_claim_only_not_v3",
                "median_ratio": float(match.group("ratio")),
                "source_url": source_url,
            }
    return {
        "status": "unknown",
        "reason": "public issue has no machine-readable ordinary-execute runtime ratio",
        "source_url": source_url,
    }


def _write_package_inventory(package: Path) -> None:
    files = []
    for name in sorted(REQUIRED_EVIDENCE_FILES):
        path = package / name
        if not path.is_file():
            raise RuntimeError(f"cannot inventory incomplete evidence package: {package / name}")
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    (package / "evidence_inventory.json").write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_global_inventory(root: Path) -> None:
    packages = []
    for package in sorted(path for path in root.iterdir() if path.is_dir()):
        metadata = package / "metadata.yaml"
        inventory = package / "evidence_inventory.json"
        packages.append(
            {
                "id": package.name,
                "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
                "inventory_sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
            }
        )
    (root.parent / "evidence_inventory.json").write_text(
        json.dumps({"schema_version": 2, "packages": packages}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
