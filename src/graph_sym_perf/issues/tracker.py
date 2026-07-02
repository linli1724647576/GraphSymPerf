"""Submission-gate issue de-duplication and status artifact writing."""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

from graph_sym_perf.core import is_profile_supported_level, is_runtime_supported_level, support_level_value, to_jsonable

SYSTEM_ISSUE_REPOSITORIES: Mapping[str, str] = {
    "neo4j": "neo4j/neo4j",
    "memgraph": "memgraph/memgraph",
    "falkordb": "FalkorDB/FalkorDB",
    "arcadedb": "ArcadeData/arcadedb",
    "apache_age": "apache/age",
    "kuzu": "kuzudb/kuzu",
    "agensgraph": "bitnine-oss/agensgraph",
}


def build_issue_tracking_artifacts(
    run_dir: str | Path,
    triage_payload: Mapping[str, Any],
    validation_summary: Mapping[str, Any],
    *,
    issue_search: bool = False,
    searcher=None,
) -> Mapping[str, Any]:
    """Write reportable issue and status-tracking files for a validated triage run."""

    root = Path(run_dir)
    validation_by_case = {
        str(item.get("case_id")): item
        for item in validation_summary.get("results") or ()
        if item.get("case_id") is not None
    }
    reportable: list[Mapping[str, Any]] = []
    status_rows: list[Mapping[str, Any]] = []
    checked_at = _utc_now()
    for triage_item in triage_payload.get("items") or ():
        case_id = str(triage_item.get("representative_case_id") or triage_item.get("case_id") or "")
        validation = validation_by_case.get(case_id, {})
        if not _is_reportable(validation):
            continue
        search_payload = _search_for_existing_issues(
            triage_item,
            validation,
            enabled=issue_search,
            searcher=searcher,
        )
        known_issue_url = _known_issue_url(triage_item, validation)
        status = "reported" if known_issue_url else _external_issue_status(search_payload)
        reducer = validation.get("reducer") or {}
        entry = {
            "case_id": case_id,
            "system": triage_item.get("system") or validation.get("system"),
            "cluster_key": triage_item.get("cluster_key"),
            "issue_family_key": triage_item.get("issue_family_key"),
            "unique_issue_key": triage_item.get("unique_issue_key"),
            "status": status,
            "developer_status": _developer_status(triage_item, validation),
            "support_level": _support_value(validation.get("support_level")),
            "equivalence_passed": bool(validation.get("equivalence_passed")),
            "runtime_supported": is_runtime_supported_level(validation.get("support_level")),
            "root_cause_labels": tuple(triage_item.get("root_cause_labels") or ()),
            "transform_rules": triage_item.get("transform_rules") or {},
            "latency_ratio": validation.get("latency_ratio") or triage_item.get("latency_ratio"),
            "db_hits_ratio": validation.get("db_hits_ratio") or triage_item.get("db_hits_ratio"),
            "rows_ratio": validation.get("rows_ratio") or triage_item.get("rows_ratio"),
            "issue_url": known_issue_url,
            "issue_draft_path": validation.get("issue_draft_path"),
            "reducer_artifact": reducer.get("artifact_dir"),
            "reduced_fast_query": (reducer.get("query_reduction") or {}).get("fast_query_path"),
            "reduced_slow_query": (reducer.get("query_reduction") or {}).get("slow_query_path"),
            "reduced_graph": (reducer.get("graph_reduction") or {}).get("graph_path"),
            "existing_issue_status": status,
            "duplicate_of": _first_existing_issue_url(search_payload),
            "existing_issue_search": search_payload,
            "last_checked_at": checked_at,
        }
        reportable.append(entry)
        status_rows.append(_status_row(entry))

    existing_reported = _load_reported_issues(root / "reported_issues.yaml")
    merged_reported = _merge_reported_issues(existing_reported, reportable, checked_at=checked_at)
    _write_json(root / "reportable_issues.json", {"generated_at": checked_at, "issue_search": issue_search, "issues": reportable})
    _write_yaml(root / "reported_issues.yaml", {"issues": merged_reported})
    _write_csv(root / "reported_issues.csv", merged_reported)
    _write_csv(root / "issue_status_table.csv", status_rows)
    _write_feedback_summary(root / "developer_feedback_summary.md", merged_reported)
    return {
        "reportable_count": len(reportable),
        "reported_issue_count": len(merged_reported),
        "issue_search_enabled": issue_search,
        "artifacts": {
            "reportable_issues": "reportable_issues.json",
            "reported_issues": "reported_issues.yaml",
            "reported_issues_csv": "reported_issues.csv",
            "issue_status_table": "issue_status_table.csv",
            "developer_feedback_summary": "developer_feedback_summary.md",
        },
    }


def search_existing_issues(
    item: Mapping[str, Any],
    *,
    timeout_seconds: float = 10.0,
    max_results: int = 5,
    token: str | None = None,
) -> Mapping[str, Any]:
    """Search the target system's GitHub issue tracker for likely duplicates."""

    system = str(item.get("system") or "").strip().lower()
    repository = SYSTEM_ISSUE_REPOSITORIES.get(system)
    terms = _search_terms(item)
    if not repository:
        return {
            "status": "unsupported_system",
            "system": system,
            "repository": None,
            "terms": terms,
            "items": (),
        }
    query = f"repo:{repository} is:issue {' '.join(terms)}"
    url = "https://api.github.com/search/issues?" + urlencode({"q": query, "per_page": str(max_results)})
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "graph-sym-perf",
    }
    auth_token = token or os.environ.get("GITHUB_TOKEN")
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {
            "status": "search_failed",
            "system": system,
            "repository": repository,
            "terms": terms,
            "query": query,
            "error": str(exc),
            "items": (),
        }
    items = tuple(_github_issue_payload(item) for item in payload.get("items") or ())
    return {
        "status": "searched",
        "system": system,
        "repository": repository,
        "terms": terms,
        "query": query,
        "items": items,
        "total_count": int(payload.get("total_count") or 0),
    }


