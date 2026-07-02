"""Command-line entrypoint."""

from __future__ import annotations

import argparse

from graph_sym_perf.app import EXIT_CONFIG_ERROR, run_gdsmith_mining, run_ldbc_mining
from graph_sym_perf.cluster import write_clustered_summary
from graph_sym_perf.core import ConfigError
from graph_sym_perf.experiment import (
    aggregate_run_root,
    export_run_report,
    load_known_replay_cases,
    run_doctor,
    run_experiment,
    run_known_replay,
    run_triage,
    validate_triage,
)


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
    mine.add_argument("--generator-target", default="neo4j")
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
    doctor.add_argument("--dry-run", action="store_true")

    experiment = subparsers.add_parser("experiment", help="Run or aggregate sharded experiments.")
    experiment_subparsers = experiment.add_subparsers(dest="experiment_command")
    experiment_run = experiment_subparsers.add_parser("run", help="Run one experiment shard from a manifest.")
    experiment_run.add_argument("--manifest", required=True)
    experiment_run.add_argument("--shard", type=int, default=None)
    experiment_run.add_argument("--num-shards", type=int, default=None)
    experiment_run.add_argument("--systems", default=None)
    experiment_run.add_argument("--max-cases", type=int, default=None)
    experiment_feedback = experiment_run.add_mutually_exclusive_group()
    experiment_feedback.add_argument("--feedback", dest="feedback_enabled", action="store_true")
    experiment_feedback.add_argument("--no-feedback", dest="feedback_enabled", action="store_false")
    experiment_run.set_defaults(feedback_enabled=None)
    experiment_resume = experiment_run.add_mutually_exclusive_group()
    experiment_resume.add_argument("--resume", dest="resume_enabled", action="store_true")
    experiment_resume.add_argument("--no-resume", dest="resume_enabled", action="store_false")
    experiment_run.set_defaults(resume_enabled=None)
    experiment_aggregate = experiment_subparsers.add_parser("aggregate", help="Aggregate shard outputs.")
    experiment_aggregate.add_argument("--run-root", required=True)

    triage = subparsers.add_parser("triage", help="Rank top unique issues from an aggregated run root.")
    triage.add_argument("--run-dir", required=True)
    triage.add_argument("--top", type=int, default=100)

    validate = subparsers.add_parser("validate", help="Replay triaged representatives.")
    validate.add_argument("--triage", required=True)
    validate.add_argument("--runtime-repeats", type=int, default=15)
    validate.add_argument("--reduce", action="store_true")
    validate.add_argument("--issue-search", action="store_true")

    replay = subparsers.add_parser("replay", help="Replay known issue cases from a manifest.")
    replay.add_argument("--config", required=True)
    replay.add_argument("--all-known", action="store_true")
    replay.add_argument("--case", dest="case_ids", action="append", default=[])
    replay.add_argument("--dry-run", action="store_true")

    report = subparsers.add_parser("report", help="Export a reader-facing report from an aggregated run dir.")
    report.add_argument("--run-dir", required=True)
    report.add_argument("--ablation-runs", default="")
    report.add_argument("--oracle-run", default=None)
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
                generator_target=args.generator_target,
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
            return run_doctor(args.config, strict=args.strict, dry_run=args.dry_run)
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_CONFIG_ERROR
    if args.command == "experiment":
        if args.experiment_command == "run":
            try:
                return run_experiment(
                    args.manifest,
                    shard=args.shard,
                    num_shards=args.num_shards,
                    systems=args.systems,
                    max_cases=args.max_cases,
                    feedback_enabled=args.feedback_enabled,
                    resume=args.resume_enabled,
                )
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
        payload = validate_triage(
            args.triage,
            runtime_repeats=args.runtime_repeats,
            reduce=args.reduce,
            issue_search=args.issue_search,
        )
        print(
            "validated representatives: "
            f"{payload['validated']} total, {payload['passed']} equivalence-passing"
        )
        return 0
    if args.command == "replay":
        try:
            case_ids = tuple(args.case_ids or ())
            if not args.all_known and not case_ids:
                raise ConfigError("replay requires --all-known or at least one --case")
            exit_code, payload = run_known_replay(args.config, case_ids=case_ids, dry_run=args.dry_run)
            if args.dry_run:
                print(f"replay dry-run enumerated {payload['case_count']} cases")
            else:
                print(f"replay executed {payload['case_count']} cases")
            return exit_code
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_CONFIG_ERROR
    if args.command == "report":
        ablation_runs = tuple(item.strip() for item in str(args.ablation_runs or "").split(",") if item.strip())
        payload = export_run_report(args.run_dir, ablation_runs=ablation_runs, oracle_run=args.oracle_run)
        print(f"exported report: {payload['run_dir']}/report.md")
        return 0

    print(
        "GraphSymPerf is installed. Use: "
        "graph-sym-perf run-ldbc-mining | run-gdsmith-mining | cluster | doctor | experiment | triage | validate | replay | report"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
