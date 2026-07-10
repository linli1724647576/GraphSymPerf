"""Write a dry-run deletion manifest; never deletes files."""

from __future__ import annotations

import json
from pathlib import Path

from graph_sym_perf.workspace_cleanup import audit_paths


def main() -> int:
    roots = tuple(Path(name) for name in ("runs", "logs", "results", "reports") if Path(name).exists())
    manifest = audit_paths(roots)
    Path("deletion_manifest.json").write_text(json.dumps({"dry_run": True, "items": manifest}, indent=2) + "\n", encoding="utf-8")
    print("wrote deletion_manifest.json; no files were deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
