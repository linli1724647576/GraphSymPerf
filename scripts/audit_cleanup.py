"""Write a dry-run deletion manifest; never deletes files."""

from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.workspace_cleanup import approve_candidates, audit_paths, delete_approved


def main(argv: list[str] | None = None) -> int:
    mode = (argv or ["--dry-run"])[0]
    manifest_path = Path("deletion_manifest.json")
    if mode == "--approve":
        approve_candidates(manifest_path)
        print("approved exact candidates in deletion_manifest.json")
        return 0
    if mode == "--execute-approved":
        deleted = delete_approved(manifest_path, root=Path.cwd())
        print(json.dumps({"deleted": deleted}, indent=2))
        return 0
    if mode != "--dry-run":
        raise SystemExit("usage: audit_cleanup.py [--dry-run|--approve|--execute-approved]")
    roots = tuple(Path(name) for name in ("runs", "logs", "results", "reports") if Path(name).exists())
    manifest = audit_paths(roots)
    manifest_path.write_text(json.dumps({"dry_run": True, "items": manifest}, indent=2) + "\n", encoding="utf-8")
    print("wrote deletion_manifest.json; no files were deleted")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
