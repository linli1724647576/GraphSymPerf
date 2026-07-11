"""The one installed GraphSymPerf command-line entry point."""

from __future__ import annotations

import argparse

from graph_sym_perf.experiment import ExperimentCommands, ExperimentNotReady
from graph_sym_perf.issues import IssueCommands, IssueGateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphsymperf", description="GraphSymPerf runtime-first CLI")
    root = parser.add_subparsers(dest="command")

    experiment = root.add_parser("experiment", help="run or resume the formal experiment")
    experiment_commands = experiment.add_subparsers(dest="experiment_command")
    run = experiment_commands.add_parser("run", help="run the complete formal pipeline")
    run.add_argument("--config", default="config/experiment.yaml")
    run.add_argument("--output-root", default="runs")
    resume = experiment_commands.add_parser("resume", help="resume the same immutable run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--config", default="config/experiment.yaml")
    resume.add_argument("--output-root", default="runs")
    for name, help_text in (
        ("doctor", "advanced: verify all formal-run dependencies"),
        ("plan", "advanced: resolve the immutable experiment matrix"),
    ):
        command = experiment_commands.add_parser(name, help=help_text)
        command.add_argument("--config", default="config/experiment.yaml")
    experiment_commands.choices["doctor"].add_argument("--output-root")
    experiment_commands.choices["plan"].add_argument("--output")
    status = experiment_commands.add_parser("status", help="advanced: verify durable shard state")
    status.add_argument("--run-id", required=True)
    status.add_argument("--output-root", default="runs")
    migrate = experiment_commands.add_parser("migrate-config", help="advanced: one-way schema-v2 migration")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--output", required=True)
    migrate.add_argument("--template", default="config/experiment.yaml")

    issue = root.add_parser("issue", help="advanced validated-issue lifecycle")
    issue_commands = issue.add_subparsers(dest="issue_command")
    export = issue_commands.add_parser("export", help="advanced: export only a SubmitReadyIssue")
    export.add_argument("--issue", required=True)
    export.add_argument("--output", required=True)
    for name in ("validate", "reduce"):
        command = issue_commands.add_parser(name, help=f"advanced: {name} a sealed runtime candidate")
        command.add_argument("--candidate", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    experiments = ExperimentCommands()
    issues = IssueCommands()
    try:
        if args.command == "experiment" and args.experiment_command == "run":
            result = experiments.run(config=args.config, output_root=args.output_root)
            print(
                "runtime-first run: "
                f"run_id={result.run_id} completed={len(result.summary.completed)} "
                f"incomplete={len(result.summary.incomplete)} failed={len(result.summary.failed)}"
            )
            return 0 if not result.summary.incomplete and not result.summary.failed else 5
        if args.command == "experiment" and args.experiment_command == "resume":
            result = experiments.resume(config=args.config, output_root=args.output_root, run_id=args.run_id)
            print(
                "runtime-first resume: "
                f"run_id={result.run_id} completed={len(result.summary.completed)} "
                f"incomplete={len(result.summary.incomplete)} failed={len(result.summary.failed)}"
            )
            return 0 if not result.summary.incomplete and not result.summary.failed else 5
        if args.command == "experiment" and args.experiment_command == "plan":
            plan = experiments.plan(config=args.config, output=args.output)
            print(
                f"runtime-first plan: systems=7 shards={len(plan.tasks)} "
                f"accepted_pair_budget={plan.total_accepted_pair_budget}"
            )
            return 0
        if args.command == "experiment" and args.experiment_command == "doctor":
            report = experiments.doctor(config=args.config, output_root=args.output_root)
            print(f"doctor: targets={len(report.systems)} gdsmith_dirty={report.gdsmith_dirty} ready={report.ready}")
            return 0 if report.ready else 4
        if args.command == "experiment" and args.experiment_command == "status":
            report = experiments.status(output_root=args.output_root, run_id=args.run_id)
            print(
                f"runtime-first status: run_id={report.run_id} total={report.total} "
                f"completed={report.completed} incomplete={report.incomplete} failed={report.failed} "
                f"active={report.active} invalid={report.invalid}"
            )
            return 0 if report.invalid == 0 else 5
        if args.command == "experiment" and args.experiment_command == "migrate-config":
            destination = experiments.migrate_config(
                source=args.input,
                destination=args.output,
                template=args.template,
            )
            print(f"migrated schema-v2 experiment: {destination}")
            return 0
        if args.command == "issue" and args.issue_command == "validate":
            outcome = issues.validate(candidate=args.candidate)
            print(
                f"issue validation: stage={outcome['stage']} "
                f"candidate={outcome.get('candidate_hash', 'unknown')}"
            )
            return 0
        if args.command == "issue" and args.issue_command == "reduce":
            outcome = issues.reduce(candidate=args.candidate)
            print(
                f"issue reduction: status={outcome['status']} "
                f"candidate={outcome.get('candidate_hash', 'unknown')}"
            )
            return 0
        if args.command == "issue" and args.issue_command == "export":
            destination = issues.export(issue=args.issue, output=args.output)
            print(f"exported SubmitReadyIssue: {destination}")
            return 0
    except ExperimentNotReady as exc:
        print(f"experiment run refused: {exc}")
        return 4
    except IssueGateError as exc:
        print("missing gate: " + "; ".join(exc.missing_gates))
        return 2
    except Exception as exc:
        print(f"command error: {exc}")
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
