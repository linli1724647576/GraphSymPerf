"""Command-line entrypoint."""

from __future__ import annotations

import argparse

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_gdsmith_mining, run_ldbc_mining
from graph_sym_perf.cluster import write_clustered_summary
from graph_sym_perf.core import ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GraphSymPerf helper CLI.")
    subparsers = parser.add_subparsers(dest="command")
    ldbc = subparsers.add_parser("run-ldbc-mining", help="Run the deterministic LDBC mini mining loop.")
    ldbc.add_argument("--config", default="config/graph_sym_perf.yaml")
    ldbc.add_argument("--systems", default="auto")
    ldbc.add_argument("--load", action="store_true")
    ldbc.add_argument("--max-cases", type=int, default=None)
    ldbc.add_argument("--run-id", default=None)
    ldbc.add_argument("--max-rules-per-case", type=int, default=None)
    ldbc.add_argument(
        "--transform-family",
        choices=("auto", "bool_redundancy", "symmetry"),
        default="auto",
    )
    ldbc_feedback = ldbc.add_mutually_exclusive_group()
    ldbc_feedback.add_argument("--feedback", dest="feedback_enabled", action="store_true")
    ldbc_feedback.add_argument("--no-feedback", dest="feedback_enabled", action="store_false")
    ldbc.set_defaults(feedback_enabled=None)

    mine = subparsers.add_parser("run-gdsmith-mining", help="Run the GDsmith-backed symmetry mining loop.")
    mine.add_argument("--config", default="config/graph_sym_perf.yaml")
    mine.add_argument("--systems", default="neo4j")
    mine.add_argument("--max-cases", type=int, default=None)
    mine.add_argument("--run-id", default=None)
    mine.add_argument("--seed", type=int, default=None)
    mine.add_argument("--max-rules-per-case", type=int, default=None)
    mine.add_argument(
        "--transform-family",
        choices=("auto", "bool_redundancy", "symmetry"),
        default="auto",
    )
    mine_feedback = mine.add_mutually_exclusive_group()
    mine_feedback.add_argument("--feedback", dest="feedback_enabled", action="store_true")
    mine_feedback.add_argument("--no-feedback", dest="feedback_enabled", action="store_false")
    mine.set_defaults(feedback_enabled=None)

    cluster = subparsers.add_parser("cluster", help="Cluster candidate cases from an existing run directory.")
    cluster.add_argument("--run-dir", required=True)
    cluster.add_argument("--no-summary-update", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "run-ldbc-mining":
        try:
            return run_ldbc_mining(
                config_path=args.config,
                systems=args.systems,
                load=args.load,
                max_cases=args.max_cases,
                run_id=args.run_id,
                feedback_enabled=args.feedback_enabled,
                max_rules_per_case=args.max_rules_per_case,
                transform_family=args.transform_family,
            )
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_CONFIG_ERROR
    if args.command == "run-gdsmith-mining":
        try:
            return run_gdsmith_mining(
                config_path=args.config,
                systems=args.systems,
                max_cases=args.max_cases,
                run_id=args.run_id,
                seed=args.seed,
                feedback_enabled=args.feedback_enabled,
                max_rules_per_case=args.max_rules_per_case,
                transform_family=args.transform_family,
            )
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_CONFIG_ERROR
    if args.command == "cluster":
        payload = write_clustered_summary(args.run_dir, update_summary=not args.no_summary_update)
        print(
            "clustered candidates: "
            f"{payload['candidate_count']} candidates, "
            f"{payload['issue_family_count']} issue families, "
            f"{payload['unique_issue_count']} unique issues"
        )
        return 0

    print("GraphSymPerf is installed. Use: graph-sym-perf run-ldbc-mining | run-gdsmith-mining | cluster")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
