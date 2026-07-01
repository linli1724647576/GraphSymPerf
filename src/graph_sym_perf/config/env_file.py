"""Small .env loader for local experiment runs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def environment_with_dotenv(env: Mapping[str, str] | None, *, dotenv_path: str | Path = ".env") -> dict[str, str]:
    if env is not None:
        return dict(env)
    values = _read_dotenv(Path(dotenv_path))
    values.update(os.environ)
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_quotes(value.strip())
    return values


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
