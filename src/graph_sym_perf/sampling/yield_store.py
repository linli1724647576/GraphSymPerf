from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import fcntl


_RULE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_FORBIDDEN_INPUT_NAMES = frozenset({"plan", "profile", "pmc", "developer_status"})


@dataclass(frozen=True)
class YieldSnapshot:
    counts: Mapping[str, tuple[int, int]]

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[int, int]] = {}
        for rule_id, raw_count in self.counts.items():
            if not isinstance(rule_id, str) or _RULE_ID.fullmatch(rule_id) is None:
                raise ValueError(f"invalid rule id in yield snapshot: {rule_id!r}")
            if any(token in rule_id.lower() for token in _FORBIDDEN_INPUT_NAMES):
                raise ValueError(f"forbidden scheduler input field: {rule_id}")
            if not isinstance(raw_count, tuple) or len(raw_count) != 2:
                raise TypeError("yield count must be an (accepted, validation_passed) tuple")
            accepted, validation_passed = raw_count
            if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in raw_count):
                raise ValueError("yield counts must be non-negative integers")
            if validation_passed > accepted:
                raise ValueError("validation-passed count cannot exceed accepted count")
            normalized[rule_id] = (accepted, validation_passed)
        object.__setattr__(self, "counts", MappingProxyType(dict(sorted(normalized.items()))))

    @property
    def total_accepted(self) -> int:
        return sum(accepted for accepted, _ in self.counts.values())

    @property
    def content_hash(self) -> str:
        payload = json.dumps(dict(self.counts), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def validated_yield(self, rule_id: str) -> float:
        accepted, passed = self.counts.get(rule_id, (0, 0))
        return (passed + 1) / (accepted + 2)


class RuleQuarantineStore:
    """Append-only dynamic soundness quarantine keyed by DBMS/rule/version."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def quarantine(
        self,
        *,
        dbms: str,
        rule_id: str,
        rule_version: str,
        counterexample: Mapping[str, object],
    ) -> None:
        if not dbms or _RULE_ID.fullmatch(rule_id) is None or not rule_version:
            raise ValueError("quarantine identity is incomplete")
        if not counterexample:
            raise ValueError("quarantine requires a counterexample")
        record = {
            "counterexample": dict(counterexample),
            "dbms": dbms,
            "rule_id": rule_id,
            "rule_version": rule_version,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = _read_quarantine_records(handle.read(), self.path)
            identity = (dbms, rule_id, rule_version)
            if any(_quarantine_identity(item) == identity for item in existing):
                return
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def is_quarantined(self, dbms: str, rule_id: str, rule_version: str) -> bool:
        identity = (dbms, rule_id, rule_version)
        return any(_quarantine_identity(record) == identity for record in self.records())

    def records(self) -> tuple[dict[str, object], ...]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        return _read_quarantine_records(raw, self.path)


class ValidatedYieldStore:
    """Append-only rule outcomes sourced only from completed validation reports."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record_accepted_pair(self, *, rule_id: str, accepted_pair_hash: str) -> None:
        self._append_event(
            rule_id=rule_id,
            event_type="accepted_pair",
            evidence_hash=accepted_pair_hash,
            validation_passed_delta=0,
        )

    def record_completed_validation(
        self,
        *,
        rule_id: str,
        passed: bool,
        validation_report_hash: str,
    ) -> None:
        if not isinstance(passed, bool):
            raise TypeError("validation outcome must be boolean")
        self._append_event(
            rule_id=rule_id,
            event_type="validation_completed",
            evidence_hash=validation_report_hash,
            validation_passed_delta=int(passed),
        )

    def _append_event(
        self,
        *,
        rule_id: str,
        event_type: str,
        evidence_hash: str,
        validation_passed_delta: int,
    ) -> None:
        if _RULE_ID.fullmatch(rule_id) is None:
            raise ValueError("validated-yield event has an invalid rule id")
        if re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None:
            raise ValueError("validated-yield event requires a completed report hash or accepted-pair hash")
        event = {
            "event_type": event_type,
            "evidence_hash": evidence_hash,
            "rule_id": rule_id,
            "validation_passed_delta": validation_passed_delta,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            for line in handle.read().splitlines():
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OSError(f"corrupt validated-yield store: {self.path}") from exc
                if (
                    isinstance(existing, dict)
                    and existing.get("event_type") == event_type
                    and existing.get("evidence_hash") == evidence_hash
                ):
                    if existing.get("rule_id") != rule_id:
                        raise OSError("yield evidence hash is already bound to another rule")
                    return
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def snapshot(self, rule_ids: tuple[str, ...]) -> YieldSnapshot:
        counts: Counter[str] = Counter()
        passed: Counter[str] = Counter()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                lines = handle.read().splitlines()
        except FileNotFoundError:
            lines = []
        required = {"event_type", "evidence_hash", "rule_id", "validation_passed_delta"}
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OSError(f"corrupt validated-yield store: {self.path}") from exc
            if not isinstance(event, dict) or set(event) != required:
                raise OSError(f"invalid validated-yield event: {self.path}")
            rule_id = event["rule_id"]
            if (
                rule_id not in rule_ids
                or event["event_type"] not in {"accepted_pair", "validation_completed"}
                or event["validation_passed_delta"] not in {0, 1}
                or (event["event_type"] == "accepted_pair" and event["validation_passed_delta"] != 0)
            ):
                raise OSError(f"invalid validated-yield event values: {self.path}")
            if not isinstance(event["evidence_hash"], str) or re.fullmatch(r"[0-9a-f]{64}", event["evidence_hash"]) is None:
                raise OSError(f"invalid yield evidence hash: {self.path}")
            if event["event_type"] == "accepted_pair":
                counts[rule_id] += 1
            else:
                passed[rule_id] += event["validation_passed_delta"]
        try:
            return YieldSnapshot({rule_id: (counts[rule_id], passed[rule_id]) for rule_id in rule_ids})
        except ValueError as exc:
            raise OSError(f"validated-yield passes exceed accepted pairs: {self.path}") from exc


def _read_quarantine_records(raw: str, path: Path) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    required = {"counterexample", "dbms", "rule_id", "rule_version"}
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError(f"corrupt rule quarantine store: {path}") from exc
        if not isinstance(record, dict) or set(record) != required:
            raise OSError(f"invalid rule quarantine record: {path}")
        if not isinstance(record["counterexample"], dict) or not record["counterexample"]:
            raise OSError(f"invalid quarantine counterexample: {path}")
        if not all(isinstance(record[key], str) and record[key] for key in ("dbms", "rule_id", "rule_version")):
            raise OSError(f"invalid quarantine identity: {path}")
        records.append(record)
    return tuple(records)


def _quarantine_identity(record: Mapping[str, object]) -> tuple[object, object, object]:
    return record["dbms"], record["rule_id"], record["rule_version"]


__all__ = ["RuleQuarantineStore", "ValidatedYieldStore", "YieldSnapshot"]
