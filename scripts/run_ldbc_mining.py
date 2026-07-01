#!/usr/bin/env python3
"""Run the deterministic LDBC mini mining loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_ldbc_mining
from graph_sym_perf.core import ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run GraphSymPerf against real graph databases.")
    parser.add_argument("--config", default="config/graph_sym_perf.yaml", help="Path to the run configuration YAML.")
    parser.add_argument(
        "--systems",
        default="auto",
        help="auto or a comma-separated subset: neo4j,memgraph,falkordb,agensgraph,apache_age,arcadedb,kuzu.",
    )
    parser.add_argument("--load", action="store_true", help="Load the controlled LDBC mini dataset before mining.")
    parser.add_argument("--max-cases", type=int, default=None, help="Override the configured maximum number of cases.")
    parser.add_argument("--run-id", default=None, help="Override the generated run id.")
    args = parser.parse_args(argv)

    try:
        code = run_ldbc_mining(
            config_path=args.config,
            systems=args.systems,
            load=args.load,
            max_cases=args.max_cases,
            run_id=args.run_id,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if code == 0:
        print("GraphSymPerf LDBC mining run completed.")
    elif code == 2:
        print("No configured real database target passed preflight.", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
