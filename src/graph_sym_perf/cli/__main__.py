"""Command-line entrypoint."""

from __future__ import annotations

import argparse

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_gdsmith_mining, run_ldbc_mining
from graph_sym_perf.cluster import write_clustered_summary
from graph_sym_perf.core import ConfigError
from graph_sym_perf.experiment import aggregate_run_root, run_doctor, run_experiment, run_triage, validate_triage


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

    doctor = subparsers.add_parser("doctor", help="Run experiment preflight checks from a manifest.")
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--strict", action="store_true")

    experiment = subparsers.add_parser("experiment", help="Run or aggregate sharded experiments.")
    experiment_subparsers = experiment.add_subparsers(dest="experiment_command")
    experiment_run = experiment_subparsers.add_parser("run", help="Run one experiment shard from a manifest.")
    experiment_run.add_argument("--manifest", required=True)
    experiment_run.add_argument("--shard", type=int, default=None)
    experiment_run.add_argument("--num-shards", type=int, default=None)
    experiment_aggregate = experiment_subparsers.add_parser("aggregate", help="Aggregate shard outputs.")
    experiment_aggregate.add_argument("--run-root", required=True)

    triage = subparsers.add_parser("triage", help="Rank top unique issues from an aggregated run root.")
    triage.add_argument("--run-dir", required=True)
    triage.add_argument("--top", type=int, default=100)

    validate = subparsers.add_parser("validate", help="Replay triaged representatives.")
    validate.add_argument("--triage", required=True)
    validate.add_argument("--runtime-repeats", type=int, default=15)
    validate.add_argument("--reduce", action="store_true")
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
    if args.command == "doctor":
        try:
            return run_doctor(args.config, strict=args.strict)
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_CONFIG_ERROR
    if args.command == "experiment":
        if args.experiment_command == "run":
            try:
                return run_experiment(args.manifest, shard=args.shard, num_shards=args.num_shards)
            except ConfigError as exc:
                print(f"configuration error: {exc}")
                return EXIT_CONFIG_ERROR
        if args.experiment_command == "aggregate":
            payload = aggregate_run_root(args.run_root)
            print(
                "aggregated shards: "
                f"{payload['candidate_count']} unique candidate signatures, "
                f"{payload['unique_issue_count']} unique issues"
            )
            return 0
    if args.command == "triage":
        payload = run_triage(args.run_dir, top=args.top)
        print(f"triaged {len(payload['items'])} representatives into triage_top{args.top}.json")
        return 0
    if args.command == "validate":
        payload = validate_triage(args.triage, runtime_repeats=args.runtime_repeats, reduce=args.reduce)
        print(
            "validated representatives: "
            f"{payload['validated']} total, {payload['passed']} equivalence-passing"
        )
        return 0

    print(
        "GraphSymPerf is installed. Use: "
        "graph-sym-perf run-ldbc-mining | run-gdsmith-mining | cluster | doctor | experiment | triage | validate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
