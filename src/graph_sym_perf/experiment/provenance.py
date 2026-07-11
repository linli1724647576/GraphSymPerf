"""Fail-closed probes for the actual DBMS execution image."""

from __future__ import annotations

import re
import subprocess
from typing import Mapping

from graph_sym_perf.core import TargetSystem


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


__all__ = ["verified_container_image_digest"]
