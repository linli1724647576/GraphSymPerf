#!/usr/bin/env python3
"""Run the GDsmith-backed GraphSymPerf mining loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_gdsmith_mining
from graph_sym_perf.core import ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run GraphSymPerf with GDsmith as the Cypher query generator.")
    parser.add_argument("--config", default="config/graph_sym_perf.yaml", help="Path to the run configuration YAML.")
    parser.add_argument("--systems", default="neo4j", help="GDsmith v1 supports only neo4j.")
    parser.add_argument("--max-cases", type=int, default=None, help="Override the configured maximum number of cases.")
    parser.add_argument("--run-id", default=None, help="Override the generated run id.")
    parser.add_argument("--seed", type=int, default=None, help="Override query_generation.gdsmith.random_seed.")
    args = parser.parse_args(argv)

    try:
        code = run_gdsmith_mining(
            config_path=args.config,
            systems=args.systems,
            max_cases=args.max_cases,
            run_id=args.run_id,
            seed=args.seed,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if code == 0:
        print("GraphSymPerf GDsmith mining run completed.")
    elif code == 2:
        print("No configured real database target passed preflight.", file=sys.stderr)
    elif code == 3:
        print("GDsmith preflight failed; inspect preflight.json and summary.json.", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
