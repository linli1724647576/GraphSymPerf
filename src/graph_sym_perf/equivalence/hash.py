"""Result normalization and hash-based equivalence checks."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from graph_sym_perf.core import EquivalenceRecord, stable_json_dumps, to_jsonable


def normalize_rows(rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    normalized = [to_jsonable(dict(row)) for row in rows]
    normalized.sort(key=stable_json_dumps)
    return tuple(normalized)


def result_hash(rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]) -> str:
    payload = stable_json_dumps(normalize_rows(rows)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compare_results(
    case_id: str,
    left_rows: tuple[Mapping[str, Any], ...],
    right_rows: tuple[Mapping[str, Any], ...],
    max_result_rows: int,
) -> EquivalenceRecord:
    if len(left_rows) > max_result_rows or len(right_rows) > max_result_rows:
        return EquivalenceRecord(
            case_id=case_id,
            passed=False,
            left_hash=None,
            right_hash=None,
            left_count=len(left_rows),
            right_count=len(right_rows),
            reason="SKIPPED_RESULT_TOO_LARGE",
            sample_left=tuple(normalize_rows(left_rows)[:5]),
            sample_right=tuple(normalize_rows(right_rows)[:5]),
        )
    left_hash = result_hash(left_rows)
    right_hash = result_hash(right_rows)
    passed = left_hash == right_hash and len(left_rows) == len(right_rows)
    return EquivalenceRecord(
        case_id=case_id,
        passed=passed,
        left_hash=left_hash,
        right_hash=right_hash,
        left_count=len(left_rows),
        right_count=len(right_rows),
        reason="EQUIVALENT" if passed else "RESULT_MISMATCH",
        sample_left=tuple(normalize_rows(left_rows)[:5]),
        sample_right=tuple(normalize_rows(right_rows)[:5]),
    )
