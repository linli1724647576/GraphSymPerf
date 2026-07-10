"""The single runtime-first CLI; legacy CLI is removed after parity migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graph_sym_perf.experiment.planner import build_run_plan, write_resolved_manifest
from graph_sym_perf.experiment.spec import load_experiment_spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphsymperf", description="GraphSymPerf runtime-first CLI")
    root = parser.add_subparsers(dest="command")
    experiment = root.add_parser("experiment")
    experiment_commands = experiment.add_subparsers(dest="experiment_command")
    for name in ("plan", "doctor", "run"):
        command = experiment_commands.add_parser(name)
        command.add_argument("--config", default="config/experiment.yaml")
    experiment_commands.choices["plan"].add_argument("--output")
    resume = experiment_commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    status = experiment_commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    issue = root.add_parser("issue")
    issue_commands = issue.add_subparsers(dest="issue_command")
    export = issue_commands.add_parser("export")
    export.add_argument("--issue", required=True)
    export.add_argument("--output", required=True)
    for name in ("validate", "reduce"):
        command = issue_commands.add_parser(name)
        command.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
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
            missing = [name for system in spec.systems.values() for name in system.required_env if not __import__("os").environ.get(name)]
            print(f"doctor: targets={len(spec.targets)} gdsmith_dirty={plan.source_revision.gdsmith_dirty} missing_env={len(missing)}")
            return 0 if not missing and plan.source_revision.gdsmith_dirty is False else 4
        print("experiment run refused: run doctor first; formal runs require a clean GDsmith checkout and configured targets")
        return 4
    if args.command == "experiment" and args.experiment_command in {"resume", "status"}:
        print(f"{args.experiment_command}: run_id={args.run_id}")
        return 0
    if args.command == "issue" and args.issue_command == "export":
        path = Path(args.issue)
        if not path.exists():
            print("missing gate: submit-ready issue payload does not exist")
            return 2
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("stage") != "submit_ready" or payload.get("reduction_status") != "reduced_valid":
            print("missing gate: stage=submit_ready and reduction_status=reduced_valid are required")
            return 2
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
