"""The single runtime-first CLI; legacy CLI is removed after parity migration."""

from __future__ import annotations

import argparse
from pathlib import Path

from graph_sym_perf.experiment import (
    ExperimentLifecycle,
    RuntimeFirstShardWorker,
    build_run_plan,
    load_experiment_spec,
    migrate_legacy_config_to_v2,
    run_doctor,
    write_resolved_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphsymperf", description="GraphSymPerf runtime-first CLI")
    root = parser.add_subparsers(dest="command")
    experiment = root.add_parser("experiment")
    experiment_commands = experiment.add_subparsers(dest="experiment_command")
    for name in ("plan", "doctor", "run"):
        command = experiment_commands.add_parser(name)
        command.add_argument("--config", default="config/experiment.yaml")
    experiment_commands.choices["run"].add_argument("--output-root", default="runs")
    experiment_commands.choices["plan"].add_argument("--output")
    resume = experiment_commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    status = experiment_commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    migrate = experiment_commands.add_parser("migrate-config")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--output", required=True)
    migrate.add_argument("--template", default="config/experiment.yaml")
    issue = root.add_parser("issue")
    issue_commands = issue.add_subparsers(dest="issue_command")
    export = issue_commands.add_parser("export")
    export.add_argument("--issue", required=True)
    export.add_argument("--output", required=True)
    for name in ("validate", "reduce"):
        command = issue_commands.add_parser(name)
        command.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    if args.command == "experiment" and args.experiment_command == "migrate-config":
        try:
            destination = migrate_legacy_config_to_v2(args.input, args.output, template_path=args.template)
        except Exception as exc:
            print(f"migration error: {exc}")
            return 2
        print(f"migrated schema-v2 experiment: {destination}")
        return 0
    if args.command == "experiment" and args.experiment_command in {"plan", "doctor", "run"}:
        try:
            spec = load_experiment_spec(args.config)
            plan = build_run_plan(spec)
        except Exception as exc:
            print(f"configuration error: {exc}")
            return 2
        if args.experiment_command == "plan":
            if args.output:
                write_resolved_manifest(plan, args.output)
            print(f"runtime-first plan: systems=7 shards={len(plan.tasks)} accepted_pair_budget={plan.total_accepted_pair_budget}")
            return 0
        if args.experiment_command == "doctor":
            report = run_doctor(args.config)
            print(f"doctor: targets={len(report.systems)} gdsmith_dirty={report.gdsmith_dirty} ready={report.ready}")
            return 0 if report.ready else 4
        report = run_doctor(args.config, output_root=args.output_root)
        if not report.ready:
            print("experiment run refused: doctor is not ready; inspect the generated doctor report")
            return 4
        worker = RuntimeFirstShardWorker(spec)
        result = ExperimentLifecycle(args.output_root).run(spec, worker)
        print(
            "runtime-first run: "
            f"run_id={result.run_id} completed={len(result.summary.completed)} "
            f"incomplete={len(result.summary.incomplete)} failed={len(result.summary.failed)}"
        )
        return 0 if not result.summary.incomplete and not result.summary.failed else 5
    if args.command == "experiment" and args.experiment_command in {"resume", "status"}:
        print(f"{args.experiment_command}: run_id={args.run_id}")
        return 0
    if args.command == "issue" and args.issue_command == "export":
        path = Path(args.issue)
        if not path.exists():
            print("missing gate: submit-ready issue payload does not exist")
            return 2
        print("missing gate: raw JSON cannot prove a typed SubmitReadyIssue; use the typed issue exporter")
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
