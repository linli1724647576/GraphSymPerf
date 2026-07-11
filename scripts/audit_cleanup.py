#!/usr/bin/env python3
"""Write a deletion audit by default; execute only a final APPROVED manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graph_sym_perf.issues.evidence import audit_evidence_inventory
from graph_sym_perf.workspace_cleanup import audit_paths, delete_approved, discover_cleanup_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="audit only (default)")
    mode.add_argument("--execute-approved", action="store_true", help="consume exact APPROVED manifest entries")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="workspace root to audit")
    parser.add_argument("--manifest", type=Path, default=Path("deletion_manifest.json"))
    args = parser.parse_args(argv)

    root = args.root.resolve(strict=True)
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    if args.execute_approved:
        evidence_reasons = audit_evidence_inventory(root / "issues" / "evidence")
        if evidence_reasons:
            raise ValueError(f"evidence inventory is not verified: {', '.join(evidence_reasons)}")
        deleted = delete_approved(manifest_path, root=root)
        print(json.dumps({"deleted": deleted}, indent=2))
        return 0

    items = audit_paths(discover_cleanup_paths(root), root=root)
    payload = {
        "schema_version": 1,
        "dry_run": True,
        "root": str(root),
        "summary": {
            "bytes": sum(int(item["bytes"]) for item in items),
            "file_count": sum(int(item["file_count"]) for item in items),
            "candidate_count": sum(item["status"] == "CANDIDATE" for item in items),
            "protected_count": sum(item["status"] == "PROTECTED" for item in items),
        },
        "items": items,
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote dry-run manifest: {manifest_path}; no files were approved or deleted")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
