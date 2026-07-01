"""CSV table emitters for experiment-level summaries."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping


def write_table(path: str | Path, rows: Iterable[Mapping[str, object]]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    items = list(rows)
    fieldnames: list[str] = []
    for row in items:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(str(key))
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in items:
            writer.writerow({key: row.get(key) for key in fieldnames})
    return output
