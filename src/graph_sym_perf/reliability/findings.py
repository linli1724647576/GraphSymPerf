"""Append-only reliability evidence, isolated from runtime candidates."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from graph_sym_perf.experiment.state import ErrorKind, _append_jsonl_fsync


@dataclass(frozen=True)
class ReliabilityFinding:
    kind: ErrorKind
    query_hash: str
    graph_hash: str
    dbms_fingerprint: str
    dbms_version: str | None
    reproductions: int
    attempt_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["attempt_evidence"] = list(self.attempt_evidence)
        return payload


@dataclass(frozen=True)
class EntryReliabilityFinding:
    kind: ErrorKind
    entry_id: str
    query_hash: None
    graph_hash: str
    dbms_fingerprint: str
    dbms_version: str | None
    observed_identity: Mapping[str, object]
    reproductions: int
    attempt_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "scope": "entry",
            "kind": self.kind.value,
            "entry_id": self.entry_id,
            "query_hash": None,
            "graph_hash": self.graph_hash,
            "dbms_fingerprint": self.dbms_fingerprint,
            "dbms_version": self.dbms_version,
            "observed_identity": dict(self.observed_identity),
            "reproductions": self.reproductions,
            "attempt_evidence": list(self.attempt_evidence),
        }


class ReliabilityFindingStore:
    def __init__(self, run_dir: str | Path) -> None:
        self.path = Path(run_dir) / "reliability" / "findings.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        kind: ErrorKind,
        query_hash: str,
        graph_hash: str,
        dbms_fingerprint: str,
        dbms_version: str | None = None,
        attempt_evidence: str,
    ) -> ReliabilityFinding:
        reproductions = 1
        evidence = [attempt_evidence]
        for previous in self._read():
            if (
                previous.get("scope", "query") == "query"
                and previous.get("kind") == kind.value
                and previous.get("query_hash") == query_hash
                and previous.get("graph_hash") == graph_hash
                and previous.get("dbms_fingerprint") == dbms_fingerprint
            ):
                reproductions = max(reproductions, int(previous.get("reproductions", 0)) + 1)
                previous_evidence = previous.get("attempt_evidence", [])
                if isinstance(previous_evidence, list):
                    evidence = [*(str(item) for item in previous_evidence), attempt_evidence]
        finding = ReliabilityFinding(
            kind=kind,
            query_hash=query_hash,
            graph_hash=graph_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_version=dbms_version,
            reproductions=reproductions,
            attempt_evidence=tuple(dict.fromkeys(evidence)),
        )
        _append_jsonl_fsync(self.path, finding.to_dict())
        return finding

    def record_entry(
        self,
        *,
        kind: ErrorKind,
        entry_id: str,
        graph_hash: str,
        dbms_fingerprint: str,
        dbms_version: str | None,
        observed_identity: Mapping[str, object],
        attempt_evidence: str,
    ) -> EntryReliabilityFinding:
        reproductions = 1
        evidence = [attempt_evidence]
        canonical_observed = dict(observed_identity)
        for previous in self._read():
            if (
                previous.get("scope") == "entry"
                and previous.get("kind") == kind.value
                and previous.get("entry_id") == entry_id
                and previous.get("graph_hash") == graph_hash
                and previous.get("dbms_fingerprint") == dbms_fingerprint
                and previous.get("observed_identity") == canonical_observed
            ):
                reproductions = max(
                    reproductions,
                    int(previous.get("reproductions", 0)) + 1,
                )
                previous_evidence = previous.get("attempt_evidence", [])
                if isinstance(previous_evidence, list):
                    evidence = [
                        *(str(item) for item in previous_evidence),
                        attempt_evidence,
                    ]
        finding = EntryReliabilityFinding(
            kind=kind,
            entry_id=entry_id,
            query_hash=None,
            graph_hash=graph_hash,
            dbms_fingerprint=dbms_fingerprint,
            dbms_version=dbms_version,
            observed_identity=canonical_observed,
            reproductions=reproductions,
            attempt_evidence=tuple(dict.fromkeys(evidence)),
        )
        _append_jsonl_fsync(self.path, finding.to_dict())
        return finding

    def _read(self) -> tuple[dict[str, object], ...]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        records = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return tuple(records)


__all__ = [
    "EntryReliabilityFinding",
    "ReliabilityFinding",
    "ReliabilityFindingStore",
]
