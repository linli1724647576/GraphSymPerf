"""Fail-closed probes for the actual DBMS execution artifact."""

from __future__ import annotations

import re
import subprocess
import hashlib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Mapping

from graph_sym_perf.core import TargetSystem


@dataclass(frozen=True)
class ExecutionProvenance:
    mode: str
    digest: str


def verified_execution_provenance(
    environment: Mapping[str, str],
    target: TargetSystem,
) -> ExecutionProvenance | None:
    if target is TargetSystem.LADYBUGDB:
        expected = environment.get("GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST", "")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is None:
            return None
        actual = installed_distribution_digest("ladybug")
        return ExecutionProvenance("embedded_package", expected) if actual == expected else None
    digest = verified_container_image_digest(environment, target)
    return ExecutionProvenance("container_image", digest) if digest is not None else None


def verified_container_image_digest(environment: Mapping[str, str], target: TargetSystem) -> str | None:
    prefix = f"GRAPHSYMPERF_{target.value.upper()}"
    expected = environment.get(f"{prefix}_IMAGE_DIGEST", "")
    container = environment.get(f"{prefix}_CONTAINER", "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is None or not container:
        return None
    try:
        completed = subprocess.run(
            ("docker", "inspect", "--format={{.Image}}", container),
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    actual = completed.stdout.strip() if completed.returncode == 0 else ""
    return expected if actual == expected else None


def installed_distribution_digest(package: str) -> str | None:
    try:
        distribution = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        return None
    files = tuple(
        sorted(
            (
                entry
                for entry in (distribution.files or ())
                if not str(entry).endswith((".pyc", ".pyo"))
            ),
            key=str,
        )
    )
    if not files:
        return None
    digest = hashlib.sha256()
    digest.update(f"{package}\0{distribution.version}\0".encode("utf-8"))
    hashed_files = 0
    for entry in files:
        path = Path(distribution.locate_file(entry))
        if not path.is_file():
            continue
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(str(entry).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
        hashed_files += 1
    return "sha256:" + digest.hexdigest() if hashed_files else None


__all__ = [
    "ExecutionProvenance",
    "installed_distribution_digest",
    "verified_container_image_digest",
    "verified_execution_provenance",
]