def _search_for_existing_issues(
    triage_item: Mapping[str, Any],
    validation: Mapping[str, Any],
    *,
    enabled: bool,
    searcher,
) -> Mapping[str, Any]:
    item = dict(triage_item)
    item.update({key: value for key, value in validation.items() if value is not None})
    if not enabled:
        return {
            "status": "not_searched",
            "system": item.get("system"),
            "terms": _search_terms(item),
            "items": (),
        }
    if searcher is not None:
        return dict(searcher(item))
    return dict(search_existing_issues(item))


def _search_terms(item: Mapping[str, Any]) -> tuple[str, ...]:
    terms: list[str] = ["performance", "Cypher"]
    for label in tuple(item.get("root_cause_labels") or ())[:2]:
        if label:
            terms.append(str(label))
    for rule in tuple((item.get("transform_rules") or {}).keys())[:2]:
        if rule:
            terms.append(str(rule).replace("_", " "))
    plan_sig = str(item.get("plan_signature") or item.get("metric_signature") or "")
    for token in ("db_hits", "rows", "filter", "scan", "expand"):
        if token in plan_sig:
            terms.append(token)
    return tuple(dict.fromkeys(term for term in terms if term.strip()))


def _github_issue_payload(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "html_url": item.get("html_url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _is_reportable(validation: Mapping[str, Any]) -> bool:
    if not validation:
        return False
    if not validation.get("equivalence_passed"):
        return False
    support_level = _support_value(validation.get("support_level"))
    return is_profile_supported_level(support_level) or is_runtime_supported_level(support_level)


def _external_issue_status(search_payload: Mapping[str, Any]) -> str:
    if search_payload.get("status") == "not_searched":
        return "not_searched"
    if search_payload.get("status") == "search_failed":
        return "search_failed"
    if search_payload.get("status") == "unsupported_system":
        return "unsupported_system"
    if search_payload.get("items"):
        return "possible_duplicate"
    return "new_candidate"


def _developer_status(triage_item: Mapping[str, Any], validation: Mapping[str, Any]) -> str:
    for payload in (validation, triage_item, triage_item.get("metadata") or {}):
        value = payload.get("developer_status") if isinstance(payload, Mapping) else None
        if value:
            return str(value)
    return "unknown"


def _known_issue_url(triage_item: Mapping[str, Any], validation: Mapping[str, Any]) -> str | None:
    for payload in (validation, triage_item, triage_item.get("artifact_paths") or {}, triage_item.get("metadata") or {}):
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("issue_url")
        if value:
            return str(value)
    return None


def _first_existing_issue_url(search_payload: Mapping[str, Any]) -> str | None:
    items = tuple(search_payload.get("items") or ())
    if not items:
        return None
    first = items[0]
    if not isinstance(first, Mapping):
        return None
    return str(first.get("html_url") or "") or None


def _status_row(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "case_id": entry.get("case_id"),
        "system": entry.get("system"),
        "status": entry.get("status"),
        "developer_status": entry.get("developer_status"),
        "issue_url": entry.get("issue_url"),
        "duplicate_of": entry.get("duplicate_of"),
        "support_level": entry.get("support_level"),
        "latency_ratio": entry.get("latency_ratio"),
        "db_hits_ratio": entry.get("db_hits_ratio"),
        "rows_ratio": entry.get("rows_ratio"),
        "last_checked_at": entry.get("last_checked_at"),
    }


def _load_reported_issues(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(payload, Mapping):
        return [item for item in payload.get("issues") or () if isinstance(item, Mapping)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _merge_reported_issues(
    existing: list[Mapping[str, Any]],
    current: list[Mapping[str, Any]],
    *,
    checked_at: str,
) -> list[Mapping[str, Any]]:
    merged = {str(item.get("case_id")): dict(item) for item in existing if item.get("case_id")}
    for item in current:
        key = str(item.get("case_id") or "")
        if not key:
            continue
        prior = merged.get(key, {})
        merged[key] = {
            **prior,
            **{field: value for field, value in item.items() if value is not None},
            "first_seen_at": prior.get("first_seen_at") or checked_at,
            "last_checked_at": checked_at,
        }
    return sorted(merged.values(), key=lambda item: (str(item.get("system") or ""), str(item.get("case_id") or "")))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(yaml.safe_dump(to_jsonable(payload), allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_feedback_summary(path: Path, rows: list[Mapping[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("developer_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        "# Developer Feedback Summary",
        "",
        f"- Tracked issues: {len(rows)}",
        "",
        "| Developer Status | Count |",
        "|---|---:|",
    ]
    for status, count in sorted(counts.items()):
        lines.append(f"| {status} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _csv_value(value: Any) -> Any:
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
    return value


def _support_value(value: Any) -> str:
    return support_level_value(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
