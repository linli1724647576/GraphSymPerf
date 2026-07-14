"""Append-only reliability evidence, isolated from runtime candidates."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Mapping

import fcntl

from graph_sym_perf.experiment.state import ErrorKind


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
        self.lock_path = self.path.with_name("findings.lock")
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
        with self._locked_records() as records:
            reproductions = 1
            evidence = [attempt_evidence]
            for previous in records:
                if (
                    previous.get("scope", "query") == "query"
                    and previous.get("kind") == kind.value
                    and previous.get("query_hash") == query_hash
                    and previous.get("graph_hash") == graph_hash
                    and previous.get("dbms_fingerprint") == dbms_fingerprint
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
            finding = ReliabilityFinding(
                kind=kind,
                query_hash=query_hash,
                graph_hash=graph_hash,
                dbms_fingerprint=dbms_fingerprint,
                dbms_version=dbms_version,
                reproductions=reproductions,
                attempt_evidence=tuple(dict.fromkeys(evidence)),
            )
            self._atomic_rewrite((*records, finding.to_dict()))
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
        with self._locked_records() as records:
            reproductions = 1
            evidence = [attempt_evidence]
            canonical_observed = dict(observed_identity)
            for previous in records:
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
            self._atomic_rewrite((*records, finding.to_dict()))
            return finding

    @contextmanager
    def _locked_records(self) -> Iterator[tuple[dict[str, object], ...]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read_strict()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_strict(self) -> tuple[dict[str, object], ...]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("reliability JSONL has a truncated tail")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise ValueError("reliability JSONL is not UTF-8") from exc
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"reliability JSONL line {line_number} is malformed"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"reliability JSONL line {line_number} is not an object"
                )
            records.append(value)
        return tuple(records)

    def _atomic_rewrite(self, records: tuple[dict[str, object], ...]) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary = handle.name
                for record in records:
                    handle.write(
                        json.dumps(
                            record,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        except (OSError, TypeError, ValueError):
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "EntryReliabilityFinding",
    "ReliabilityFinding",
    "ReliabilityFindingStore",
]
