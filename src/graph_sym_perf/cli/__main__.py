"""Command-line entrypoint."""

from __future__ import annotations

import argparse

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_gdsmith_mining
from graph_sym_perf.core import ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GraphSymPerf helper CLI.")
    subparsers = parser.add_subparsers(dest="command")
    mine = subparsers.add_parser("run-gdsmith-mining", help="Run the GDsmith-backed symmetry mining loop.")
    mine.add_argument("--config", default="config/graph_sym_perf.yaml")
    mine.add_argument("--systems", default="neo4j")
    mine.add_argument("--max-cases", type=int, default=None)
    mine.add_argument("--run-id", default=None)
    mine.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command != "run-gdsmith-mining":
        print("GraphSymPerf is installed. Use: graph-sym-perf run-gdsmith-mining")
        return 0
    try:
        return run_gdsmith_mining(
            config_path=args.config,
            systems=args.systems,
            max_cases=args.max_cases,
            run_id=args.run_id,
            seed=args.seed,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
