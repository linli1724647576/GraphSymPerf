#!/usr/bin/env python3
"""Export review candidates from an existing GraphSymPerf run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from graph_sym_perf.cluster import write_review_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export GraphSymPerf profile-supported candidates sorted for manual review."
    )
    parser.add_argument("--run-dir", required=True, help="Run artifact directory containing cases.jsonl.")
    parser.add_argument(
        "--no-summary-update",
        action="store_true",
        help="Write review artifacts without updating summary.json.",
    )
    args = parser.parse_args(argv)

    payload = write_review_candidates(args.run_dir, update_summary=not args.no_summary_update)
    print(
        "exported review candidates: "
        f"{payload['candidate_count']} candidates, "
        "artifacts: review_candidates.json, review_candidates.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
