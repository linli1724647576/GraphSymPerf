"""Run-scoped, append-only ownership for accepted GDsmith ASTs and pairs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import fcntl


_HASH = re.compile(r"^[0-9a-f]{64}$")


class AcceptedCorpusRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def reserve(
        self,
        *,
        source_ast_hash: str,
        pair_hash: str,
        target: str,
        shard_index: int,
    ) -> str | None:
        _validate_hash(source_ast_hash, "source_ast_hash")
        _validate_hash(pair_hash, "pair_hash")
        if not target or not isinstance(shard_index, int) or isinstance(shard_index, bool) or shard_index < 0:
            raise ValueError("registry owner must contain a target and non-negative shard index")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            records = _read_records(handle.read(), self.path)
            owner = (target, shard_index)
            for record in records:
                candidate_owner = (record["target"], record["shard_index"])
                if candidate_owner == owner and record["source_ast_hash"] == source_ast_hash and record["pair_hash"] == pair_hash:
                    return None
            if any(record["source_ast_hash"] == source_ast_hash and (record["target"], record["shard_index"]) != owner for record in records):
                return "CROSS_SHARD_DUPLICATE_AST"
            if any(record["pair_hash"] == pair_hash and (record["target"], record["shard_index"]) != owner for record in records):
                return "CROSS_SHARD_DUPLICATE_PAIR"
            payload = {
                "pair_hash": pair_hash,
                "shard_index": shard_index,
                "source_ast_hash": source_ast_hash,
                "target": target,
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return None


def _read_records(raw: str, path: Path) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError(f"corrupt accepted corpus registry: {path}") from exc
        if not isinstance(value, dict) or set(value) != {"pair_hash", "shard_index", "source_ast_hash", "target"}:
            raise OSError(f"corrupt accepted corpus registry record: {path}")
        try:
            _validate_hash(value["source_ast_hash"], "source_ast_hash")
            _validate_hash(value["pair_hash"], "pair_hash")
        except (TypeError, ValueError) as exc:
            raise OSError(f"corrupt accepted corpus registry hash: {path}") from exc
        if (
            not isinstance(value["target"], str)
            or not value["target"]
            or not isinstance(value["shard_index"], int)
            or isinstance(value["shard_index"], bool)
            or value["shard_index"] < 0
        ):
            raise OSError(f"corrupt accepted corpus registry owner: {path}")
        records.append(value)
    return tuple(records)


def _validate_hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase 64-hex")


__all__ = ["AcceptedCorpusRegistry"]
