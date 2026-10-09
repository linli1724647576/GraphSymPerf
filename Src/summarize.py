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
from urllib.parse import urlsplit


BUG_FIELDS = ("case_id", "dbms", "bug_id", "status", "issue_url", "database_version")
OPTIONAL_BUG_FIELDS = ("bug_type", "local_status", "status_source", "last_updated", "local_report")
LOCAL_BUG_STATUSES = {"needs_review", "reproduced", "confirmed", "fixed", "rejected"}
BUG_STATUSES = LOCAL_BUG_STATUSES | {"pending", "uninterested"}
VALIDATED = {"reproduced", "confirmed", "fixed"}
STATUS_RANK = {"needs_review": 0, "pending": 0, "uninterested": 0,
               "rejected": 1, "reproduced": 2, "confirmed": 3, "fixed": 4}
VALID_CASE = {"no_candidate", "not_confirmed", "candidate", "timing_observation"}
CANDIDATE = {"candidate", "timeout_candidate"}
SUMMARY_FIELDS = (
    "dbms", "language", "pattern", "versions", "attempted", "cases", "valid", "candidate", "timing_observations",
    "symmetry_candidates", "reference_candidates", "unclassified_candidates",
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


def _language(data, path):
    # Historical cases and run manifests were all generated as Cypher.
    value = data.get("language", "cypher")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: language must be a nonempty string")
    return value.strip().lower()


def _comparison_candidates(result, path):
    """Use independent pair statuses, never the top-level winning summary."""
    families = set()
    comparisons = result.get("comparisons", {})
    if not isinstance(comparisons, dict):
        raise ValueError(f"{path}: result.comparisons must be an object")
    references = result.get("reference_comparisons", {})
    if not isinstance(references, dict):
        raise ValueError(f"{path}: result.reference_comparisons must be an object")
    # Some records put only AB in comparisons and retain the reference pairs
    # in reference_comparisons.  New compact entries override the same pair's
    # older record, without suppressing other independently measured pairs.
    pairs = {name: ("reference", pair) for name, pair in references.items()}
    pairs.update({name: (None, pair) for name, pair in comparisons.items()})
    for default_kind, pair in pairs.values():
        if not isinstance(pair, dict):
            raise ValueError(f"{path}: each comparison must be an object")
        kind = pair.get("pair_kind", default_kind)
        if kind in {"symmetry", "reference"} and pair.get("status") in CANDIDATE:
            families.add(kind)
    ab = comparisons.get("ab", {})
    ab_recorded = ab.get("pair_kind") in {"symmetry", "reference"} and isinstance(ab.get("status"), str) and bool(ab["status"])
    return families, result.get("status") in CANDIDATE and not ab_recorded


def load_records(root):
    records, runs, patterns_by_case = [], [], {}
    excluded_scopes, excluded_cache = set(), {}
    # Exclusion is explicit and scoped to this results root. Validate markers
    # before reading results, including markers inside an excluded subtree.
    for marker in sorted(root.rglob("EXCLUDED.json")):
        exclusion = _json(marker)
        if type(exclusion.get("include_in_analysis")) is not bool:
            raise ValueError(f"{marker}: include_in_analysis must be a boolean")
        if exclusion["include_in_analysis"] is False:
            excluded_scopes.add(marker.parent)

    def excluded(directory):
        if directory not in excluded_cache:
            inherited = False if directory == root else excluded(directory.parent)
            excluded_cache[directory] = directory in excluded_scopes or inherited
        return excluded_cache[directory]

    for path in sorted(root.rglob("result.json")):
        if excluded(path.parent):
            continue
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
        language = _language({**data, **case, **result}, path)
        declared = {_language(source, path) for source in (data, case, result) if "language" in source}
        if len(declared) > 1:
            raise ValueError(f"{path}: conflicting case and result languages")
        key = (target, identity)
        if key in patterns_by_case and patterns_by_case[key][0] != pattern:
            raise ValueError(f"{path}: identical input_hash has conflicting patterns")
        if key in patterns_by_case and patterns_by_case[key][1] != language:
            raise ValueError(f"{path}: identical input_hash has conflicting languages")
        patterns_by_case[key] = (pattern, language)
        families, unclassified = _comparison_candidates(result, path)
        records.append({"dbms": target, "case_id": identity,
                        "pattern": pattern, "language": language, "version": version,
                        "status": status, "path": path,
                        "candidate_families": families, "unclassified_candidate": unclassified})
    for path in sorted(root.rglob("run.json")):
        if excluded(path.parent):
            continue
        data = _json(path)
        data = {**data, "dbms": _dbms(data, path), "language": _language(data, path), "path": path}
        runs.append(data)
    return records, runs


def load_bugs(path, records):
    """Read legacy annotations or an extended, public-issue registry.

    An issue without a recorded case remains in the registry, but receives no
    pattern attribution. Public status and local validation are independent.
    """
    identities = {(r["dbms"], r["case_id"], r["version"]) for r in records}
    bugs, bug_metadata = {}, {}
    local_statuses, bug_types = {}, {}
    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read bug annotations: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if (fields[:len(BUG_FIELDS)] != list(BUG_FIELDS) or len(fields) != len(set(fields))
                or set(fields[len(BUG_FIELDS):]) - set(OPTIONAL_BUG_FIELDS)):
            raise ValueError(f"{path}: header must begin with {','.join(BUG_FIELDS)}; "
                             f"optional fields: {','.join(OPTIONAL_BUG_FIELDS)}")
        extended = len(fields) > len(BUG_FIELDS)
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{number}: malformed CSV row")
            row = {key: value.strip() for key, value in row.items()}
            dbms, prefix = row["dbms"].lower(), row["case_id"].lower()
            if not dbms:
                raise ValueError(f"{path}:{number}: dbms is required")
            if not row["bug_id"] or row["status"] not in BUG_STATUSES:
                raise ValueError(f"{path}:{number}: bug_id or status is invalid")
            local_status = row.get("local_status", row["status"])
            if "local_status" in row and local_status not in LOCAL_BUG_STATUSES | {""}:
                raise ValueError(f"{path}:{number}: local_status is invalid")
            type_names = {"": "", "performance": "performance", "performance bug": "performance",
                          "logical": "logical", "logical bug": "logical"}
            bug_type = type_names.get(row.get("bug_type", "").lower())
            if bug_type is None:
                raise ValueError(f"{path}:{number}: bug_type is invalid")
            matches = []
            if not prefix and extended:
                try:
                    url = urlsplit(row["issue_url"])
                    valid_url = (url.scheme in {"http", "https"} and bool(url.hostname)
                                 and bool(url.path.strip("/")) and url.username is None
                                 and not any(c.isspace() for c in row["issue_url"]))
                except ValueError:
                    valid_url = False
                if not valid_url:
                    raise ValueError(f"{path}:{number}: an unlinked issue requires a valid issue_url")
            elif not re.fullmatch(r"[0-9a-f]{8,64}", prefix):
                raise ValueError(f"{path}:{number}: case_id requires 8–64 hexadecimal characters")
            else:
                matches = [key for key in identities if key[0] == dbms and key[1].startswith(prefix)
                           and key[2] == row["database_version"]]
                if len(matches) != 1:
                    raise ValueError(f"{path}:{number}: case_id is {'ambiguous' if matches else 'not recorded'} "
                                     f"for {dbms}/{row['database_version']}")
            bug_key = (dbms, row["bug_id"])
            prior = bug_metadata.get(bug_key)
            if "local_status" in row and prior and prior["status"] != row["status"]:
                raise ValueError(f"{path}:{number}: conflicting public status for {dbms}/{row['bug_id']}")
            checked = local_statuses.setdefault(bug_key, set())
            checked.add(local_status)
            if (("rejected" in checked and bool(checked & VALIDATED)) or
                    (prior and prior["issue_url"] and row["issue_url"] and prior["issue_url"] != row["issue_url"])):
                raise ValueError(f"{path}:{number}: conflicting status or issue_url for {dbms}/{row['bug_id']}")
            if bug_type:
                if bug_key in bug_types and bug_types[bug_key] != bug_type:
                    raise ValueError(f"{path}:{number}: conflicting bug_type for {dbms}/{row['bug_id']}")
                bug_types[bug_key] = bug_type
            # A separate local_status makes status the public disposition.
            # Preserve legacy rank merging only for legacy case annotations.
            status = row["status"] if "local_status" in row else max(
                (row["status"], (prior or {}).get("status", "needs_review")), key=STATUS_RANK.get)
            bug_metadata[bug_key] = {"status": status,
                                     "issue_url": row["issue_url"] or (prior or {}).get("issue_url", "")}
            if matches and local_status in VALIDATED:
                bugs.setdefault(bug_key, set()).add(matches[0])
    # Explicitly reported logical bugs are maintained, not counted as missed
    # optimizations, even if another annotation of the same bug omits its type.
    bugs = {key: cases for key, cases in bugs.items() if bug_types.get(key) != "logical"}
    return bugs, bug_metadata


def _number(value, path, field, *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (integer and type(value) is not int):
        raise ValueError(f"{path}: {field} must be a nonnegative {'integer' if integer else 'number'}")
    return value


def _owning_run(record, runs):
    matching = [run for run in runs if run["dbms"] == record["dbms"]
                and record["path"].is_relative_to(run["path"].parent)]
    return max(matching, key=lambda run: len(run["path"].parts), default=None)


def build_summary(records, runs, bugs, metadata, *, dbms=None, version=None, language=None):
    selected = [r for r in records if (dbms is None or r["dbms"] in dbms)
                and (version is None or r["version"] in version)
                and (language is None or r["language"] in language)]
    cases = {}
    selected_versions = {(r["dbms"], r["case_id"], r["version"]) for r in selected}
    groups = {}
    for record in selected:
        key = (record["dbms"], record["case_id"])
        if key in cases and cases[key]["pattern"] != record["pattern"]:
            raise ValueError(f"{record['path']}: identical input_hash has conflicting patterns")
        if key in cases and cases[key]["language"] != record["language"]:
            raise ValueError(f"{record['path']}: identical input_hash has conflicting languages")
        item = cases.setdefault(key, {"pattern": record["pattern"], "language": record["language"], "statuses": set(), "versions": set(),
                                      "candidate_families": set(), "unclassified_candidate": False})
        item["statuses"].add(record["status"])
        item["versions"].add(record["version"])
        item["candidate_families"].update(record["candidate_families"])
        item["unclassified_candidate"] |= record["unclassified_candidate"]
        group = groups.setdefault((record["dbms"], record["language"], record["pattern"]), {"cases": set(), "runs": [], "records": []})
        group["cases"].add(key)
        group["records"].append(record)

    for run in runs:
        if run.get("kind", "discovery") == "replay":
            continue
        if dbms is not None and run["dbms"] not in dbms:
            continue
        if version is not None and run.get("database_version", "unknown") not in version:
            continue
        if language is not None and run["language"] not in language:
            continue
        patterns, stats, budgets = run.get("patterns"), run.get("per_pattern", {}), run.get("budgets", {})
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{run['path']}: run.patterns must be a list")
        if not isinstance(stats, dict) or not isinstance(budgets, dict):
            raise ValueError(f"{run['path']}: per_pattern and budgets must be objects")
        for pattern in dict.fromkeys(patterns):
            groups.setdefault((run["dbms"], run["language"], pattern), {"cases": set(), "runs": [], "records": []})["runs"].append(run)

    bug_patterns, bug_groups = {}, {}
    for bug_key, annotated in bugs.items():
        observed = {(cases[key[:2]]["language"], cases[key[:2]]["pattern"])
                    for key in annotated if key in selected_versions}
        if observed:
            bug_groups[bug_key] = observed
            bug_patterns[bug_key] = {pattern for _, pattern in observed}

    summary = []
    for (target, source_language, pattern), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1], _pattern_key(item[0][2]))):
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
        matching_bugs = [key for key, observed in bug_groups.items()
                         if key[0] == target and (source_language, pattern) in observed]
        row = {"dbms": target, "language": source_language, "pattern": pattern, "versions": ";".join(sorted(versions)),
               "attempted": attempted if covered else len(entries), "cases": len(entries),
               "valid": sum(bool(e["statuses"] & VALID_CASE) for e in entries),
               "candidate": sum(bool(e["statuses"] & CANDIDATE) for e in entries),
               "timing_observations": sum("timing_observation" in e["statuses"] for e in entries),
               "symmetry_candidates": sum("symmetry" in e["candidate_families"] for e in entries),
               "reference_candidates": sum("reference" in e["candidate_families"] for e in entries),
               "unclassified_candidates": sum(e["unclassified_candidate"] for e in entries),
               "unique_bugs": len(matching_bugs),
               # Exclusivity concerns patterns within this language among the
               # selected records, not uniqueness across query languages.
               "exclusive_bugs": sum(len({p for lang, p in bug_groups[key] if lang == source_language}) == 1
                                     for key in matching_bugs),
               "elapsed_seconds": round(elapsed, 6) if covered else "",
               "bugs_per_hour": round(len(matching_bugs) * 3600 / elapsed, 6) if covered and elapsed > 0 else "",
               "case_budget": case_budget if covered else "", "time_budget_seconds": time_budget if covered else "",
               "budget_coverage": "incomplete_run" if incomplete else "recorded" if covered else "missing_manifest",
               "stop_reasons": ";".join(sorted(reasons))}
        summary.append(row)
    patterns = sorted({pattern for _, _, pattern in groups}, key=_pattern_key)
    matrix = [{"dbms": key[0], "bug_id": key[1], **metadata[key],
               **{pattern: int(pattern in values) for pattern in patterns}}
              for key, values in sorted(bug_patterns.items())]
    return summary, matrix, patterns


def _write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(results, bugs_csv, output, *, dbms=None, version=None, language=None):
    if not results.is_dir():
        raise ValueError(f"results directory does not exist: {results}")
    records, runs = load_records(results)
    bugs, metadata = load_bugs(bugs_csv, records)
    summary, matrix, patterns = build_summary(records, runs, bugs, metadata, dbms=dbms, version=version, language=language)
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
    parser.add_argument("--language", action="append", help="Query language, repeatable; legacy records are Cypher")
    parser.add_argument("--version", action="append", help="Exact database_version, repeatable")
    args = parser.parse_args(argv)
    try:
        rows, matrix = summarize(args.results, args.bugs, args.output,
                                 dbms={s.lower() for s in args.dbms} if args.dbms else None,
                                 version=set(args.version) if args.version else None,
                                 language={s.lower() for s in args.language} if args.language else None)
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"{len(rows)} DBMS/language/pattern rows; {len(matrix)} manually validated bugs")
    print(args.output / "summary.csv")
    print(args.output / "pattern_bug.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
