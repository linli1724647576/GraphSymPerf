"""Append-only reliability evidence, isolated from runtime candidates."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

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
                previous.get("kind") == kind.value
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


__all__ = ["ReliabilityFinding", "ReliabilityFindingStore"]
