"""Summarize recorded cases and manually checked bugs (standard library only).

Candidate timing differences are not bugs.  Add checked root-cause IDs to
bugs.csv; replay files and different database versions never create extra bugs.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import sys


BUG_FIELDS = ("case_id", "dbms", "bug_id", "status", "issue_url", "database_version")
BUG_STATUSES = {"needs_review", "reproduced", "confirmed", "fixed", "rejected"}
VALIDATED = {"reproduced", "confirmed", "fixed"}
STATUS_RANK = {"needs_review": 0, "rejected": 1, "reproduced": 2, "confirmed": 3, "fixed": 4}
VALID_CASE = {"no_candidate", "not_confirmed", "candidate"}
CANDIDATE = {"candidate", "timeout_candidate"}
SUMMARY_FIELDS = (
    "dbms", "pattern", "versions", "attempted", "cases", "valid", "candidate",
    "unique_bugs", "exclusive_bugs", "elapsed_seconds", "bugs_per_hour",
    "case_budget", "time_budget_seconds", "budget_coverage", "stop_reasons",
)


def _pattern_key(pattern):
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", pattern)
    return (match[1], int(match[2])) if match else (pattern, 0)


def _json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: cannot read JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _dbms(data, path):
    value = data.get("dbms")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    # Format 1 was produced solely by the Neo4j executor.  Require its actual
    # version string rather than assigning every unidentified record to Neo4j.
    version = data.get("database_version", "")
    if isinstance(version, str) and version.lower().startswith("neo4j/"):
        return "neo4j"
    raise ValueError(f"{path}: dbms is missing and cannot be inferred from database_version")


def load_records(root):
    records, runs, patterns_by_case = [], [], {}
    for path in sorted(root.rglob("result.json")):
        data = _json(path)
        case, result = data.get("case", {}), data.get("result", {})
        identity = data.get("input_hash", "")
        if not isinstance(case, dict) or not isinstance(result, dict):
            raise ValueError(f"{path}: missing case or result object")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError(f"{path}: input_hash must be a full SHA-256 digest")
        pattern, status = case.get("pattern"), result.get("status")
        if not isinstance(pattern, str) or not pattern or not isinstance(status, str) or not status:
            raise ValueError(f"{path}: case.pattern and result.status are required")
        db_data = {**data, **result}
        target, version = _dbms(db_data, path), result.get("database_version", "unknown")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{path}: database_version must be a nonempty string")
        key = (target, identity)
        if key in patterns_by_case and patterns_by_case[key] != pattern:
            raise ValueError(f"{path}: identical input_hash has conflicting patterns")
        patterns_by_case[key] = pattern
        records.append({"dbms": target, "case_id": identity,
                        "pattern": pattern, "version": version,
                        "status": status, "path": path})
    for path in sorted(root.rglob("run.json")):
        data = _json(path)
        data = {**data, "dbms": _dbms(data, path), "path": path}
        runs.append(data)
    return records, runs


def load_bugs(path, records):
    """Validate all annotations before applying any report filter."""
    identities = {(r["dbms"], r["case_id"], r["version"]) for r in records}
    bugs, bug_metadata = {}, {}
    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read bug annotations: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(BUG_FIELDS):
            raise ValueError(f"{path}: header must be {','.join(BUG_FIELDS)}")
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{number}: malformed CSV row")
            row = {key: value.strip() for key, value in row.items()}
            dbms, prefix = row["dbms"].lower(), row["case_id"].lower()
            if not re.fullmatch(r"[0-9a-f]{8,64}", prefix):
                raise ValueError(f"{path}:{number}: case_id requires 8–64 hexadecimal characters")
            matches = [key for key in identities if key[0] == dbms and key[1].startswith(prefix)
                       and key[2] == row["database_version"]]
            if len(matches) != 1:
                raise ValueError(f"{path}:{number}: case_id is {'ambiguous' if matches else 'not recorded'} "
                                 f"for {dbms}/{row['database_version']}")
            if not row["bug_id"] or row["status"] not in BUG_STATUSES:
                raise ValueError(f"{path}:{number}: bug_id or status is invalid")
            bug_key = (dbms, row["bug_id"])
            prior = bug_metadata.get(bug_key)
            if prior and (("rejected" in {prior["status"], row["status"]} and
                           bool({prior["status"], row["status"]} & VALIDATED)) or
                          (prior["issue_url"] and row["issue_url"] and prior["issue_url"] != row["issue_url"])):
                raise ValueError(f"{path}:{number}: conflicting status or issue_url for {dbms}/{row['bug_id']}")
            status = max((row["status"], (prior or {}).get("status", "needs_review")), key=STATUS_RANK.get)
            bug_metadata[bug_key] = {"status": status,
                                     "issue_url": row["issue_url"] or (prior or {}).get("issue_url", "")}
            if row["status"] in VALIDATED:
                bugs.setdefault(bug_key, set()).add(matches[0])
    return bugs, bug_metadata


def _number(value, path, field, *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (integer and type(value) is not int):
        raise ValueError(f"{path}: {field} must be a nonnegative {'integer' if integer else 'number'}")
    return value


def _owning_run(record, runs):
    matching = [run for run in runs if run["dbms"] == record["dbms"]
                and record["path"].is_relative_to(run["path"].parent)]
    return max(matching, key=lambda run: len(run["path"].parts), default=None)


def build_summary(records, runs, bugs, metadata, *, dbms=None, version=None):
    selected = [r for r in records if (dbms is None or r["dbms"] in dbms)
                and (version is None or r["version"] in version)]
    cases = {}
    selected_versions = {(r["dbms"], r["case_id"], r["version"]) for r in selected}
    groups = {}
    for record in selected:
        key = (record["dbms"], record["case_id"])
        if key in cases and cases[key]["pattern"] != record["pattern"]:
            raise ValueError(f"{record['path']}: identical input_hash has conflicting patterns")
        item = cases.setdefault(key, {"pattern": record["pattern"], "statuses": set(), "versions": set()})
        item["statuses"].add(record["status"])
        item["versions"].add(record["version"])
        group = groups.setdefault((record["dbms"], record["pattern"]), {"cases": set(), "runs": [], "records": []})
        group["cases"].add(key)
        group["records"].append(record)

    for run in runs:
        if run.get("kind", "discovery") == "replay":
            continue
        if dbms is not None and run["dbms"] not in dbms:
            continue
        if version is not None and run.get("database_version", "unknown") not in version:
            continue
        patterns, stats, budgets = run.get("patterns"), run.get("per_pattern", {}), run.get("budgets", {})
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{run['path']}: run.patterns must be a list")
        if not isinstance(stats, dict) or not isinstance(budgets, dict):
            raise ValueError(f"{run['path']}: per_pattern and budgets must be objects")
        for pattern in dict.fromkeys(patterns):
            groups.setdefault((run["dbms"], pattern), {"cases": set(), "runs": [], "records": []})["runs"].append(run)

    bug_patterns = {}
    for bug_key, annotated in bugs.items():
        if metadata[bug_key]["status"] not in VALIDATED:
            continue
        patterns = {cases[key[:2]]["pattern"] for key in annotated if key in selected_versions}
        if patterns:
            bug_patterns[bug_key] = patterns

    summary = []
    for (target, pattern), group in sorted(groups.items(), key=lambda item: (item[0][0], _pattern_key(item[0][1]))):
        entries = [cases[key] for key in group["cases"]]
        versions = {v for entry in entries for v in entry["versions"]}
        # Only discovery runs provide the detection-time denominator.  A replay
        # of an already discovered input/version is harmless; a new replay-only
        # input/version must not borrow another run's discovery time.
        discovery_inputs = set()
        for record in group["records"]:
            owner = _owning_run(record, runs)
            if owner in group["runs"] and record["version"] == owner.get("database_version", "unknown"):
                discovery_inputs.add((record["dbms"], record["case_id"], record["version"]))
        group_inputs = {(r["dbms"], r["case_id"], r["version"]) for r in group["records"]}
        covered = bool(group["runs"]) and group_inputs <= discovery_inputs
        incomplete = any(r["status"] in {"started", "pending"} for r in group["records"])
        attempted = elapsed = case_budget = time_budget = 0
        reasons = set()
        for run in group["runs"]:
            stat = run.get("per_pattern", {}).get(pattern)
            budget = run.get("budgets", {}).get(pattern)
            if not isinstance(stat, dict) or not isinstance(budget, dict):
                covered = False
                continue
            attempted += _number(stat.get("attempted"), run["path"], "attempted", integer=True)
            elapsed += _number(stat.get("elapsed_seconds"), run["path"], "elapsed_seconds")
            case_budget += _number(budget.get("max_cases"), run["path"], "max_cases", integer=True)
            time_budget += _number(budget.get("max_seconds"), run["path"], "max_seconds")
            versions.add(run.get("database_version", "unknown"))
            if stat.get("stop_reason"):
                reasons.add(str(stat["stop_reason"]))
            if stat.get("stop_reason") in {"running", "pending", "", None}:
                incomplete = True
        if incomplete:
            covered = False
        matching_bugs = [key for key, patterns in bug_patterns.items() if key[0] == target and pattern in patterns]
        row = {"dbms": target, "pattern": pattern, "versions": ";".join(sorted(versions)),
               "attempted": attempted if covered else len(entries), "cases": len(entries),
               "valid": sum(bool(e["statuses"] & VALID_CASE) for e in entries),
               "candidate": sum(bool(e["statuses"] & CANDIDATE) for e in entries),
               "unique_bugs": len(matching_bugs),
               "exclusive_bugs": sum(len(bug_patterns[key]) == 1 for key in matching_bugs),
               "elapsed_seconds": round(elapsed, 6) if covered else "",
               "bugs_per_hour": round(len(matching_bugs) * 3600 / elapsed, 6) if covered and elapsed > 0 else "",
               "case_budget": case_budget if covered else "", "time_budget_seconds": time_budget if covered else "",
               "budget_coverage": "incomplete_run" if incomplete else "recorded" if covered else "missing_manifest",
               "stop_reasons": ";".join(sorted(reasons))}
        summary.append(row)
    patterns = sorted({pattern for _, pattern in groups}, key=_pattern_key)
    matrix = [{"dbms": key[0], "bug_id": key[1], **metadata[key],
               **{pattern: int(pattern in values) for pattern in patterns}}
              for key, values in sorted(bug_patterns.items())]
    return summary, matrix, patterns


def _write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(results, bugs_csv, output, *, dbms=None, version=None):
    if not results.is_dir():
        raise ValueError(f"results directory does not exist: {results}")
    records, runs = load_records(results)
    bugs, metadata = load_bugs(bugs_csv, records)
    summary, matrix, patterns = build_summary(records, runs, bugs, metadata, dbms=dbms, version=version)
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "summary.csv", SUMMARY_FIELDS, summary)
    _write_csv(output / "pattern_bug.csv", ("dbms", "bug_id", "status", "issue_url", *patterns), matrix)
    return summary, matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--bugs", type=Path, default=Path("bugs.csv"))
    parser.add_argument("--output", type=Path, default=Path("results/summary"))
    parser.add_argument("--dbms", action="append", help="DBMS name, repeatable")
    parser.add_argument("--version", action="append", help="Exact database_version, repeatable")
    args = parser.parse_args(argv)
    try:
        rows, matrix = summarize(args.results, args.bugs, args.output,
                                 dbms={s.lower() for s in args.dbms} if args.dbms else None,
                                 version=set(args.version) if args.version else None)
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"{len(rows)} DBMS/pattern rows; {len(matrix)} manually validated bugs")
    print(args.output / "summary.csv")
    print(args.output / "pattern_bug.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
