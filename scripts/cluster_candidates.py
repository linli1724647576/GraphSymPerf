#!/usr/bin/env python3
"""Cluster candidate cases from an existing GraphSymPerf run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from graph_sym_perf.cluster import write_clustered_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cluster GraphSymPerf candidate cases from cases.jsonl.")
    parser.add_argument("--run-dir", required=True, help="Run artifact directory containing cases.jsonl.")
    parser.add_argument(
        "--no-summary-update",
        action="store_true",
        help="Write clustered artifacts without updating summary.json.",
    )
    args = parser.parse_args(argv)

    summary = write_clustered_summary(args.run_dir, update_summary=not args.no_summary_update)
    print(
        "clustered candidates: "
        f"{summary['candidate_count']} candidates, "
        f"{summary['issue_family_count']} issue families, "
        f"{summary['unique_issue_count']} unique issues"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
